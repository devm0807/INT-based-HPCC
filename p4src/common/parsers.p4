/*
 * Shared parser + deparser for hpcc.p4 and dctcp.p4.
 *
 * Both programs use the same on-wire layout (see headers.p4); the parser
 * is identical. Each program then plugs in its own ingress/egress logic.
 *
 * Architecture: v1model (BMv2 simple_switch / simple_switch_grpc).
 */

#ifndef _COMMON_PARSERS_P4_
#define _COMMON_PARSERS_P4_

#include <core.p4>
#include <v1model.p4>
#include "headers.p4"

/* --- ingress parser -------------------------------------------------- */

parser CommonParser(packet_in pkt,
                    out headers_t hdr,
                    inout metadata_t meta,
                    inout standard_metadata_t std_meta) {

    state start { transition parse_ethernet; }

    state parse_ethernet {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.ethertype) {
            ETHERTYPE_IPV4: parse_ipv4;
            default: accept;
        }
    }

    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            IP_PROTO_UDP: parse_udp;
            default: accept;
        }
    }

    state parse_udp {
        pkt.extract(hdr.udp);
        /* Only parse the SHIM/INT/payload stack for our two ports. Other
         * UDP traffic is forwarded as-is. */
        transition select(hdr.udp.dst_port) {
            DATA_PORT: parse_shim_data;
            ACK_PORT:  parse_shim_ack;
            default:   accept;
        }
    }

    state parse_shim_data {
        pkt.extract(hdr.shim);
        meta.is_data = 1;
        meta.parsed_hop_count = hdr.shim.hop_count;
        transition parse_hops;
    }

    state parse_shim_ack {
        pkt.extract(hdr.shim);
        meta.is_ack = 1;
        meta.parsed_hop_count = hdr.shim.hop_count;
        transition parse_hops;
    }

    /* Variable-length INT stack. shim.hop_count tells us how many entries
     * to extract; cap at MAX_HOPS to keep the parser bounded. */
    state parse_hops {
        transition select(meta.parsed_hop_count) {
            0:       parse_payload;
            default: parse_one_hop;
        }
    }

    state parse_one_hop {
        pkt.extract(hdr.hops.next);
        meta.parsed_hop_count = meta.parsed_hop_count - 1;
        transition parse_hops;
    }

    state parse_payload {
        transition select(meta.is_data, meta.is_ack) {
            (1, 0): parse_data_payload;
            (0, 1): parse_ack_payload;
            default: accept;
        }
    }

    state parse_data_payload { pkt.extract(hdr.data); transition accept; }
    state parse_ack_payload  { pkt.extract(hdr.ack);  transition accept; }
}

/* --- IPv4 checksum verify / compute --------------------------------- */

control CommonVerifyChecksum(inout headers_t hdr, inout metadata_t meta) {
    apply {
        verify_checksum(
            hdr.ipv4.isValid(),
            { hdr.ipv4.version, hdr.ipv4.ihl, hdr.ipv4.dscp, hdr.ipv4.ecn,
              hdr.ipv4.total_len, hdr.ipv4.identification,
              hdr.ipv4.flags, hdr.ipv4.frag_offset,
              hdr.ipv4.ttl, hdr.ipv4.protocol,
              hdr.ipv4.src_addr, hdr.ipv4.dst_addr },
            hdr.ipv4.hdr_checksum,
            HashAlgorithm.csum16);
    }
}

control CommonComputeChecksum(inout headers_t hdr, inout metadata_t meta) {
    apply {
        update_checksum(
            hdr.ipv4.isValid(),
            { hdr.ipv4.version, hdr.ipv4.ihl, hdr.ipv4.dscp, hdr.ipv4.ecn,
              hdr.ipv4.total_len, hdr.ipv4.identification,
              hdr.ipv4.flags, hdr.ipv4.frag_offset,
              hdr.ipv4.ttl, hdr.ipv4.protocol,
              hdr.ipv4.src_addr, hdr.ipv4.dst_addr },
            hdr.ipv4.hdr_checksum,
            HashAlgorithm.csum16);
        /* UDP checksum left at whatever the sender supplied (typically 0,
         * which is legal in IPv4). hpcc.p4 may zero it explicitly after
         * pushing an INT entry, since it grows the UDP payload. */
    }
}

/* --- deparser -------------------------------------------------------- */

control CommonDeparser(packet_out pkt, in headers_t hdr) {
    apply {
        pkt.emit(hdr.ethernet);
        pkt.emit(hdr.ipv4);
        pkt.emit(hdr.udp);
        pkt.emit(hdr.shim);
        pkt.emit(hdr.hops);     /* emits only the .isValid() entries */
        pkt.emit(hdr.data);
        pkt.emit(hdr.ack);
    }
}

#endif /* _COMMON_PARSERS_P4_ */
