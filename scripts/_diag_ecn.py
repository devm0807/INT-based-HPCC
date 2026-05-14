"""Diagnose where the ECN bit is getting lost on the DCTCP path."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet

from topo.dumbbell import DumbbellTopo, configure_hosts

JSON = "/workspace/build/dctcp.json"


def read_register(thrift_port: int, name: str, index: int = 0) -> int:
    proc = subprocess.run(
        ["simple_switch_CLI", "--thrift-port", str(thrift_port)],
        input=f"register_read {name} {index}\n".encode(),
        capture_output=True, timeout=5,
    )
    out = proc.stdout.decode()
    # Parse: "RuntimeCmd: register name[idx]= <value>"
    for line in out.splitlines():
        if "=" in line and name in line:
            return int(line.split("=")[-1].strip())
    return -1


def main() -> None:
    setLogLevel("warning")
    net = Mininet(topo=DumbbellTopo(json_path=JSON), link=TCLink, controller=None)
    net.start()
    configure_hosts(net)
    time.sleep(0.5)
    subprocess.run([sys.executable, "-m", "controller.load_tables",
                    "--algo", "dctcp", "--ecn-k", "5"],
                   check=True, cwd="/workspace")

    h1, r1 = net.get("h1"), net.get("r1")

    # Diagnostic reflector: log every received packet's TOS via tcpdump on
    # r1-eth0, and an in-process Python listener that reports cmsg results.
    pcap = "/tmp/diag_r1.pcap"
    td = r1.popen(
        ["tcpdump", "-i", "r1-eth0", "-U", "-w", pcap,
         "udp", "and", "dst", "port", "50000"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    refl = r1.popen(
        [sys.executable, "-u", "-m", "receiver.reflector",
         "--bind", "10.0.0.10", "-v"],
        cwd="/workspace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(0.3)

    # Burst at high rate so queue overflows K=5.
    print("== driving sender (heavy burst, w=32) ==")
    out = h1.cmd(
        f"cd /workspace && {sys.executable} -m sender.dctcp_sender "
        f"10.0.0.10 --duration 5 --log /tmp/diag.csv --padding 1400 --w-init 32"
    )
    print(out)

    td.terminate(); td.wait(2)
    refl.terminate(); refl.wait(2)

    # 1) Did the switch mark anything?
    print("\n== bmv2 counters ==")
    for sw in ("s1", "s2"):
        port = 9090 if sw == "s1" else 9091
        marked = read_register(port, "DctcpEgress.marked_pkt_count")
        total = read_register(port, "DctcpEgress.data_pkt_count")
        print(f"  {sw}: data={total}  marked={marked}  K=5")

    # 2) What ToS bytes were on the wire arriving at r1?
    print("\n== tcpdump tos sample (first 20 unique tos vals) ==")
    os.system(
        f"tcpdump -nn -r {pcap} -c 100 -vv 2>/dev/null "
        f"| grep -oE 'tos 0x[0-9a-f]+' | sort | uniq -c | head"
    )

    # 3) What did the reflector echo back?
    print("\n== sender ECN-echo events ==")
    os.system("awk -F, 'NR>1 && $2==\"ack\" && $5==1' /tmp/diag.csv | wc -l")
    os.system(
        "awk -F, 'NR>1 && $2==\"ack\" {n++; if($5==1) m++} END "
        "{printf \"acks=%d  ecn_echo=%d  frac=%.4f\\n\", n, m, m/n}' /tmp/diag.csv"
    )

    net.stop()


if __name__ == "__main__":
    main()
