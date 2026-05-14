"""
HPCC sender v2 — proper W_ref / incarnation snapshot per packet.

Difference vs sender/hpcc_sender.py:

The base HPCC sender keeps `self.w_ref` as "the W after the last
update". The window-update gate uses that scalar. The downside is
that ACKs from packets sent BEFORE a multiplicative decrease still
push U upward (the queue they saw is recent state of the world);
the next update gate uses the now-shrunken w_ref, fires sooner,
and applies ANOTHER MD to W. Result: cascading reductions in one
congestion epoch, W oscillates down to W_min, low utilization.

v2 attaches the (w_ref, incarnation) snapshot at SEND time to each
in-flight seq. On the update-gate check we use the W_REF FROM THE
EARLIEST UNACKED packet (i.e. the cum_acked + 1 entry) — that's
the W that was in effect when the oldest still-outstanding feedback
was emitted. If a packet's incarnation is STALE (older than the
current incarnation), its ACK only advances cum_acked; it does not
contribute U to a fresh MD decision.

Same wire format, same control loop interface, same CSV schema as
sender/hpcc_sender.py. Drop-in replacement.
"""
from __future__ import annotations

import argparse
import csv
import logging
import socket
import sys
import threading
import time

sys.path.insert(0, "/workspace")

from sender.packet_format import (  # noqa: E402
    ACK_PAYLOAD_SIZE,
    ACK_PORT,
    DATA_PORT,
    INT_HOP_SIZE,
    SHIM_SIZE,
    AckPayload,
    DataPacket,
    DataPayload,
    IntHop,
    Shim,
)

log = logging.getLogger("hpcc_sender_v2")

_TOS_ECT0 = 0x02
DEFAULT_W_INIT = 8
DEFAULT_W_MIN = 1
DEFAULT_W_MAX = 64
DEFAULT_ETA = 0.99
DEFAULT_W_AI = 3.0
DEFAULT_TAU = 0.05
DEFAULT_PADDING = 1400
_QDEPTH_PKT_BYTES = 1500
_RTO_MIN_S = 0.020
_RTO_MAX_S = 1.000


def _now_us() -> int:
    return time.time_ns() // 1000


def _rto_seconds(base_rtt_s: float) -> float:
    return max(_RTO_MIN_S, min(_RTO_MAX_S, 3.0 * base_rtt_s))


class HpccSenderV2:
    """v2: proper W_ref/incarnation snapshot per packet."""

    def __init__(
        self,
        receiver_ip: str,
        data_port: int,
        ack_port: int,
        w_init: int,
        w_max: int,
        eta: float,
        w_ai: float,
        tau_over_T: float,
        padding: int,
        base_rtt_s: float,
        log_path: str,
    ):
        self.receiver_ip = receiver_ip
        self.data_port = data_port
        self.ack_port = ack_port
        self.w = float(w_init)
        self.w_ref = float(w_init)
        self.incarnation = 0
        self.w_min = DEFAULT_W_MIN
        self.w_max = w_max
        self.eta = eta
        self.w_ai = w_ai
        self.tau_over_T = tau_over_T
        self.u_smoothed = 1.0
        self.padding = padding
        self.base_rtt_s = base_rtt_s
        self.rto_s = _rto_seconds(base_rtt_s)
        self.log_path = log_path

        self.sock = self._open_socket()
        self.next_seq = 0
        self.cum_acked_seq = -1
        # in_flight[seq] = (send_ts_us, w_ref_at_send, incarnation_at_send, rtx)
        self.in_flight: dict[int, tuple[int, float, int, int]] = {}
        self.lock = threading.Lock()
        self.hop_last: dict[tuple[int, int], tuple[int, int]] = {}
        self.last_update_seq = -1
        self.last_update_time = time.monotonic()
        self.events: list[tuple] = []
        self.stop = threading.Event()

    def _open_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _TOS_ECT0)
        s.bind(("0.0.0.0", self.ack_port))
        s.settimeout(0.05)
        return s

    def _log(self, event: str, seq: int, rtt_us: int = 0, u: float = 0.0,
             hop_count: int = 0) -> None:
        self.events.append((
            _now_us(), event, seq, rtt_us, hop_count,
            round(self.w, 3), round(self.w_ref, 3), self.incarnation,
            round(u, 5), round(self.u_smoothed, 5),
            self.cum_acked_seq, len(self.in_flight),
        ))

    def _build(self, seq: int) -> bytes:
        body = DataPacket(payload=DataPayload(seq=seq, send_ts_us=_now_us())).pack()
        if self.padding > 0:
            body = body + b"\x00" * self.padding
        return body

    def _send_locked(self, seq: int) -> None:
        try:
            self.sock.sendto(self._build(seq),
                             (self.receiver_ip, self.data_port))
        except OSError as e:
            log.warning("sendto failed: %s", e)
            return
        ts = _now_us()
        if seq in self.in_flight:
            _, w_snap, inc_snap, rtx = self.in_flight[seq]
            self.in_flight[seq] = (ts, w_snap, inc_snap, rtx + 1)
            self._log("rtx", seq)
        else:
            # Snapshot the (w_ref, incarnation) AT SEND TIME — used later
            # to decide whether this packet's ACK gets to contribute to
            # an MD decision.
            self.in_flight[seq] = (ts, self.w_ref, self.incarnation, 0)
            self._log("send", seq)

    def _parse_ack(self, data: bytes):
        if len(data) < SHIM_SIZE:
            return None
        shim = Shim.unpack(data)
        n = shim.hop_count
        if n > 8 or len(data) < SHIM_SIZE + n * INT_HOP_SIZE + ACK_PAYLOAD_SIZE:
            return None
        hops: list[IntHop] = []
        off = SHIM_SIZE
        for _ in range(n):
            hops.append(IntHop.unpack(data[off:off + INT_HOP_SIZE]))
            off += INT_HOP_SIZE
        ack = AckPayload.unpack(data[off:off + ACK_PAYLOAD_SIZE])
        return shim, hops, ack

    def _compute_u(self, hops: list[IntHop]) -> float:
        u_max = 0.0
        for hop in hops:
            if hop.link_bps == 0:
                continue
            key = (hop.switch_id, hop.egress_port)
            last = self.hop_last.get(key)
            self.hop_last[key] = (hop.egress_tstamp_us, hop.tx_byte_count)
            if last is None:
                continue
            last_ts, last_bytes = last
            dt = hop.egress_tstamp_us - last_ts
            if dt <= 0:
                continue
            db = hop.tx_byte_count - last_bytes
            if db < 0:
                continue
            tx_rate = (db * 8.0 * 1_000_000.0) / dt
            b_bytes = hop.link_bps * self.base_rtt_s / 8.0
            if b_bytes <= 0:
                continue
            q_bytes = hop.qdepth * _QDEPTH_PKT_BYTES
            u_i = q_bytes / b_bytes + tx_rate / hop.link_bps
            if u_i > u_max:
                u_max = u_i
        return u_max

    def _on_ack_locked(self, ack_seq: int, hops: list[IntHop], rtt_us: int,
                       incarnation_at_send: int) -> None:
        if ack_seq > self.cum_acked_seq:
            for s in range(self.cum_acked_seq + 1, ack_seq + 1):
                self.in_flight.pop(s, None)
            self.cum_acked_seq = ack_seq

        # v2: stale-incarnation ACKs do NOT contribute to U updates. They
        # represent feedback from a congestion epoch we've already reacted
        # to; folding them in would double-count.
        if incarnation_at_send == self.incarnation:
            u = self._compute_u(hops)
            if u > 0:
                self.u_smoothed = (
                    (1.0 - self.tau_over_T) * self.u_smoothed
                    + self.tau_over_T * u
                )
            self._log("ack", ack_seq, rtt_us, u, len(hops))
        else:
            # Log the ack but don't move u_smoothed.
            self._log("ack-stale", ack_seq, rtt_us, 0.0, len(hops))
        self._maybe_update_window_locked()

    def _earliest_unacked_w_ref(self) -> float:
        """W_ref of the earliest unACKed packet, if any; else self.w_ref."""
        if not self.in_flight:
            return self.w_ref
        earliest_seq = min(self.in_flight)
        return self.in_flight[earliest_seq][1]

    def _maybe_update_window_locked(self) -> None:
        now = time.monotonic()
        gate_w = self._earliest_unacked_w_ref()
        if (self.cum_acked_seq < self.last_update_seq + int(max(gate_w, 1))
                or now - self.last_update_time < self.base_rtt_s):
            return

        # The W_REF used in the formula is the W of the earliest unACKed
        # packet — the W that was in effect when feedback for this update
        # was generated.
        u_eff = max(self.u_smoothed, 1e-3)
        w_new = gate_w / (u_eff / self.eta) + self.w_ai
        w_new = max(self.w_min, min(self.w_max, w_new))

        if w_new < self.w_ref:
            self.incarnation += 1
        self.w_ref = w_new
        self.w = w_new
        self.last_update_seq = self.cum_acked_seq
        self.last_update_time = now
        self._log("update", -1, u=u_eff)

    def _ack_listener(self) -> None:
        while not self.stop.is_set():
            try:
                data, _src = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            parsed = self._parse_ack(data)
            if parsed is None:
                continue
            shim, hops, ack = parsed
            with self.lock:
                meta = self.in_flight.get(ack.ack_seq)
                if meta is None and ack.ack_seq <= self.cum_acked_seq:
                    continue
                send_ts = meta[0] if meta else 0
                inc_snap = meta[2] if meta else self.incarnation
                rtt_us = _now_us() - send_ts if send_ts else 0
                self._on_ack_locked(ack.ack_seq, hops, rtt_us, inc_snap)

    def _rto_checker(self) -> None:
        while not self.stop.is_set():
            time.sleep(self.rto_s / 4)
            with self.lock:
                if not self.in_flight:
                    continue
                now = _now_us()
                rto_us = int(self.rto_s * 1_000_000)
                expired = [s for s, m in self.in_flight.items()
                           if now - m[0] >= rto_us]
                if not expired:
                    continue
                self.w_ref = max(self.w_min, self.w_ref / 2)
                self.w = self.w_ref
                self.incarnation += 1
                self._log("rto", -1)
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
                if max_packets is not None and self.next_seq >= max_packets:
                    with self.lock:
                        if not self.in_flight:
                            break
                time.sleep(0.0005)
        finally:
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
            "final_u_smoothed": round(self.u_smoothed, 5),
            "incarnation": self.incarnation,
            "events": len(self.events),
        }

    def _dump_log(self) -> None:
        with open(self.log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "ts_us", "event", "seq", "rtt_us", "hop_count",
                "w", "w_ref", "incarnation", "u", "u_smoothed",
                "cum_acked", "in_flight",
            ])
            w.writerows(self.events)


def main() -> None:
    p = argparse.ArgumentParser(description="HPCC sender v2 (proper W_ref snapshot)")
    p.add_argument("receiver_ip")
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--log", default="/tmp/hpcc_v2.csv")
    p.add_argument("--data-port", type=int, default=DATA_PORT)
    p.add_argument("--ack-port", type=int, default=ACK_PORT)
    p.add_argument("--w-init", type=int, default=DEFAULT_W_INIT)
    p.add_argument("--w-max", type=int, default=DEFAULT_W_MAX)
    p.add_argument("--eta", type=float, default=DEFAULT_ETA)
    p.add_argument("--w-ai", type=float, default=DEFAULT_W_AI)
    p.add_argument("--tau", type=float, default=DEFAULT_TAU)
    p.add_argument("--padding", type=int, default=DEFAULT_PADDING)
    p.add_argument("--base-rtt", type=float, default=0.006)
    p.add_argument("--max-packets", type=int, default=None)
    args = p.parse_args()

    sender = HpccSenderV2(
        receiver_ip=args.receiver_ip,
        data_port=args.data_port,
        ack_port=args.ack_port,
        w_init=args.w_init,
        w_max=args.w_max,
        eta=args.eta,
        w_ai=args.w_ai,
        tau_over_T=args.tau,
        padding=args.padding,
        base_rtt_s=args.base_rtt,
        log_path=args.log,
    )
    summary = sender.run(args.duration, max_packets=args.max_packets)
    print(f"sent={summary['sent']} acked={summary['acked']} "
          f"final_w={summary['final_w']} u_smoothed={summary['final_u_smoothed']} "
          f"incarnations={summary['incarnation']} events={summary['events']} "
          f"log={args.log}")


if __name__ == "__main__":
    main()
