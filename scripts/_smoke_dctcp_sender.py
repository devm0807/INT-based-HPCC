"""Throwaway smoke: DctcpSender for 8s, post-mortem from CSV."""
from __future__ import annotations

import os
import subprocess
import sys
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet

from topo.dumbbell import DumbbellTopo, configure_hosts

JSON = "/workspace/build/dctcp.json"
LOG = "/tmp/sender_smoke.csv"


def main() -> None:
    setLogLevel("warning")
    net = Mininet(topo=DumbbellTopo(json_path=JSON), link=TCLink, controller=None)
    net.start()
    configure_hosts(net)
    time.sleep(0.5)

    rc = subprocess.run(
        [sys.executable, "-m", "controller.load_tables",
         "--algo", "dctcp", "--ecn-k", "5"],
        check=True, cwd="/workspace",
    )

    h1, r1 = net.get("h1"), net.get("r1")
    refl = r1.popen(
        [sys.executable, "-u", "-m", "receiver.reflector",
         "--bind", "10.0.0.10"],
        cwd="/workspace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(0.5)

    try:
        out = h1.cmd(
            f"cd /workspace && {sys.executable} -m sender.dctcp_sender "
            f"10.0.0.10 --duration 8 --log {LOG} --padding 1400"
        )
        print(out)
    finally:
        refl.terminate()
        try:
            refl.wait(timeout=2)
        except subprocess.TimeoutExpired:
            refl.kill()
        net.stop()

    print("== event breakdown ==")
    os.system(f"awk -F, 'NR>1 {{print $2}}' {LOG} | sort | uniq -c")
    print("== ack count ==")
    os.system(f"awk -F, 'NR>1 && $2==\"ack\"' {LOG} | wc -l")
    print("== avg rtt(us) ==")
    os.system(
        f"awk -F, 'NR>1 && $2==\"ack\" {{n++; s+=$4}} END "
        f"{{if(n) printf \"%.0f us over %d acks\\n\", s/n, n}}' {LOG}"
    )
    print("== final 3 update rows ==")
    os.system(f"awk -F, 'NR>1 && $2==\"update\"' {LOG} | tail -3")


if __name__ == "__main__":
    main()
