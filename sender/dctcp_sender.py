"""
DCTCP-style sender — windowed UDP with ECN-driven AIMD.

Per PLAN.md week 2:
    F = marked / total per RTT
    α = (1 - g)·α + g·F,   g = 1/16
    marks  ⇒ W = W·(1 - α/2)
    no marks ⇒ W += 1
    loss   ⇒ go-back-N on 3·baseRTT timeout, W ← W/2

Runs over the same UDP+ACK harness as the HPCC sender so the
comparison is apples-to-apples.

Run inside the dev container, with the dumbbell topology + tables up
and the reflector listening on the receiver:

    python3 -m sender.dctcp_sender 10.0.0.10 --duration 60 \
        --log /tmp/dctcp.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import socket
import threading
import time

from sender.packet_format import (
    ACK_PAYLOAD_SIZE,
    ACK_PORT,
    DATA_PORT,
    INT_HOP_SIZE,
    SHIM_FLAG_ECN_ECHO,
    SHIM_SIZE,
    AckPayload,
    DataPacket,
    DataPayload,
    Shim,
)

log = logging.getLogger("dctcp_sender")

# ECT(0) — opt this flow into ECN so the switch will mark CE under load.
_TOS_ECT0 = 0x02

DEFAULT_W_INIT = 4
DEFAULT_W_MAX = 64
DEFAULT_W_MIN = 1
DEFAULT_G = 1.0 / 16
DEFAULT_PADDING = 1400  # zero bytes after the 16 B data payload to fatten frames

# Timeout for loss recovery, clamped to a sane range.
_RTO_MIN_S = 0.020
_RTO_MAX_S = 1.000


def _now_us() -> int:
    return time.time_ns() // 1000


def _rto_seconds(base_rtt_s: float) -> float:
    return max(_RTO_MIN_S, min(_RTO_MAX_S, 3.0 * base_rtt_s))


class DctcpSender:
    """Stateful DCTCP-style controller over UDP+ACK."""

    def __init__(
        self,
        receiver_ip: str,
        data_port: int,
        ack_port: int,
        w_init: int,
        w_max: int,
        g: float,
        padding: int,
        base_rtt_s: float,
        log_path: str,
    ):
        self.receiver_ip = receiver_ip
        self.data_port = data_port
        self.ack_port = ack_port
        self.w = float(w_init)
        self.w_min = DEFAULT_W_MIN
        self.w_max = w_max
        self.g = g
        self.alpha = 0.0
        self.padding = padding
        self.base_rtt_s = base_rtt_s
        self.rto_s = _rto_seconds(base_rtt_s)
        self.log_path = log_path

        self.sock = self._open_socket()

        # Sequence + in-flight state.
        self.next_seq = 0
        self.cum_acked_seq = -1
        self.in_flight: dict[int, tuple[int, int]] = {}  # seq -> (send_ts_us, rtx)
        self.lock = threading.Lock()

        # Per-RTT bookkeeping for α update.
        self.last_update_seq = -1
        self.rtt_window_total = 0
        self.rtt_window_marked = 0

        # In-memory event buffer; flushed at shutdown.
        self.events: list[tuple] = []
        self.stop = threading.Event()

    def _open_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _TOS_ECT0)
        s.bind(("0.0.0.0", self.ack_port))
        s.settimeout(0.05)
        return s

    def _log_event(self, event: str, seq: int, rtt_us: int = 0, ecn: int = 0) -> None:
        self.events.append((
            _now_us(), event, seq, rtt_us, ecn,
            round(self.w, 3),
            round(self.alpha, 5),
            self.cum_acked_seq,
            len(self.in_flight),
        ))

    def _build_packet_bytes(self, seq: int) -> bytes:
        pkt = DataPacket(payload=DataPayload(seq=seq, send_ts_us=_now_us()))
        body = pkt.pack()
        if self.padding > 0:
            body = body + b"\x00" * self.padding
        return body

    def _send_locked(self, seq: int) -> None:
        try:
            self.sock.sendto(self._build_packet_bytes(seq),
                             (self.receiver_ip, self.data_port))
        except OSError as e:
            log.warning("sendto failed for seq %d: %s", seq, e)
            return
        ts = _now_us()
        if seq in self.in_flight:
            _, rtx = self.in_flight[seq]
            self.in_flight[seq] = (ts, rtx + 1)
            self._log_event("rtx", seq)
        else:
            self.in_flight[seq] = (ts, 0)
            self._log_event("send", seq)

    def _on_ack_locked(self, ack_seq: int, ecn_echo: int, rtt_us: int) -> None:
        # Cumulative ACK semantics: any seq <= ack_seq is acknowledged.
        if ack_seq > self.cum_acked_seq:
            for s in range(self.cum_acked_seq + 1, ack_seq + 1):
                self.in_flight.pop(s, None)
            self.cum_acked_seq = ack_seq

        self.rtt_window_total += 1
        if ecn_echo:
            self.rtt_window_marked += 1
        self._log_event("ack", ack_seq, rtt_us, ecn_echo)
        self._maybe_update_window_locked()

    def _maybe_update_window_locked(self) -> None:
        # Per-RTT update: fire when at least W packets have been ACKed since
        # the previous update.
        if self.cum_acked_seq < self.last_update_seq + int(self.w):
            return
        total = self.rtt_window_total
        if total == 0:
            return
        f = self.rtt_window_marked / total
        self.alpha = (1.0 - self.g) * self.alpha + self.g * f

        if self.rtt_window_marked > 0:
            self.w = max(self.w_min, self.w * (1.0 - self.alpha / 2.0))
        else:
            self.w = min(self.w_max, self.w + 1.0)

        self._log_event("update", -1)
        self.last_update_seq = self.cum_acked_seq
        self.rtt_window_total = 0
        self.rtt_window_marked = 0

    def _ack_listener(self) -> None:
        while not self.stop.is_set():
            try:
                data, _src = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            if len(data) < SHIM_SIZE:
                continue
            shim = Shim.unpack(data)
            hops_size = shim.hop_count * INT_HOP_SIZE
            ack_off = SHIM_SIZE + hops_size
            if len(data) < ack_off + ACK_PAYLOAD_SIZE:
                continue
            ack = AckPayload.unpack(data[ack_off:ack_off + ACK_PAYLOAD_SIZE])
            ecn = 1 if (shim.flags & SHIM_FLAG_ECN_ECHO) else 0
            with self.lock:
                send_ts = self.in_flight.get(ack.ack_seq, (0, 0))[0]
                if send_ts == 0 and ack.ack_seq <= self.cum_acked_seq:
                    continue  # stale duplicate
                rtt_us = _now_us() - send_ts if send_ts else 0
                self._on_ack_locked(ack.ack_seq, ecn, rtt_us)

    def _rto_checker(self) -> None:
        while not self.stop.is_set():
            time.sleep(self.rto_s / 4)
            with self.lock:
                if not self.in_flight:
                    continue
                now = _now_us()
                rto_us = int(self.rto_s * 1_000_000)
                expired = [s for s, (ts, _) in self.in_flight.items()
                           if now - ts >= rto_us]
                if not expired:
                    continue
                # Multiplicative cut on RTO + go-back-N. (Real DCTCP follows
                # TCP RTO behavior — cwnd=1 + slow-start. We just halve W;
                # in this simulator drops are rare enough that the difference
                # doesn't matter.)
                self.w = max(self.w_min, self.w / 2)
                self._log_event("rto", -1)
                seqs = sorted(self.in_flight.keys())
                self.in_flight.clear()
                for s in seqs:
                    self._send_locked(s)

    def run(self, duration_s: float, max_packets: int | None = None) -> dict:
        rx = threading.Thread(target=self._ack_listener, daemon=True)
        rx.start()
        rto_t = threading.Thread(target=self._rto_checker, daemon=True)
        rto_t.start()

        end = time.time() + duration_s
        try:
            while time.time() < end:
                with self.lock:
                    while (len(self.in_flight) < int(self.w)
                           and not self.stop.is_set()
                           and (max_packets is None
                                or self.next_seq < max_packets)):
                        self._send_locked(self.next_seq)
                        self.next_seq += 1
                # Early-exit when all bytes are sent AND drained.
                if max_packets is not None and self.next_seq >= max_packets:
                    with self.lock:
                        if not self.in_flight:
                            break
                # Small yield so the listener thread runs.
                time.sleep(0.0005)
        finally:
            # Let late ACKs drain.
            time.sleep(min(self.rto_s * 2, 0.5))
            self.stop.set()
            rx.join(timeout=1.0)
            rto_t.join(timeout=1.0)
            self.sock.close()
            self._dump_log()

        return {
            "sent": self.next_seq,
            "acked": self.cum_acked_seq + 1,
            "final_w": round(self.w, 3),
            "final_alpha": round(self.alpha, 5),
            "events": len(self.events),
        }

    def _dump_log(self) -> None:
        with open(self.log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "ts_us", "event", "seq", "rtt_us", "ecn_echo",
                "w", "alpha", "cum_acked", "in_flight",
            ])
            w.writerows(self.events)


def measure_base_rtt(receiver_ip: str, data_port: int, ack_port: int,
                     n_probes: int = 10) -> float:
    """Send a few unloaded probes and return the median RTT (seconds)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _TOS_ECT0)
    s.bind(("0.0.0.0", ack_port))
    s.settimeout(0.5)
    rtts: list[float] = []
    try:
        for i in range(n_probes):
            seq = 1_000_000 + i
            pkt = DataPacket(payload=DataPayload(seq=seq, send_ts_us=_now_us()))
            ts = _now_us()
            s.sendto(pkt.pack(), (receiver_ip, data_port))
            try:
                _data, _src = s.recvfrom(2048)
                rtts.append((_now_us() - ts) / 1_000_000)
            except socket.timeout:
                pass
            time.sleep(0.02)
    finally:
        s.close()
    if not rtts:
        return 0.005  # 5 ms fallback
    rtts.sort()
    return rtts[len(rtts) // 2]


def main() -> None:
    p = argparse.ArgumentParser(description="DCTCP-style UDP sender")
    p.add_argument("receiver_ip")
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--log", default="/tmp/dctcp.csv")
    p.add_argument("--data-port", type=int, default=DATA_PORT)
    p.add_argument("--ack-port", type=int, default=ACK_PORT)
    p.add_argument("--w-init", type=int, default=DEFAULT_W_INIT)
    p.add_argument("--w-max", type=int, default=DEFAULT_W_MAX)
    p.add_argument("--g", type=float, default=DEFAULT_G,
                   help="α EWMA gain (default 1/16)")
    p.add_argument("--padding", type=int, default=DEFAULT_PADDING,
                   help="zero bytes after data payload (fattens frames; "
                        "default 1400 ≈ MTU-sized).")
    p.add_argument("--base-rtt", type=float, default=0.0,
                   help="baseRTT in seconds; 0 = measure with probes")
    p.add_argument("--max-packets", type=int, default=None,
                   help="stop after sending this many packets (default unlimited)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    base_rtt = args.base_rtt
    if base_rtt == 0.0:
        print("measuring baseRTT ...")
        base_rtt = measure_base_rtt(args.receiver_ip, args.data_port, args.ack_port)
        print(f"baseRTT = {base_rtt * 1000:.2f} ms")

    sender = DctcpSender(
        receiver_ip=args.receiver_ip,
        data_port=args.data_port,
        ack_port=args.ack_port,
        w_init=args.w_init,
        w_max=args.w_max,
        g=args.g,
        padding=args.padding,
        base_rtt_s=base_rtt,
        log_path=args.log,
    )
    summary = sender.run(args.duration, max_packets=args.max_packets)
    print(f"sent={summary['sent']} acked={summary['acked']} "
          f"final_w={summary['final_w']} final_alpha={summary['final_alpha']} "
          f"events={summary['events']} log={args.log}")


if __name__ == "__main__":
    main()
