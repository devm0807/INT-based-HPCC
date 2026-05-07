/*
 * dctcp.p4 — DCTCP-style ECN marking baseline.
 *
 * Pipeline:
 *   ingress: IPv4 LPM forwarding (egress port + new dst MAC + TTL--).
 *   egress:  if data packet and ipv4.ecn == ECT, mark CE when
 *            standard_metadata.enq_qdepth > K. K is a control-plane-writable
 *            register (per the plan: K = 5 packets at 10 Mbps).
 *
 * INT is never inserted; shim.hop_count stays 0 throughout. The receiver
 * echoes the CE bit back to the sender via shim.flags ECN_ECHO on the ACK.
 *
 * Architecture: v1model (BMv2 simple_switch / simple_switch_grpc).
 */

#include "common/parsers.p4"

/* --- ingress -------------------------------------------------------- */

control DctcpIngress(inout headers_t hdr,
                     inout metadata_t meta,
                     inout standard_metadata_t std_meta) {

    action drop() { mark_to_drop(std_meta); }

    action ipv4_forward(bit<48> dst_mac, bit<9> port) {
        std_meta.egress_spec = port;
        hdr.ethernet.src_mac = hdr.ethernet.dst_mac;
        hdr.ethernet.dst_mac = dst_mac;
        hdr.ipv4.ttl         = hdr.ipv4.ttl - 1;
    }

    table ipv4_lpm {
        key            = { hdr.ipv4.dst_addr: lpm; }
        actions        = { ipv4_forward; drop; NoAction; }
        size           = 1024;
        default_action = drop();
    }

    apply {
        if (hdr.ipv4.isValid() && hdr.ipv4.ttl > 1) {
            ipv4_lpm.apply();
        } else if (hdr.ipv4.isValid()) {
            drop();
        }
    }
}

/* --- egress: ECN marking ------------------------------------------- */

control DctcpEgress(inout headers_t hdr,
                    inout metadata_t meta,
                    inout standard_metadata_t std_meta) {

    /* Instantaneous queue threshold, in BMv2 packet units. Controller
     * writes at startup (load_tables.py). */
    register<bit<32>>(1) ecn_threshold;

    /* Coarse counters for evaluation. Control plane reads via thrift. */
    register<bit<64>>(1) data_pkt_count;
    register<bit<64>>(1) marked_pkt_count;

    apply {
        if (!hdr.ipv4.isValid() || meta.is_data != 1) {
            return;
        }

        bit<64> total;
        data_pkt_count.read(total, 0);
        data_pkt_count.write(0, total + 1);

        /* Only mark packets the sender has opted into ECN for. */
        if (hdr.ipv4.ecn == ECN_NOT_ECT) {
            return;
        }

        bit<32> K;
        ecn_threshold.read(K, 0);

        if ((bit<32>)std_meta.enq_qdepth > K) {
            hdr.ipv4.ecn = ECN_CE;
            bit<64> marks;
            marked_pkt_count.read(marks, 0);
            marked_pkt_count.write(0, marks + 1);
        }
    }
}

/* --- pipeline ------------------------------------------------------- */

V1Switch(
    CommonParser(),
    CommonVerifyChecksum(),
    DctcpIngress(),
    DctcpEgress(),
    CommonComputeChecksum(),
    CommonDeparser()
) main;
