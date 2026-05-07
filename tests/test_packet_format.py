"""Round-trip tests for sender/packet_format.py.

These pin the on-wire layout: if anyone changes a field width or order in
either headers.p4 or packet_format.py without updating the other, the
fixed-size assertions or the byte-exact reference vectors below will trip.
"""

import pytest

from sender.packet_format import (
    ACK_PAYLOAD_SIZE,
    ACK_PORT,
    AckPacket,
    AckPayload,
    DATA_PAYLOAD_SIZE,
    DATA_PORT,
    DataPacket,
    DataPayload,
    INT_HOP_SIZE,
    IntHop,
    MAX_HOPS,
    SHIM_FLAG_ECN_ECHO,
    SHIM_SIZE,
    Shim,
    total_size,
)


# --- fixed sizes ----------------------------------------------------------

def test_constant_sizes():
    assert SHIM_SIZE == 4
    assert INT_HOP_SIZE == 26
    assert DATA_PAYLOAD_SIZE == 16
    assert ACK_PAYLOAD_SIZE == 16
    assert MAX_HOPS == 4
    assert DATA_PORT == 50000
    assert ACK_PORT == 50001


def test_pack_sizes():
    assert len(Shim().pack()) == SHIM_SIZE
    assert len(IntHop(1, 2, 3, 4, 5, 6, 7).pack()) == INT_HOP_SIZE
    assert len(DataPayload(seq=1, send_ts_us=2).pack()) == DATA_PAYLOAD_SIZE
    assert len(AckPayload(ack_seq=1, recv_ts_us=2).pack()) == ACK_PAYLOAD_SIZE


@pytest.mark.parametrize("n_hops", [0, 1, 2, MAX_HOPS])
def test_total_size(n_hops):
    assert total_size(n_hops, "data") == SHIM_SIZE + n_hops * INT_HOP_SIZE + DATA_PAYLOAD_SIZE
    assert total_size(n_hops, "ack")  == SHIM_SIZE + n_hops * INT_HOP_SIZE + ACK_PAYLOAD_SIZE


# --- shim --------------------------------------------------------------

def test_shim_roundtrip_default():
    s = Shim()
    s2 = Shim.unpack(s.pack())
    assert s == s2


def test_shim_ecn_bit():
    s = Shim()
    assert not s.ecn_echo
    s.set_ecn_echo(True)
    assert s.ecn_echo
    assert s.flags == SHIM_FLAG_ECN_ECHO
    s.set_ecn_echo(False)
    assert not s.ecn_echo
    assert s.flags == 0


def test_shim_byte_layout_known():
    """Pin shim byte order: flags, hop_count, max_hops, reserved."""
    s = Shim(flags=0x01, hop_count=2, max_hops=4, reserved=0xAB)
    assert s.pack() == bytes([0x01, 0x02, 0x04, 0xAB])


# --- int_hop -----------------------------------------------------------

def test_int_hop_roundtrip_simple():
    h = IntHop(switch_id=1, ingress_port=2, egress_port=3,
               qdepth=42, egress_tstamp_us=100_000, tx_byte_count=200_000,
               link_bps=10_000_000)
    assert IntHop.unpack(h.pack()) == h


def test_int_hop_roundtrip_max_values():
    h = IntHop(
        switch_id=0xFFFF,
        ingress_port=0xFFFF,
        egress_port=0xFFFF,
        qdepth=0xFFFFFFFF,
        egress_tstamp_us=(1 << 48) - 1,
        tx_byte_count=(1 << 48) - 1,
        link_bps=0xFFFFFFFF,
    )
    assert IntHop.unpack(h.pack()) == h


def test_int_hop_byte_layout_known():
    """Pin field order: switch_id, ingress, egress, qdepth, ts48, tx48, link_bps."""
    h = IntHop(
        switch_id=0x1122,
        ingress_port=0x3344,
        egress_port=0x5566,
        qdepth=0x778899AA,
        egress_tstamp_us=0xBBCCDDEEFF00,
        tx_byte_count=0x1122334455_66,
        link_bps=0xDEADBEEF,
    )
    expected = (
        b"\x11\x22"                          # switch_id
        b"\x33\x44"                          # ingress_port
        b"\x55\x66"                          # egress_port
        b"\x77\x88\x99\xAA"                  # qdepth
        b"\xBB\xCC\xDD\xEE\xFF\x00"          # egress_tstamp_us (u48)
        b"\x11\x22\x33\x44\x55\x66"          # tx_byte_count (u48)
        b"\xDE\xAD\xBE\xEF"                  # link_bps
    )
    assert h.pack() == expected


def test_int_hop_u48_overflow_rejected():
    h = IntHop(0, 0, 0, 0, 1 << 48, 0, 0)
    with pytest.raises(ValueError):
        h.pack()


# --- payloads ---------------------------------------------------------

def test_data_payload_roundtrip():
    p = DataPayload(seq=42, send_ts_us=1_700_000_000_000_000, reserved=0xC0FFEE)
    assert DataPayload.unpack(p.pack()) == p


def test_ack_payload_roundtrip():
    p = AckPayload(ack_seq=42, recv_ts_us=1_700_000_000_000_005, reserved=0xC0FFEE)
    assert AckPayload.unpack(p.pack()) == p


# --- composite packets -----------------------------------------------

@pytest.mark.parametrize("n_hops", [0, 1, 2, MAX_HOPS])
def test_data_packet_roundtrip(n_hops):
    hops = [
        IntHop(switch_id=i, ingress_port=10 + i, egress_port=20 + i,
               qdepth=100 * i, egress_tstamp_us=1_000_000 + i,
               tx_byte_count=2_000_000 + i, link_bps=10_000_000)
        for i in range(n_hops)
    ]
    pkt = DataPacket(
        shim=Shim(flags=0),
        hops=hops,
        payload=DataPayload(seq=99, send_ts_us=1_700_000_000_000_000),
    )
    raw = pkt.pack()
    assert len(raw) == total_size(n_hops, "data")

    pkt2 = DataPacket.unpack(raw)
    assert pkt2.shim.hop_count == n_hops
    assert pkt2.shim.max_hops == MAX_HOPS
    assert pkt2.hops == hops
    assert pkt2.payload == pkt.payload


@pytest.mark.parametrize("n_hops", [0, 1, MAX_HOPS])
@pytest.mark.parametrize("ecn", [False, True])
def test_ack_packet_roundtrip(n_hops, ecn):
    hops = [
        IntHop(switch_id=i, ingress_port=10 + i, egress_port=20 + i,
               qdepth=50 * i, egress_tstamp_us=500_000 + i,
               tx_byte_count=1_500_000 + i, link_bps=10_000_000)
        for i in range(n_hops)
    ]
    shim = Shim()
    shim.set_ecn_echo(ecn)
    pkt = AckPacket(
        shim=shim, hops=hops,
        payload=AckPayload(ack_seq=99, recv_ts_us=1_700_000_000_000_010),
    )
    raw = pkt.pack()
    assert len(raw) == total_size(n_hops, "ack")

    pkt2 = AckPacket.unpack(raw)
    assert pkt2.shim.ecn_echo is ecn
    assert pkt2.hops == hops
    assert pkt2.payload == pkt.payload


def test_data_packet_too_many_hops_rejected():
    pkt = DataPacket(hops=[IntHop(0, 0, 0, 0, 0, 0, 0)] * (MAX_HOPS + 1),
                     payload=DataPayload(seq=0, send_ts_us=0))
    with pytest.raises(ValueError):
        pkt.pack()
