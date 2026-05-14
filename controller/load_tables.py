"""
Populate BMv2 tables and registers via thrift (simple_switch_CLI).

Run this AFTER `python3 -m topo.dumbbell` has the switches up. Topology
constants (host IPs/MACs, switch thrift ports) must match topo/dumbbell.py.

We use thrift instead of P4Runtime for the first cut — simple_switch_grpc
exposes both, and thrift commands are a 4-line script per switch with no
proto generation. Migrate to P4Runtime if/when we need it.

Usage:
    python3 -m controller.load_tables --algo dctcp --ecn-k 5
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import textwrap


# Topology constants — keep in sync with topo/dumbbell.py.
MAX_SENDERS = 8
R1_IP, R1_MAC = "10.0.0.10", "00:00:00:00:01:0a"


def _sender(i: int) -> tuple[str, str]:
    return f"10.0.0.{i}", f"00:00:00:00:01:{i:02x}"


S1_THRIFT_DEFAULT = 9090
S2_THRIFT_DEFAULT = 9091

# Bottleneck on s1's egress port toward s2 (port = N senders + 1).
S1_BOTTLENECK_PORT = MAX_SENDERS + 1
BOTTLENECK_RATE_PPS_DEFAULT = 833
BOTTLENECK_DEPTH_DEFAULT = 40

# Inter-switch "gateway" MACs. The receiving switch overwrites dst_mac in
# its own ipv4_forward action, so the value here is arbitrary as long as
# it's distinct enough to spot in pcaps.
S1_GW_MAC = "00:00:00:01:00:01"  # MAC s2 stamps when sending toward s1
S2_GW_MAC = "00:00:00:01:00:02"  # MAC s1 stamps when sending toward s2


def _dctcp_s1(ecn_k: int, bottleneck_pps: int, bottleneck_depth: int) -> str:
    """s1 sees each sender h_i on port i, s2 on port MAX_SENDERS+1.

    BMv2 queue rate + depth on the bottleneck egress port keeps the
    congested queue inside BMv2 where enq_qdepth is visible from P4.
    """
    lines = [
        "table_set_default DctcpIngress.ipv4_lpm DctcpIngress.drop",
    ]
    for i in range(1, MAX_SENDERS + 1):
        ip, mac = _sender(i)
        lines.append(
            f"table_add DctcpIngress.ipv4_lpm DctcpIngress.ipv4_forward "
            f"{ip}/32 => {mac} {i}"
        )
    lines.extend([
        f"table_add DctcpIngress.ipv4_lpm DctcpIngress.ipv4_forward "
        f"{R1_IP}/32 => {S2_GW_MAC} {S1_BOTTLENECK_PORT}",
        f"register_write DctcpEgress.ecn_threshold 0 {ecn_k}",
        "register_write DctcpEgress.data_pkt_count 0 0",
        "register_write DctcpEgress.marked_pkt_count 0 0",
        f"set_queue_rate {bottleneck_pps} {S1_BOTTLENECK_PORT}",
        f"set_queue_depth {bottleneck_depth} {S1_BOTTLENECK_PORT}",
    ])
    return "\n".join(lines) + "\n"


def _dctcp_s2(ecn_k: int) -> str:
    """s2 sees s1 on port 1, r1 on port 2. All sender IPs route via port 1."""
    lines = [
        "table_set_default DctcpIngress.ipv4_lpm DctcpIngress.drop",
    ]
    for i in range(1, MAX_SENDERS + 1):
        ip, _ = _sender(i)
        lines.append(
            f"table_add DctcpIngress.ipv4_lpm DctcpIngress.ipv4_forward "
            f"{ip}/32 => {S1_GW_MAC} 1"
        )
    lines.extend([
        f"table_add DctcpIngress.ipv4_lpm DctcpIngress.ipv4_forward "
        f"{R1_IP}/32 => {R1_MAC} 2",
        f"register_write DctcpEgress.ecn_threshold 0 {ecn_k}",
        "register_write DctcpEgress.data_pkt_count 0 0",
        "register_write DctcpEgress.marked_pkt_count 0 0",
    ])
    return "\n".join(lines) + "\n"


def _hpcc_s1(bottleneck_pps: int, bottleneck_depth: int) -> str:
    lines = [
        "table_set_default HpccIngress.ipv4_lpm HpccIngress.drop",
    ]
    for i in range(1, MAX_SENDERS + 1):
        ip, mac = _sender(i)
        lines.append(
            f"table_add HpccIngress.ipv4_lpm HpccIngress.ipv4_forward "
            f"{ip}/32 => {mac} {i}"
        )
    lines.extend([
        f"table_add HpccIngress.ipv4_lpm HpccIngress.ipv4_forward "
        f"{R1_IP}/32 => {S2_GW_MAC} {S1_BOTTLENECK_PORT}",
        "register_write HpccEgress.switch_id_reg 0 1",
    ])
    for port in range(1, MAX_SENDERS + 1):
        lines.append(f"register_write HpccEgress.link_bps_reg {port} 100000000")
        lines.append(f"register_write HpccEgress.tx_byte_count_reg {port} 0")
    lines.extend([
        f"register_write HpccEgress.link_bps_reg {S1_BOTTLENECK_PORT} 10000000",
        f"register_write HpccEgress.tx_byte_count_reg {S1_BOTTLENECK_PORT} 0",
        f"set_queue_rate {bottleneck_pps} {S1_BOTTLENECK_PORT}",
        f"set_queue_depth {bottleneck_depth} {S1_BOTTLENECK_PORT}",
    ])
    return "\n".join(lines) + "\n"


def _hpcc_s2() -> str:
    lines = [
        "table_set_default HpccIngress.ipv4_lpm HpccIngress.drop",
    ]
    for i in range(1, MAX_SENDERS + 1):
        ip, _ = _sender(i)
        lines.append(
            f"table_add HpccIngress.ipv4_lpm HpccIngress.ipv4_forward "
            f"{ip}/32 => {S1_GW_MAC} 1"
        )
    lines.extend([
        f"table_add HpccIngress.ipv4_lpm HpccIngress.ipv4_forward "
        f"{R1_IP}/32 => {R1_MAC} 2",
        "register_write HpccEgress.switch_id_reg 0 2",
        "register_write HpccEgress.link_bps_reg 1 100000000",
        "register_write HpccEgress.link_bps_reg 2 100000000",
        "register_write HpccEgress.tx_byte_count_reg 1 0",
        "register_write HpccEgress.tx_byte_count_reg 2 0",
    ])
    return "\n".join(lines) + "\n"


def push(thrift_port: int, commands: str) -> str:
    cli = shutil.which("simple_switch_CLI")
    if cli is None:
        sys.exit("simple_switch_CLI not on PATH (run inside the docker dev container)")
    proc = subprocess.run(
        [cli, "--thrift-port", str(thrift_port)],
        input=commands.encode(),
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        sys.exit(
            f"thrift load on port {thrift_port} failed:\n"
            f"--- stdout ---\n{proc.stdout.decode()}\n"
            f"--- stderr ---\n{proc.stderr.decode()}"
        )
    return proc.stdout.decode()


def main():
    p = argparse.ArgumentParser(description="INT-CC table loader")
    p.add_argument("--algo", choices=["dctcp", "hpcc"], required=True)
    p.add_argument("--ecn-k", type=int, default=5,
                   help="ECN marking threshold in packets (DCTCP only). "
                        "Plan default = 5 packets at 10 Mbps.")
    p.add_argument("--bottleneck-pps", type=int,
                   default=BOTTLENECK_RATE_PPS_DEFAULT,
                   help="BMv2 dequeue rate on s1's port toward s2 (pps).")
    p.add_argument("--bottleneck-depth", type=int,
                   default=BOTTLENECK_DEPTH_DEFAULT,
                   help="BMv2 queue depth on s1's port toward s2 (packets).")
    p.add_argument("--s1-thrift", type=int, default=S1_THRIFT_DEFAULT)
    p.add_argument("--s2-thrift", type=int, default=S2_THRIFT_DEFAULT)
    args = p.parse_args()

    if args.algo == "dctcp":
        s1_cmds = _dctcp_s1(args.ecn_k, args.bottleneck_pps, args.bottleneck_depth)
        s2_cmds = _dctcp_s2(args.ecn_k)
    else:
        s1_cmds = _hpcc_s1(args.bottleneck_pps, args.bottleneck_depth)
        s2_cmds = _hpcc_s2()

    print(f"== loading s1 on thrift {args.s1_thrift} ==")
    print(push(args.s1_thrift, s1_cmds))
    print(f"== loading s2 on thrift {args.s2_thrift} ==")
    print(push(args.s2_thrift, s2_cmds))


if __name__ == "__main__":
    main()
