"""Quick A/B smoke for HPCC v1 vs v2 on E1 single flow."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import csv
import statistics

sys.path.insert(0, "/workspace")

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet

from topo.dumbbell import DumbbellTopo, cleanup_mn, configure_hosts

JSON = "/workspace/build/hpcc.json"
LOG_V1 = "/tmp/hpcc_v1.csv"
LOG_V2 = "/tmp/hpcc_v2.csv"


def _run_sender(net, module: str, log_path: str, duration: float = 15) -> None:
    cleanup_mn()
    print(f"\n== sender = {module} ==")
    net.start()
    configure_hosts(net)
    time.sleep(0.5)
    subprocess.run(
        [sys.executable, "-m", "controller.load_tables", "--algo", "hpcc"],
        check=True, cwd="/workspace",
    )
    h1, r1 = net.get("h1"), net.get("r1")
    refl = r1.popen(
        [sys.executable, "-u", "-m", "receiver.reflector", "--bind", "10.0.0.10"],
        cwd="/workspace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(0.5)
    try:
        out = h1.cmd(
            f"cd /workspace && {sys.executable} -m {module} "
            f"10.0.0.10 --duration {duration} --log {log_path} "
            f"--padding 1400 --base-rtt 0.006"
        )
        print(out)
    finally:
        refl.terminate()
        try: refl.wait(timeout=2)
        except subprocess.TimeoutExpired: refl.kill()
        net.stop()


def _summarize(path: str, duration: float) -> None:
    rows = list(csv.DictReader(open(path)))
    acks = [r for r in rows if r["event"] in ("ack", "ack-stale")]
    stale = sum(1 for r in rows if r["event"] == "ack-stale")
    n = len(acks)
    ws = [float(r["w"]) for r in acks]
    rtts = [float(r["rtt_us"]) for r in acks]
    print(f"  acks={n} stale={stale}  throughput≈{n*1500*8/duration/1e6:.2f} Mbps")
    if ws:
        print(f"  w: mean={statistics.mean(ws):.2f} stdev={statistics.stdev(ws):.2f}")
    if rtts:
        sorted_r = sorted(rtts)
        print(f"  rtt(us): mean={statistics.mean(rtts):.0f} "
              f"p99={sorted_r[int(0.99*n)]:.0f}")


def main() -> None:
    setLogLevel("warning")
    duration = 15.0

    # v1 (baseline from sender/hpcc_sender.py)
    net = Mininet(topo=DumbbellTopo(json_path=JSON), link=TCLink, controller=None)
    _run_sender(net, "sender.hpcc_sender", LOG_V1, duration)
    print("\n-- v1 summary --"); _summarize(LOG_V1, duration)

    # v2 (this extension)
    net = Mininet(topo=DumbbellTopo(json_path=JSON), link=TCLink, controller=None)
    _run_sender(net, "extensions.hpcc_v2.hpcc_sender_v2", LOG_V2, duration)
    print("\n-- v2 summary --"); _summarize(LOG_V2, duration)


if __name__ == "__main__":
    main()
