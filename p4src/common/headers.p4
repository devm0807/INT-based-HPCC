/*
 * INT-CC project — frozen wire format.
 *
 * This file is the single source of truth for the on-wire layout that the
 * P4 data plane (hpcc.p4 / dctcp.p4) and the Python sender / receiver
 * (sender/packet_format.py) both speak. Keep them in lockstep — any field
 * change here MUST land in packet_format.py in the same commit and pass
 * tests/test_packet_format.py.
 *
 * Layout, top to bottom on the wire:
 *
 *   Ethernet
 *   IPv4              (ECN bits used by DCTCP; protocol = UDP = 17 always)
 *   UDP               (dst_port = 50000 for data, 50001 for ACK)
 *   shim_t   (4 B)
 *   int_hop_t × N    (26 B each, N in 0..MAX_HOPS)
 *   data_payload_t OR ack_payload_t (16 B)
 *
 * Discrimination: HPCC switches insert an int_hop entry only when
 * udp.dst_port == DATA_PORT (50000). Everything else (ACKs from receiver,
 * unrelated UDP traffic) is forwarded without modification. The receiver
 * reflects the SHIM + int_hop stack verbatim into ACKs; switches do not
 * modify ACKs.
 *
 * For DCTCP runs the data plane never inserts INT entries (hop_count
 * stays 0); instead, switches mark ipv4.ecn = CE on egress when the queue
 * is over threshold, and the receiver echoes that bit back via the SHIM
 * flags ECN_ECHO bit on the ACK.
 */

#ifndef _COMMON_HEADERS_P4_
#define _COMMON_HEADERS_P4_

/* --- constants ------------------------------------------------------- */

const bit<16> ETHERTYPE_IPV4 = 0x0800;
const bit<8>  IP_PROTO_UDP   = 17;

const bit<16> DATA_PORT      = 50000;   /* sender → receiver */
const bit<16> ACK_PORT       = 50001;   /* receiver → sender */

/* shim.flags bits (LSB first) */
const bit<8>  SHIM_FLAG_ECN_ECHO = 0x01;
const bit<8>  SHIM_FLAG_RESERVED = 0xFE;

/* INT bound — fixed at compile time so the parser unrolls cleanly.
 * Topology has 2 BMv2 switches in series, but we leave headroom. */
#define MAX_HOPS 4

/* IPv4 ECN codepoints (ipv4.ecn is 2 bits in the ToS byte). */
const bit<2> ECN_NOT_ECT = 0;
const bit<2> ECN_ECT_0   = 2;
const bit<2> ECN_ECT_1   = 1;
const bit<2> ECN_CE      = 3;

/* --- standard headers ------------------------------------------------ */

header ethernet_t {
    bit<48> dst_mac;
    bit<48> src_mac;
    bit<16> ethertype;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<6>  dscp;
    bit<2>  ecn;
    bit<16> total_len;
    bit<16> identification;
    bit<3>  flags;
    bit<13> frag_offset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdr_checksum;
    bit<32> src_addr;
    bit<32> dst_addr;
}

header udp_t {
    bit<16> src_port;
    bit<16> dst_port;
    bit<16> length;       /* udp header (8 B) + udp payload */
    bit<16> checksum;     /* set to 0 — UDP checksum optional in IPv4 */
}

/* --- INT-CC custom headers ------------------------------------------- */

/* SHIM: always present in our flows, constant 4 B. */
header shim_t {
    bit<8> flags;         /* bit0 = ECN_ECHO; other bits reserved */
    bit<8> hop_count;     /* number of int_hop_t entries that follow (0..MAX_HOPS) */
    bit<8> max_hops;      /* echoed parser bound, set to MAX_HOPS by sender */
    bit<8> reserved;
}

/* INT_HOP: 26 B per entry. Stack of up to MAX_HOPS, count carried in shim.hop_count. */
header int_hop_t {
    bit<16> switch_id;
    bit<16> ingress_port;
    bit<16> egress_port;
    bit<32> qdepth;            /* BMv2 packet units (standard_metadata.enq_qdepth) */
    bit<48> egress_tstamp_us;  /* standard_metadata.egress_global_timestamp (us in BMv2) */
    bit<48> tx_byte_count;     /* cumulative bytes sent on this egress port (per-port register) */
    bit<32> link_bps;          /* link capacity, looked up from per-port table */
}

/* App payloads — chosen to be the same size (16 B) for symmetry. */
header data_payload_t {
    bit<32> seq;
    bit<64> send_ts_us;
    bit<32> reserved;
}

header ack_payload_t {
    bit<32> ack_seq;
    bit<64> recv_ts_us;
    bit<32> reserved;
}

/* --- aggregate ------------------------------------------------------- */

struct headers_t {
    ethernet_t       ethernet;
    ipv4_t           ipv4;
    udp_t            udp;
    shim_t           shim;
    int_hop_t[MAX_HOPS] hops;
    data_payload_t   data;
    ack_payload_t    ack;
}

/* User metadata carried across pipeline stages. Filled in by the parser
 * (e.g. caching shim.hop_count for use in egress without re-reading). */
struct metadata_t {
    bit<8>  parsed_hop_count;
    bit<1>  is_data;     /* udp.dst_port == DATA_PORT */
    bit<1>  is_ack;      /* udp.dst_port == ACK_PORT  */
}

#endif /* _COMMON_HEADERS_P4_ */
