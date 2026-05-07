"""
Open-loop UDP sender — Week-1 gate harness.

Sends fixed-rate UDP packets to a reflector (no congestion control),
listens for ACKs on the same socket, and logs per-packet
(seq, send_ts_us, ack_ts_us, rtt_us, hop_count, ecn_echo) to CSV.

This is the smoke harness shared between the HPCC and DCTCP development
paths; the real senders will replace the fixed-rate loop with their
window controllers but keep the same socket / ACK-parsing skeleton.

Run:
    python -m receiver.reflector &
    python -m sender.open_loop 10.0.0.2 --rate-pps 200 --duration 10 \\
        --log /tmp/openloop.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import socket
import threading
import time

from sender.packet_format import (
    ACK_PORT,
    ACK_PAYLOAD_SIZE,
    DATA_PORT,
    INT_HOP_SIZE,
    SHIM_FLAG_ECN_ECHO,
    SHIM_SIZE,
    AckPayload,
    DataPacket,
    DataPayload,
    Shim,
)

log = logging.getLogger("open_loop")

# ECT(0) — opt this flow into ECN so a DCTCP switch will mark CE under load.
_TOS_ECT0 = 0x02


def _now_us() -> int:
    return time.time_ns() // 1000


def _open_socket(ack_port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _TOS_ECT0)
    s.bind(("0.0.0.0", ack_port))
    s.settimeout(0.25)
    return s


def _ack_listener(
    sock: socket.socket,
    pending: dict[int, int],
    pending_lock: threading.Lock,
    csv_writer,
    csv_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            data, _src = sock.recvfrom(4096)
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
        recv_ts = _now_us()

        with pending_lock:
            send_ts = pending.pop(ack.ack_seq, None)
        if send_ts is None:
            continue  # duplicate or reordered late ACK

        ecn_echo = 1 if shim.flags & SHIM_FLAG_ECN_ECHO else 0
        with csv_lock:
            csv_writer.writerow([
                ack.ack_seq,
                send_ts,
                recv_ts,
                recv_ts - send_ts,
                shim.hop_count,
                ecn_echo,
            ])


def run_open_loop(
    receiver_ip: str,
    rate_pps: int,
    duration_s: float,
    log_path: str,
    data_port: int = DATA_PORT,
    ack_port: int = ACK_PORT,
) -> dict:
    """Send at fixed rate for `duration_s`. Returns summary stats."""
    sock = _open_socket(ack_port)
    pending: dict[int, int] = {}
    pending_lock = threading.Lock()
    csv_lock = threading.Lock()
    stop = threading.Event()

    out = open(log_path, "w", newline="")
    w = csv.writer(out)
    w.writerow(["seq", "send_ts_us", "ack_ts_us", "rtt_us", "hop_count", "ecn_echo"])

    rx = threading.Thread(
        target=_ack_listener,
        args=(sock, pending, pending_lock, w, csv_lock, stop),
        daemon=True,
    )
    rx.start()

    interval = 1.0 / rate_pps
    end = time.time() + duration_s
    next_send = time.time()
    seq = 0

    log.info("sending to %s:%d at %d pps for %.1fs",
             receiver_ip, data_port, rate_pps, duration_s)

    try:
        while time.time() < end:
            ts = _now_us()
            pkt = DataPacket(payload=DataPayload(seq=seq, send_ts_us=ts))
            with pending_lock:
                pending[seq] = ts
            sock.sendto(pkt.pack(), (receiver_ip, data_port))
            seq += 1

            next_send += interval
            sleep_for = next_send - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
    finally:
        # Drain late ACKs before shutdown.
        time.sleep(0.5)
        stop.set()
        rx.join(timeout=1.0)
        sock.close()
        out.close()

    return {"sent": seq, "unacked": len(pending)}


def main() -> None:
    p = argparse.ArgumentParser(description="Open-loop UDP sender for INT-CC week-1 gate")
    p.add_argument("receiver_ip")
    p.add_argument("--rate-pps", type=int, default=100)
    p.add_argument("--duration", type=float, default=10.0)
    p.add_argument("--log", default="/tmp/openloop.csv")
    p.add_argument("--data-port", type=int, default=DATA_PORT)
    p.add_argument("--ack-port", type=int, default=ACK_PORT)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    summary = run_open_loop(
        receiver_ip=args.receiver_ip,
        rate_pps=args.rate_pps,
        duration_s=args.duration,
        log_path=args.log,
        data_port=args.data_port,
        ack_port=args.ack_port,
    )
    print(f"sent={summary['sent']} unacked={summary['unacked']} log={args.log}")


if __name__ == "__main__":
    main()
