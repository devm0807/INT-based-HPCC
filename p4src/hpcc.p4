/*
 * hpcc.p4 — HPCC INT-driven dataplane.
 *
 * Pipeline:
 *   ingress: IPv4 LPM forwarding (egress port + new dst MAC + TTL--).
 *   egress:  on DATA packets only, append an int_hop_t to the SHIM/INT
 *            stack with this switch's per-hop telemetry:
 *              switch_id        (register)
 *              ingress_port, egress_port
 *              qdepth           (std_meta.enq_qdepth, BMv2 packet units)
 *              egress_tstamp_us (std_meta.egress_global_timestamp)
 *              tx_byte_count    (per-egress-port cumulative byte counter)
 *              link_bps         (per-egress-port register, set by controller)
 *            and bump shim.hop_count.
 *
 * The receiver reflects shim + hops verbatim into the ACK; the sender
 * computes per-hop u_i and U = max_i u_i to drive its window controller.
 *
 * Order on the wire: push_front shifts the existing stack right, so the
 * first switch's entry ends up CLOSEST to the payload (i.e. at the
 * deepest stack index) and the last switch's entry is at index 0. The
 * sender takes max over hops, so order doesn't matter for U.
 *
 * Architecture: v1model.
 */

#include "common/parsers.p4"

/* --- ingress ----------------------------------------------------------- */

control HpccIngress(inout headers_t hdr,
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

/* --- egress: INT insertion -------------------------------------------- */

#define BMV2_MAX_PORTS 16

control HpccEgress(inout headers_t hdr,
                   inout metadata_t meta,
                   inout standard_metadata_t std_meta) {

    /* Per-egress-port cumulative bytes transmitted. Controller resets to 0. */
    register<bit<48>>(BMV2_MAX_PORTS) tx_byte_count_reg;

    /* Per-egress-port link capacity in bps. Controller sets per port. */
    register<bit<32>>(BMV2_MAX_PORTS) link_bps_reg;

    /* This switch's identifier (1 byte slot 0). Controller sets at startup. */
    register<bit<16>>(1) switch_id_reg;

    apply {
        if (!hdr.ipv4.isValid() || meta.is_data != 1) {
            return;
        }
        if (hdr.shim.hop_count >= (bit<8>)MAX_HOPS) {
            return;  /* stack full — pass through unmodified */
        }

        bit<32> egport = (bit<32>)std_meta.egress_port;

        /* tx_byte_count: cumulative bytes leaving this port after we grow
         * the packet by sizeof(int_hop_t)=26.                            */
        bit<48> bytes_so_far;
        tx_byte_count_reg.read(bytes_so_far, egport);
        bytes_so_far = bytes_so_far + (bit<48>)std_meta.packet_length + 48w26;
        tx_byte_count_reg.write(egport, bytes_so_far);

        bit<32> bps;
        link_bps_reg.read(bps, egport);

        bit<16> swid;
        switch_id_reg.read(swid, 32w0);

        /* Push a new entry to the front of the hop stack and populate it. */
        hdr.hops.push_front(1);
        hdr.hops[0].setValid();
        hdr.hops[0].switch_id        = swid;
        hdr.hops[0].ingress_port     = (bit<16>)std_meta.ingress_port;
        hdr.hops[0].egress_port      = (bit<16>)std_meta.egress_port;
        hdr.hops[0].qdepth           = (bit<32>)std_meta.enq_qdepth;
        hdr.hops[0].egress_tstamp_us = (bit<48>)std_meta.egress_global_timestamp;
        hdr.hops[0].tx_byte_count    = bytes_so_far;
        hdr.hops[0].link_bps         = bps;

        hdr.shim.hop_count = hdr.shim.hop_count + 1;

        /* Packet just grew by 26 bytes — fix UDP / IP length, and zero the
         * UDP checksum (legal in IPv4, simpler than recomputing).         */
        hdr.udp.length     = hdr.udp.length     + 16w26;
        hdr.ipv4.total_len = hdr.ipv4.total_len + 16w26;
        hdr.udp.checksum   = 16w0;
    }
}

/* --- pipeline ---------------------------------------------------------- */

V1Switch(
    CommonParser(),
    CommonVerifyChecksum(),
    HpccIngress(),
    HpccEgress(),
    CommonComputeChecksum(),
    CommonDeparser()
) main;
