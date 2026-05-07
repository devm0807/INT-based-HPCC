"""End-to-end INT round-trip through the reflector on localhost.

Sends synthetic data packets (0-hop, 2-hop, MAX_HOPS-hop) through the
reflector and asserts shim + hops echo byte-for-byte and ACKs are well
formed. Also checks ECN echo on platforms where IP_TOS cmsgs are
delivered to UDP loopback receivers (Linux).
"""
from __future__ import annotations

import socket
import sys
import threading
import time

import pytest

from receiver.reflector import run_reflector
from sender.packet_format import (
    MAX_HOPS,
    AckPacket,
    DataPacket,
    DataPayload,
    IntHop,
    Shim,
)


# Unique high ports so a developer's running reflector on the spec
# defaults (50000/50001) doesn't collide with the test.
TEST_DATA_PORT = 56000
TEST_ACK_PORT = 56001


@pytest.fixture(scope="module")
def reflector():
    stop = threading.Event()
    t = threading.Thread(
        target=run_reflector,
        kwargs={
            "bind_ip": "127.0.0.1",
            "data_port": TEST_DATA_PORT,
            "ack_port": TEST_ACK_PORT,
            "stop_event": stop,
        },
        daemon=True,
    )
    t.start()
    time.sleep(0.2)  # give it time to bind
    yield
    stop.set()
    t.join(timeout=2)


def _client_sock():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", TEST_ACK_PORT))
    s.settimeout(2.0)
    return s


def _send_and_recv(s: socket.socket, pkt: DataPacket) -> AckPacket:
    s.sendto(pkt.pack(), ("127.0.0.1", TEST_DATA_PORT))
    raw, _src = s.recvfrom(4096)
    return AckPacket.unpack(raw)


def test_zero_hop_roundtrip(reflector):
    s = _client_sock()
    try:
        pkt = DataPacket(
            shim=Shim(),
            hops=[],
            payload=DataPayload(seq=42, send_ts_us=1234567),
        )
        ack = _send_and_recv(s, pkt)
        assert ack.shim.hop_count == 0
        assert ack.payload.ack_seq == 42
        assert not ack.shim.ecn_echo
    finally:
        s.close()


@pytest.mark.parametrize("n_hops", [1, 2, MAX_HOPS])
def test_n_hop_byte_exact_echo(reflector, n_hops):
    s = _client_sock()
    try:
        hops = [
            IntHop(switch_id=i, ingress_port=10 + i, egress_port=20 + i,
                   qdepth=100 * (i + 1), egress_tstamp_us=1_000_000 * (i + 1),
                   tx_byte_count=2_000_000 * (i + 1), link_bps=10_000_000)
            for i in range(n_hops)
        ]
        pkt = DataPacket(
            shim=Shim(),
            hops=hops,
            payload=DataPayload(seq=99, send_ts_us=98765),
        )
        ack = _send_and_recv(s, pkt)
        assert ack.shim.hop_count == n_hops
        assert ack.hops == hops, "INT hops must be echoed byte-for-byte"
        assert ack.payload.ack_seq == 99
        # recv_ts_us is set by the reflector and should be > the send_ts.
        assert ack.payload.recv_ts_us > pkt.payload.send_ts_us
    finally:
        s.close()


def test_oversized_hop_count_dropped(reflector):
    """A packet that claims more hops than it carries must be dropped."""
    s = _client_sock()
    try:
        # Hand-craft a malformed packet: shim says 3 hops but we only put 0.
        bad = Shim(hop_count=3).pack() + DataPayload(seq=1, send_ts_us=1).pack()
        s.sendto(bad, ("127.0.0.1", TEST_DATA_PORT))

        # Now send a valid packet to verify the reflector is still alive.
        good = DataPacket(
            shim=Shim(),
            payload=DataPayload(seq=7, send_ts_us=7),
        )
        ack = _send_and_recv(s, good)
        assert ack.payload.ack_seq == 7
    finally:
        s.close()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="IP_TOS ancillary data on UDP loopback only reliably works on Linux",
)
def test_ecn_echo(reflector):
    s = _client_sock()
    try:
        # CE codepoint in the low 2 bits of TOS.
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x03)
        pkt = DataPacket(payload=DataPayload(seq=1, send_ts_us=1))
        ack = _send_and_recv(s, pkt)
        assert ack.shim.ecn_echo, "expected ECN_ECHO bit set in shim.flags"
    finally:
        s.close()
