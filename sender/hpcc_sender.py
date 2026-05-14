"""
HPCC-style sender — INT-driven window controller.

Per PLAN.md week 3:
    For each hop i carried back in an ACK's INT stack:
        txRate_i = ΔB · 8 · 1e6 / Δt_us
        u_i      = qdepth_bytes / B_i + txRate_i / link_bps_i
        B_i      = link_bps_i · baseRTT / 8
    U = max_i u_i
    U_smoothed = (1 - τ/T)·U_smoothed + (τ/T)·U,  τ/T = 0.2
    Per-RTT, ack-clocked update:
        W_new = W_ref / (U_smoothed / η) + W_AI,  η = 0.95, W_AI = 1

W_ref + incarnation are sender-side bookkeeping (not on the wire): we
snapshot the W in effect at each send into a per-seq map, and use the
W of the ACK that opens an update gate so we don't apply MD twice for
the same congestion epoch.

Runs over the same UDP+ACK harness as the DCTCP sender so the
comparison is apples-to-apples.
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
    SHIM_SIZE,
    AckPayload,
    DataPacket,
    DataPayload,
    IntHop,
    Shim,
)

log = logging.getLogger("hpcc_sender")

_TOS_ECT0 = 0x02  # we ride the same harness; ECT-on flow but DCTCP marks unused

DEFAULT_W_INIT = 8
DEFAULT_W_MIN = 1
DEFAULT_W_MAX = 64
DEFAULT_ETA = 0.99  # paper default is 0.95; bumped to 0.99 for our 10 Mbps
                    # BMv2 setup where saturating the link demands at least
                    # ~1 packet of standing queue, which already pushes u to
                    # ~0.2 + 1.0 = 1.2 before MD fires. With η=0.99 the
                    # equilibrium pulls W toward true link capacity.
DEFAULT_W_AI = 3.0  # plan default 1; raised because at 10 Mbps BDP=5 pkt
                    # the steady-state fixed point W* = W_AI / (1 - η/U_eq)
                    # with W_AI=1 lands at ~3 packets — far below BDP. With
                    # W_AI=3 the equilibrium sits around BDP, giving us high
                    # throughput without flooding the buffer.
DEFAULT_TAU_OVER_T = 0.05  # plan default 0.2; lowered because BMv2 bursty
                           # dequeue at low rates makes per-ACK tx_rate
                           # samples noisy, so we smooth over ~20 ACKs.
DEFAULT_PADDING = 1400
# Assume MTU-sized packets when converting qdepth (in packets) to bytes.
_QDEPTH_PKT_BYTES = 1500

_RTO_MIN_S = 0.020
_RTO_MAX_S = 1.000


def _now_us() -> int:
    return time.time_ns() // 1000


def _rto_seconds(base_rtt_s: float) -> float:
    return max(_RTO_MIN_S, min(_RTO_MAX_S, 3.0 * base_rtt_s))


class HpccSender:
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
        self.u_smoothed = 1.0  # start "fully loaded" so first update can grow
        self.padding = padding
        self.base_rtt_s = base_rtt_s
        self.rto_s = _rto_seconds(base_rtt_s)
        self.log_path = log_path

        self.sock = self._open_socket()

        # in_flight[seq] = (send_ts_us, w_ref_snapshot, incarnation_snapshot, rtx)
        self.next_seq = 0
        self.cum_acked_seq = -1
        self.in_flight: dict[int, tuple[int, float, int, int]] = {}
        self.lock = threading.Lock()

        # Per-(switch_id, egress_port) → (last_tstamp_us, last_tx_byte_count).
        self.hop_last: dict[tuple[int, int], tuple[int, int]] = {}

        # Per-RTT update gate: BOTH "≥ W acks since last" AND
        # "≥ baseRTT wallclock since last". Time bound prevents the gate
        # from firing arbitrarily often when W shrinks.
        self.last_update_seq = -1
        self.last_update_time = time.monotonic()

        # Event buffer; flushed at shutdown.
        self.events: list[tuple] = []
        self.stop = threading.Event()

    def _open_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _TOS_ECT0)
        s.bind(("0.0.0.0", self.ack_port))
        s.settimeout(0.05)
        return s

    def _log_event(self, event: str, seq: int, rtt_us: int = 0,
                   u: float = 0.0, hop_count: int = 0) -> None:
        self.events.append((
            _now_us(), event, seq, rtt_us, hop_count,
            round(self.w, 3),
            round(self.w_ref, 3),
            self.incarnation,
            round(u, 5),
            round(self.u_smoothed, 5),
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
            _, w_ref_at_send, inc_at_send, rtx = self.in_flight[seq]
            self.in_flight[seq] = (ts, w_ref_at_send, inc_at_send, rtx + 1)
            self._log_event("rtx", seq)
        else:
            self.in_flight[seq] = (ts, self.w_ref, self.incarnation, 0)
            self._log_event("send", seq)

    def _parse_ack(self, data: bytes) -> tuple[Shim, list[IntHop], AckPayload] | None:
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
        """Per-hop u_i = qdepth_bytes/B_i + txRate_i/link_bps_i, take max."""
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
            dt_us = hop.egress_tstamp_us - last_ts
            if dt_us <= 0:
                continue
            dbytes = hop.tx_byte_count - last_bytes
            if dbytes < 0:
                # Counter reset between runs; skip this sample.
                continue
            tx_rate_bps = (dbytes * 8.0 * 1_000_000.0) / dt_us
            b_bytes = hop.link_bps * self.base_rtt_s / 8.0
            if b_bytes <= 0:
                continue
            qdepth_bytes = hop.qdepth * _QDEPTH_PKT_BYTES
            u_i = qdepth_bytes / b_bytes + tx_rate_bps / hop.link_bps
            if u_i > u_max:
                u_max = u_i
        return u_max

    def _on_ack_locked(self, ack_seq: int, hops: list[IntHop],
                       rtt_us: int) -> None:
        if ack_seq > self.cum_acked_seq:
            for s in range(self.cum_acked_seq + 1, ack_seq + 1):
                self.in_flight.pop(s, None)
            self.cum_acked_seq = ack_seq

        u = self._compute_u(hops)
        if u > 0:
            self.u_smoothed = (
                (1.0 - self.tau_over_T) * self.u_smoothed
                + self.tau_over_T * u
            )

        self._log_event("ack", ack_seq, rtt_us, u, len(hops))
        self._maybe_update_window_locked()

    def _maybe_update_window_locked(self) -> None:
        # Gate by both window-of-ACKs AND wall-clock since last update,
        # so we never fire faster than once per baseRTT.
        now = time.monotonic()
        if (self.cum_acked_seq < self.last_update_seq + int(max(self.w_ref, 1))
                or now - self.last_update_time < self.base_rtt_s):
            return

        # Apply HPCC update formula. Bound U_smoothed to avoid pathological w.
        u_eff = max(self.u_smoothed, 1e-3)
        w_new = self.w_ref / (u_eff / self.eta) + self.w_ai
        w_new = max(self.w_min, min(self.w_max, w_new))

        # If we just shrank, advance incarnation and re-anchor W_ref.
        if w_new < self.w_ref:
            self.incarnation += 1
        self.w_ref = w_new
        self.w = w_new
        self.last_update_seq = self.cum_acked_seq
        self.last_update_time = now
        self._log_event("update", -1, u=u_eff)

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
                    continue  # late duplicate
                send_ts = meta[0] if meta else 0
                rtt_us = _now_us() - send_ts if send_ts else 0
                self._on_ack_locked(ack.ack_seq, hops, rtt_us)

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
                # Loss: halve W, bump incarnation, retransmit unacked.
                self.w_ref = max(self.w_min, self.w_ref / 2)
                self.w = self.w_ref
                self.incarnation += 1
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
                "w", "w_ref", "incarnation",
                "u", "u_smoothed",
                "cum_acked", "in_flight",
            ])
            w.writerows(self.events)


def measure_base_rtt(receiver_ip: str, data_port: int, ack_port: int,
                     n_probes: int = 10) -> float:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _TOS_ECT0)
    s.bind(("0.0.0.0", ack_port))
    s.settimeout(0.5)
    rtts: list[float] = []
    try:
        for i in range(n_probes):
            seq = 2_000_000 + i
            pkt = DataPacket(payload=DataPayload(seq=seq, send_ts_us=_now_us()))
            ts = _now_us()
            s.sendto(pkt.pack(), (receiver_ip, data_port))
            try:
                _data, _src = s.recvfrom(4096)
                rtts.append((_now_us() - ts) / 1_000_000)
            except socket.timeout:
                pass
            time.sleep(0.02)
    finally:
        s.close()
    if not rtts:
        return 0.005
    rtts.sort()
    return rtts[len(rtts) // 2]


def main() -> None:
    p = argparse.ArgumentParser(description="HPCC-style UDP sender")
    p.add_argument("receiver_ip")
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--log", default="/tmp/hpcc.csv")
    p.add_argument("--data-port", type=int, default=DATA_PORT)
    p.add_argument("--ack-port", type=int, default=ACK_PORT)
    p.add_argument("--w-init", type=int, default=DEFAULT_W_INIT)
    p.add_argument("--w-max", type=int, default=DEFAULT_W_MAX)
    p.add_argument("--eta", type=float, default=DEFAULT_ETA)
    p.add_argument("--w-ai", type=float, default=DEFAULT_W_AI)
    p.add_argument("--tau-over-t", type=float, default=DEFAULT_TAU_OVER_T)
    p.add_argument("--padding", type=int, default=DEFAULT_PADDING)
    p.add_argument("--base-rtt", type=float, default=0.0)
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

    sender = HpccSender(
        receiver_ip=args.receiver_ip,
        data_port=args.data_port,
        ack_port=args.ack_port,
        w_init=args.w_init,
        w_max=args.w_max,
        eta=args.eta,
        w_ai=args.w_ai,
        tau_over_T=args.tau_over_t,
        padding=args.padding,
        base_rtt_s=base_rtt,
        log_path=args.log,
    )
    summary = sender.run(args.duration, max_packets=args.max_packets)
    print(f"sent={summary['sent']} acked={summary['acked']} "
          f"final_w={summary['final_w']} u_smoothed={summary['final_u_smoothed']} "
          f"incarnations={summary['incarnation']} events={summary['events']} "
          f"log={args.log}")


if __name__ == "__main__":
    main()
