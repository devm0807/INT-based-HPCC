"""
Two-switch dumbbell topology for the INT-CC project.

      h1 ─┐
          ├── s1 ──[bottleneck 10 Mbps, 1 ms, 40 pkt buf]── s2 ── r1
      h2 ─┘

Hosts are L3-only with static ARP — BMv2 doesn't intercept ARP, so we
preload every host's ARP table at startup. Switches do LPM forwarding
with src/dst MAC rewrite (action `ipv4_forward` in dctcp.p4 / hpcc.p4).

Run inside the dev container:
    python3 -m topo.dumbbell --json /workspace/build/dctcp.json

The script blocks on Mininet's CLI. From there, in another shell:
    docker compose -f docker/docker-compose.yml exec p4 \\
        python3 -m controller.load_tables --algo dctcp --ecn-k 5

After that, ping h1 -> r1 should work.
"""
from __future__ import annotations

import argparse
import os
import subprocess

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.topo import Topo

from topo.p4switch import P4Switch


_OFFLOAD_FLAGS = ("rx", "tx", "sg", "tso", "gso", "gro", "lro", "ufo")


def cleanup_mn() -> None:
    """Best-effort cleanup of leftover Mininet veths / BMv2 processes.

    Run at the start of any experiment driver: a previous crashed run can
    leave veth pairs around with the names we're about to allocate, which
    makes Mininet's net.start() fail with EEXIST. Idempotent and safe.
    """
    subprocess.run(["mn", "-c"], capture_output=True, timeout=10)
    subprocess.run(["pkill", "-f", "simple_switch"], capture_output=True)


def _disable_offloads_on_iface(runner_cmd, iface_name: str) -> None:
    """Turn off NIC offloads that don't make sense on veth + BMv2.

    BMv2 reads/writes raw bytes from veth via pcap; if a packet leaves the
    sender with CHECKSUM_PARTIAL (checksum to be filled by hardware), the
    bytes BMv2 copies into the next veth still carry the wrong checksum,
    and the destination kernel silently drops on UDP/IP csum verify.
    Disabling tx-checksum on every interface makes the sender finalize
    the checksum in software before BMv2 ever sees the frame.
    """
    flags = " ".join(f"{f} off" for f in _OFFLOAD_FLAGS)
    runner_cmd(f"ethtool --offload {iface_name} {flags} >/dev/null 2>&1")


# Topology constants. Must match controller/load_tables.py — keep them in
# lockstep so a switch's ipv4_lpm and the host that owns the IP agree.
#
# Up to MAX_SENDERS senders (h1..hN). Each h_i is on s1's port i. The
# bottleneck link s1↔s2 lives on s1's port (MAX_SENDERS+1). r1 is on s2's
# port 2. Experiments choose how many senders to activate; unused hosts
# sit idle and cost nothing.
MAX_SENDERS = 8
BOTTLENECK_PORT = MAX_SENDERS + 1  # s1's port number toward s2

HOSTS = {}
for _i in range(1, MAX_SENDERS + 1):
    HOSTS[f"h{_i}"] = dict(ip=f"10.0.0.{_i}/24",
                           mac=f"00:00:00:00:01:{_i:02x}")
HOSTS["r1"] = dict(ip="10.0.0.10/24", mac="00:00:00:00:01:0a")


class DumbbellTopo(Topo):
    def build(self, json_path: str, n_senders: int = MAX_SENDERS):
        if not 1 <= n_senders <= MAX_SENDERS:
            raise ValueError(f"n_senders must be in [1, {MAX_SENDERS}]")
        self.n_senders = n_senders

        s1 = self.addSwitch(
            "s1", cls=P4Switch,
            json_path=json_path, device_id=1,
            thrift_port=9090, grpc_port=50051,
        )
        s2 = self.addSwitch(
            "s2", cls=P4Switch,
            json_path=json_path, device_id=2,
            thrift_port=9091, grpc_port=50052,
        )

        senders = []
        for i in range(1, n_senders + 1):
            host = self.addHost(f"h{i}",
                                ip=HOSTS[f"h{i}"]["ip"],
                                mac=HOSTS[f"h{i}"]["mac"])
            self.addLink(host, s1, port2=i)
            senders.append(host)

        r1 = self.addHost("r1", ip=HOSTS["r1"]["ip"], mac=HOSTS["r1"]["mac"])
        self.addLink(r1, s2, port2=2)

        # Bottleneck — 1 ms one-way delay; rate + buffer enforced by BMv2.
        self.addLink(s1, s2, port1=BOTTLENECK_PORT, port2=1, delay="1ms")


def configure_hosts(net: Mininet) -> None:
    """Preload ARP and disable IPv6 / NIC offloads on every active host.

    Supports any number of senders h1..hN that are present in `net`.
    """
    sender_names = [n for n in HOSTS
                    if n.startswith("h") and n in net.nameToNode]
    r1 = net.get("r1")
    senders = [net.get(n) for n in sender_names]
    all_hosts = senders + [r1]

    # Build full ARP map: every host learns every other host's MAC.
    name_to_host = {h.name: h for h in all_hosts}
    for src in all_hosts:
        for dst_name, info in HOSTS.items():
            if dst_name == src.name or dst_name not in name_to_host:
                continue
            ip = info["ip"].split("/")[0]
            src.cmd(f"arp -s {ip} {info['mac']}")

    for h in all_hosts:
        h.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null")
        h.cmd("sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null")
        h.cmd("sysctl -w net.ipv4.tcp_ecn=2 >/dev/null")
        for intf in h.nameToIntf.values():
            if intf.name == "lo":
                continue
            _disable_offloads_on_iface(h.cmd, intf.name)

    # Disable offloads on the switch-side veth peers (root netns).
    def _root(cmd):
        subprocess.run(cmd, shell=True, capture_output=True)
    for sw in net.switches:
        for intf in sw.nameToIntf.values():
            if intf.name == "lo":
                continue
            _disable_offloads_on_iface(_root, intf.name)


def main():
    p = argparse.ArgumentParser(description="INT-CC dumbbell topology")
    p.add_argument("--json", required=True, help="compiled BMv2 .json")
    p.add_argument("--no-cli", action="store_true",
                   help="exit immediately after setup (for scripted use)")
    p.add_argument("--n-senders", type=int, default=MAX_SENDERS,
                   help=f"number of sender hosts h1..hN (1..{MAX_SENDERS})")
    args = p.parse_args()

    if not os.path.isfile(args.json):
        raise SystemExit(f"P4 JSON not found: {args.json}")

    setLogLevel("info")

    topo = DumbbellTopo(json_path=args.json, n_senders=args.n_senders)
    net = Mininet(topo=topo, link=TCLink, controller=None)
    net.start()

    info("*** configuring hosts (static ARP, IPv6 off)\n")
    configure_hosts(net)

    info(
        "*** topology up. switches: s1 (thrift 9090, grpc 50051), "
        "s2 (thrift 9091, grpc 50052)\n"
        "*** next: in another shell, run `python3 -m controller.load_tables "
        "--algo dctcp --ecn-k 5` to populate forwarding\n"
    )

    if not args.no_cli:
        CLI(net)
    net.stop()


if __name__ == "__main__":
    main()
