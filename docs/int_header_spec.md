# INT-CC wire format

This is the bit-exact protocol every other piece of the project speaks.
The two source-of-truth files are:

- [p4src/common/headers.p4](../p4src/common/headers.p4) — P4 data plane.
- [sender/packet_format.py](../sender/packet_format.py) — host-side codecs.

Both must be edited together. [tests/test_packet_format.py](../tests/test_packet_format.py)
asserts the field widths and pins a couple of byte-exact reference vectors,
so any drift between the P4 layout and the Python layout breaks `pytest`.

## Stack

All multibyte integers are network byte order (big-endian).

```
Ethernet (14 B)
  IPv4   (20 B)         protocol = UDP (17). ECN bits used by DCTCP.
    UDP  (8 B)          dst_port = DATA_PORT (50000) or ACK_PORT (50001)
      SHIM (4 B)
      INT_HOP × N (26 B each, N = 0..MAX_HOPS = 4)
      payload (16 B)    DATA payload if dst_port=50000, ACK payload if 50001
```

Total UDP payload size:

| kind | hops | bytes |
|------|------|-------|
| data | 0    | 20    |
| data | 4    | 124   |
| ack  | 0    | 20    |
| ack  | 4    | 124   |

## Discrimination

The P4 parser switches on `udp.dst_port` and only descends into the
SHIM/INT/payload stack for our two ports; all other UDP traffic is
forwarded as-is. Switches insert a new INT_HOP entry only when
`udp.dst_port == DATA_PORT`. ACKs are reflected verbatim by the receiver;
switches do not modify ACKs.

## SHIM (4 B)

| offset | width | name        | meaning |
|--------|-------|-------------|---------|
| 0      | u8    | flags       | bit 0 = ECN_ECHO (set by receiver in ACK if data had ECN-CE); bits 1-7 reserved (must be 0) |
| 1      | u8    | hop_count   | number of INT_HOP entries that follow, 0..MAX_HOPS |
| 2      | u8    | max_hops    | echoed parser bound, set to MAX_HOPS by sender |
| 3      | u8    | reserved    | must be 0 |

## INT_HOP (26 B)

Pushed by switches at egress (HPCC mode only). Field layout:

| offset | width | name              | source / units |
|--------|-------|-------------------|----------------|
| 0      | u16   | switch_id         | constant per-switch (config) |
| 2      | u16   | ingress_port      | `standard_metadata.ingress_port` |
| 4      | u16   | egress_port       | `standard_metadata.egress_port` |
| 6      | u32   | qdepth            | `standard_metadata.enq_qdepth`, BMv2 packet units |
| 10     | u48   | egress_tstamp_us  | `standard_metadata.egress_global_timestamp` (µs in BMv2) |
| 16     | u48   | tx_byte_count     | per-egress-port byte counter (P4 register, monotonic) |
| 22     | u32   | link_bps          | per-port link capacity (P4 table, set by control plane) |

Total: 26 B per hop. With MAX_HOPS = 4 the worst-case INT block is 104 B,
which still fits comfortably under a 1500 B MTU.

## DATA payload (16 B, dst_port = 50000)

| offset | width | name        |
|--------|-------|-------------|
| 0      | u32   | seq         |
| 4      | u64   | send_ts_us  |
| 12     | u32   | reserved    |

## ACK payload (16 B, dst_port = 50001)

| offset | width | name        |
|--------|-------|-------------|
| 0      | u32   | ack_seq     |
| 4      | u64   | recv_ts_us  |
| 12     | u32   | reserved    |

## Notes

- **UDP checksum** is set to 0 by the sender and never recomputed by the
  switch. UDP-over-IPv4 allows checksum=0; the Linux kernel accepts it.
- **HPCC bookkeeping** (`W_ref`, `incarnation`) is kept in sender memory
  keyed by `seq`, deliberately *not* on the wire — keeps the protocol
  algorithm-agnostic.
- **DCTCP** doesn't push INT entries. The data plane marks `ipv4.ecn = CE`
  on egress when the queue exceeds `K`; the receiver mirrors that bit into
  `shim.flags.ECN_ECHO` on the ACK.
