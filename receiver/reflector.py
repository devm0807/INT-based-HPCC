"""
UDP reflector — receives data packets on DATA_PORT, returns an ACK on
ACK_PORT.

Treats the SHIM + INT-hop stack as opaque bytes (per the plan: receiver
never parses INT). Mirrors the IPv4 ECN bits delivered as IP_TOS ancillary
data into the SHIM_FLAG_ECN_ECHO bit on the returning ACK so DCTCP-style
control loops on the sender can react.

Used by both the HPCC and DCTCP harnesses.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

from sender.packet_format import (
    ACK_PORT,
    DATA_PAYLOAD_SIZE,
    DATA_PORT,
    INT_HOP_SIZE,
    MAX_HOPS,
    SHIM_FLAG_ECN_ECHO,
    SHIM_SIZE,
    AckPayload,
    DataPayload,
    Shim,
)

log = logging.getLogger("reflector")

# IP_RECVTOS is Linux-specific; not all Pythons / platforms expose it.
_IP_RECVTOS = getattr(socket, "IP_RECVTOS", None)
_IP_TOS = getattr(socket, "IP_TOS", None)

# Ancillary buffer big enough for IP_TOS cmsg.
_ANCBUF = 64

# How long to block in recvmsg before checking stop_event.
_RECV_TIMEOUT_S = 0.25


def _now_us() -> int:
    return time.time_ns() // 1000


def _read_ecn(ancdata) -> int:
    """Extract the 2-bit ECN field from an IP_TOS cmsg, else 0."""
    if _IP_TOS is None:
        return 0
    for level, type_, data in ancdata:
        if level == socket.IPPROTO_IP and type_ == _IP_TOS and data:
            return data[0] & 0x03
    return 0


def _build_ack(payload: bytes, ecn_codepoint: int) -> bytes | None:
    """Parse just enough of the data payload to build the ACK; treat hops
    as opaque bytes. Returns None for malformed packets."""
    if len(payload) < SHIM_SIZE:
        return None

    shim = Shim.unpack(payload)
    if shim.hop_count > MAX_HOPS:
        return None

    hops_size = shim.hop_count * INT_HOP_SIZE
    data_off = SHIM_SIZE + hops_size
    if len(payload) < data_off + DATA_PAYLOAD_SIZE:
        return None

    hops_blob = payload[SHIM_SIZE:data_off]
    data = DataPayload.unpack(payload[data_off:data_off + DATA_PAYLOAD_SIZE])

    out_shim = Shim(
        flags=(SHIM_FLAG_ECN_ECHO if ecn_codepoint == 0x03 else 0),
        hop_count=shim.hop_count,
        max_hops=shim.max_hops,
        reserved=shim.reserved,
    )
    ack = AckPayload(ack_seq=data.seq, recv_ts_us=_now_us())
    return out_shim.pack() + hops_blob + ack.pack()


def run_reflector(
    bind_ip: str = "0.0.0.0",
    data_port: int = DATA_PORT,
    ack_port: int = ACK_PORT,
    stop_event: threading.Event | None = None,
    verbose: bool = False,
) -> None:
    if verbose:
        logging.basicConfig(level=logging.INFO)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if _IP_RECVTOS is not None:
        try:
            sock.setsockopt(socket.IPPROTO_IP, _IP_RECVTOS, 1)
        except OSError as e:
            log.warning("IP_RECVTOS not enabled, ECN echo disabled: %s", e)
    sock.bind((bind_ip, data_port))
    sock.settimeout(_RECV_TIMEOUT_S)

    log.info("reflector listening on %s:%d, ack -> :%d",
             bind_ip, data_port, ack_port)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                data, ancdata, _flags, src = sock.recvmsg(2048, _ANCBUF)
            except socket.timeout:
                continue

            ecn = _read_ecn(ancdata)
            ack = _build_ack(data, ecn)
            if ack is None:
                log.debug("dropped malformed packet, len=%d", len(data))
                continue
            sock.sendto(ack, (src[0], ack_port))
    finally:
        sock.close()


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="INT-CC UDP reflector")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--data-port", type=int, default=DATA_PORT)
    p.add_argument("--ack-port", type=int, default=ACK_PORT)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO)
    run_reflector(
        bind_ip=args.bind,
        data_port=args.data_port,
        ack_port=args.ack_port,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
