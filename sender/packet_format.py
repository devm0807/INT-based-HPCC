"""
Byte-exact codecs for the INT-CC wire format.

Mirrors p4src/common/headers.p4. The two files MUST be edited together;
tests/test_packet_format.py round-trips every struct and checks the
fixed sizes.

Layout summary (network byte order throughout):

    SHIM (4 B)
        flags : u8         bit 0 = ECN_ECHO, bits 1-7 reserved
        hop_count : u8     0..MAX_HOPS
        max_hops : u8      = MAX_HOPS
        reserved : u8

    INT_HOP (26 B)
        switch_id : u16
        ingress_port : u16
        egress_port : u16
        qdepth : u32
        egress_tstamp_us : u48
        tx_byte_count : u48
        link_bps : u32

    DATA payload (16 B)
        seq : u32
        send_ts_us : u64
        reserved : u32

    ACK payload (16 B)
        ack_seq : u32
        recv_ts_us : u64
        reserved : u32
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# --- protocol constants ----------------------------------------------------

DATA_PORT = 50000  # sender -> receiver
ACK_PORT = 50001   # receiver -> sender

MAX_HOPS = 4

SHIM_FLAG_ECN_ECHO = 0x01

# Sizes in bytes — kept here so callers don't reach into struct internals.
SHIM_SIZE = 4
INT_HOP_SIZE = 26
DATA_PAYLOAD_SIZE = 16
ACK_PAYLOAD_SIZE = 16

# --- internal helpers ------------------------------------------------------

def _pack_u48(v: int) -> bytes:
    if not 0 <= v < (1 << 48):
        raise ValueError(f"u48 out of range: {v}")
    return v.to_bytes(6, "big")


def _unpack_u48(buf: bytes) -> int:
    if len(buf) != 6:
        raise ValueError(f"u48 needs 6 bytes, got {len(buf)}")
    return int.from_bytes(buf, "big")


# --- SHIM ------------------------------------------------------------------

_SHIM_FMT = "!BBBB"  # flags, hop_count, max_hops, reserved
assert struct.calcsize(_SHIM_FMT) == SHIM_SIZE


@dataclass
class Shim:
    flags: int = 0
    hop_count: int = 0
    max_hops: int = MAX_HOPS
    reserved: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            _SHIM_FMT, self.flags, self.hop_count, self.max_hops, self.reserved
        )

    @classmethod
    def unpack(cls, buf: bytes) -> Shim:
        flags, hop_count, max_hops, reserved = struct.unpack(
            _SHIM_FMT, buf[:SHIM_SIZE]
        )
        return cls(flags=flags, hop_count=hop_count, max_hops=max_hops,
                   reserved=reserved)

    @property
    def ecn_echo(self) -> bool:
        return bool(self.flags & SHIM_FLAG_ECN_ECHO)

    def set_ecn_echo(self, on: bool) -> None:
        if on:
            self.flags |= SHIM_FLAG_ECN_ECHO
        else:
            self.flags &= ~SHIM_FLAG_ECN_ECHO


# --- INT_HOP ---------------------------------------------------------------

# struct layout: HHH I 6s 6s I  -> 2+2+2 + 4 + 6 + 6 + 4 = 26 bytes
_INT_HOP_FMT = "!HHHI6s6sI"
assert struct.calcsize(_INT_HOP_FMT) == INT_HOP_SIZE


@dataclass
class IntHop:
    switch_id: int
    ingress_port: int
    egress_port: int
    qdepth: int
    egress_tstamp_us: int
    tx_byte_count: int
    link_bps: int

    def pack(self) -> bytes:
        return struct.pack(
            _INT_HOP_FMT,
            self.switch_id,
            self.ingress_port,
            self.egress_port,
            self.qdepth,
            _pack_u48(self.egress_tstamp_us),
            _pack_u48(self.tx_byte_count),
            self.link_bps,
        )

    @classmethod
    def unpack(cls, buf: bytes) -> IntHop:
        (sw, ingress, egress, qdepth, ts_b, tx_b, link_bps) = struct.unpack(
            _INT_HOP_FMT, buf[:INT_HOP_SIZE]
        )
        return cls(
            switch_id=sw,
            ingress_port=ingress,
            egress_port=egress,
            qdepth=qdepth,
            egress_tstamp_us=_unpack_u48(ts_b),
            tx_byte_count=_unpack_u48(tx_b),
            link_bps=link_bps,
        )


# --- payloads --------------------------------------------------------------

_DATA_FMT = "!IQI"   # seq, send_ts_us, reserved
_ACK_FMT = "!IQI"    # ack_seq, recv_ts_us, reserved
assert struct.calcsize(_DATA_FMT) == DATA_PAYLOAD_SIZE
assert struct.calcsize(_ACK_FMT) == ACK_PAYLOAD_SIZE


@dataclass
class DataPayload:
    seq: int
    send_ts_us: int
    reserved: int = 0

    def pack(self) -> bytes:
        return struct.pack(_DATA_FMT, self.seq, self.send_ts_us, self.reserved)

    @classmethod
    def unpack(cls, buf: bytes) -> DataPayload:
        seq, ts, reserved = struct.unpack(_DATA_FMT, buf[:DATA_PAYLOAD_SIZE])
        return cls(seq=seq, send_ts_us=ts, reserved=reserved)


@dataclass
class AckPayload:
    ack_seq: int
    recv_ts_us: int
    reserved: int = 0

    def pack(self) -> bytes:
        return struct.pack(_ACK_FMT, self.ack_seq, self.recv_ts_us, self.reserved)

    @classmethod
    def unpack(cls, buf: bytes) -> AckPayload:
        seq, ts, reserved = struct.unpack(_ACK_FMT, buf[:ACK_PAYLOAD_SIZE])
        return cls(ack_seq=seq, recv_ts_us=ts, reserved=reserved)


# --- composite (full UDP payload) -----------------------------------------

@dataclass
class DataPacket:
    """Full UDP payload for a sender->receiver data packet."""
    shim: Shim = field(default_factory=Shim)
    hops: list[IntHop] = field(default_factory=list)
    payload: DataPayload = field(default_factory=lambda: DataPayload(seq=0, send_ts_us=0))

    def pack(self) -> bytes:
        if len(self.hops) > MAX_HOPS:
            raise ValueError(f"too many hops: {len(self.hops)} > {MAX_HOPS}")
        # Always reflect the actual count; callers shouldn't have to set it.
        self.shim.hop_count = len(self.hops)
        self.shim.max_hops = MAX_HOPS
        return b"".join(
            [self.shim.pack(), *[h.pack() for h in self.hops], self.payload.pack()]
        )

    @classmethod
    def unpack(cls, buf: bytes) -> DataPacket:
        shim = Shim.unpack(buf)
        offset = SHIM_SIZE
        hops: list[IntHop] = []
        for _ in range(shim.hop_count):
            hops.append(IntHop.unpack(buf[offset:offset + INT_HOP_SIZE]))
            offset += INT_HOP_SIZE
        payload = DataPayload.unpack(buf[offset:offset + DATA_PAYLOAD_SIZE])
        return cls(shim=shim, hops=hops, payload=payload)


@dataclass
class AckPacket:
    """Full UDP payload for a receiver->sender ACK."""
    shim: Shim = field(default_factory=Shim)
    hops: list[IntHop] = field(default_factory=list)
    payload: AckPayload = field(default_factory=lambda: AckPayload(ack_seq=0, recv_ts_us=0))

    def pack(self) -> bytes:
        if len(self.hops) > MAX_HOPS:
            raise ValueError(f"too many hops: {len(self.hops)} > {MAX_HOPS}")
        self.shim.hop_count = len(self.hops)
        self.shim.max_hops = MAX_HOPS
        return b"".join(
            [self.shim.pack(), *[h.pack() for h in self.hops], self.payload.pack()]
        )

    @classmethod
    def unpack(cls, buf: bytes) -> AckPacket:
        shim = Shim.unpack(buf)
        offset = SHIM_SIZE
        hops: list[IntHop] = []
        for _ in range(shim.hop_count):
            hops.append(IntHop.unpack(buf[offset:offset + INT_HOP_SIZE]))
            offset += INT_HOP_SIZE
        payload = AckPayload.unpack(buf[offset:offset + ACK_PAYLOAD_SIZE])
        return cls(shim=shim, hops=hops, payload=payload)


def total_size(n_hops: int, kind: str) -> int:
    """Total UDP-payload byte count for a packet with n_hops INT entries.

    Useful for sizing send buffers and verifying packet length on the wire.
    `kind` must be 'data' or 'ack'.
    """
    if not 0 <= n_hops <= MAX_HOPS:
        raise ValueError(f"hop count out of range: {n_hops}")
    payload_size = DATA_PAYLOAD_SIZE if kind == "data" else ACK_PAYLOAD_SIZE
    if kind not in ("data", "ack"):
        raise ValueError(f"kind must be 'data' or 'ack', got {kind!r}")
    return SHIM_SIZE + n_hops * INT_HOP_SIZE + payload_size
