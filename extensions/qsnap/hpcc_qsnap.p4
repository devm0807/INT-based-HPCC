/*
 * hpcc_qsnap.p4 — hpcc.p4 + a periodic qdepth snapshot.
 *
 * Identical to p4src/hpcc.p4, but adds a 1024-slot register that the
 * egress writes std_meta.enq_qdepth into, indexed by
 * (egress_global_timestamp >> 13) & 0x3FF, giving ~8 ms time buckets.
 * A control-plane thread (qsnap/snapshot_reader.py) reads the array
 * every 100 ms via thrift to build an independent ground-truth queue
 * time series, decoupled from the sender's per-ACK view.
 *
 * Only the bottleneck switch (s1, switch_id=1) records snapshots, but
 * the program is symmetric and runs unchanged on s2.
 *
 * Architecture: v1model.
 */
#include "/workspace/p4src/common/parsers.p4"

#define QSNAP_SLOTS 1024
#define QSNAP_SHIFT 13         /* time bucket = us >> 13 ≈ 8 ms */

control HpccQsnapIngress(inout headers_t hdr,
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

#define BMV2_MAX_PORTS 16

control HpccQsnapEgress(inout headers_t hdr,
                        inout metadata_t meta,
                        inout standard_metadata_t std_meta) {

    register<bit<48>>(BMV2_MAX_PORTS) tx_byte_count_reg;
    register<bit<32>>(BMV2_MAX_PORTS) link_bps_reg;
    register<bit<16>>(1) switch_id_reg;
    register<bit<32>>(QSNAP_SLOTS) qdepth_history;

    apply {
        if (!hdr.ipv4.isValid() || meta.is_data != 1) {
            return;
        }
        if (hdr.shim.hop_count >= (bit<8>)MAX_HOPS) {
            return;
        }

        bit<32> egport = (bit<32>)std_meta.egress_port;

        bit<48> bytes_so_far;
        tx_byte_count_reg.read(bytes_so_far, egport);
        bytes_so_far = bytes_so_far + (bit<48>)std_meta.packet_length + 48w26;
        tx_byte_count_reg.write(egport, bytes_so_far);

        bit<32> bps;   link_bps_reg.read(bps, egport);
        bit<16> swid;  switch_id_reg.read(swid, 32w0);

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

        hdr.udp.length     = hdr.udp.length     + 16w26;
        hdr.ipv4.total_len = hdr.ipv4.total_len + 16w26;
        hdr.udp.checksum   = 16w0;

        /* Snapshot the queue depth into a 1024-slot ring indexed by an
         * 8 ms time bucket. The control plane reads this every 100 ms
         * to build a queue time-series independent of the sender. */
        bit<32> bucket = (bit<32>)(std_meta.egress_global_timestamp >> QSNAP_SHIFT)
                         & 32w0x000003FF;
        qdepth_history.write(bucket, (bit<32>)std_meta.enq_qdepth);
    }
}

V1Switch(
    CommonParser(),
    CommonVerifyChecksum(),
    HpccQsnapIngress(),
    HpccQsnapEgress(),
    CommonComputeChecksum(),
    CommonDeparser()
) main;
