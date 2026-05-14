"""
E1 — single long flow on the dumbbell.

Brings up the dumbbell, loads tables, runs reflector on r1 and a single
long sender on h1 for `--duration` seconds. Algorithm is selectable
(dctcp now; hpcc later).

Run inside the container:
    python3 -m experiments.run_e1 --algo dctcp --duration 60 \
        --log /workspace/results/e1_dctcp.csv
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet

from topo.dumbbell import DumbbellTopo, cleanup_mn, configure_hosts


JSON_PATHS = {
    "dctcp": "/workspace/build/dctcp.json",
    "hpcc":  "/workspace/build/hpcc.json",
}


def main() -> None:
    p = argparse.ArgumentParser(description="E1: single long flow")
    p.add_argument("--algo", choices=("dctcp", "hpcc"), required=True)
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--log", default="/workspace/results/e1.csv",
                   help="sender per-event CSV")
    p.add_argument("--ecn-k", type=int, default=5)
    p.add_argument("--bottleneck-pps", type=int, default=833)
    p.add_argument("--bottleneck-depth", type=int, default=40)
    p.add_argument("--w-init", type=int, default=4)
    p.add_argument("--padding", type=int, default=1400)
    args = p.parse_args()

    json_path = JSON_PATHS[args.algo]
    if not os.path.isfile(json_path):
        sys.exit(f"{json_path} missing — run `make p4-build-{args.algo}` first")

    setLogLevel("warning")
    cleanup_mn()

    print(f"== bring up dumbbell ({args.algo}) ==")
    net = Mininet(topo=DumbbellTopo(json_path=json_path),
                  link=TCLink, controller=None)
    net.start()
    configure_hosts(net)
    time.sleep(0.5)

    print(f"== load {args.algo} tables ==")
    rc = subprocess.run(
        [sys.executable, "-m", "controller.load_tables",
         "--algo", args.algo,
         "--ecn-k", str(args.ecn_k),
         "--bottleneck-pps", str(args.bottleneck_pps),
         "--bottleneck-depth", str(args.bottleneck_depth)],
        cwd="/workspace",
    )
    if rc.returncode != 0:
        net.stop()
        sys.exit("load_tables failed")

    h1 = net.get("h1")
    r1 = net.get("r1")

    print("== start reflector on r1 ==")
    reflector_proc = r1.popen(
        [sys.executable, "-u", "-m", "receiver.reflector",
         "--bind", "10.0.0.10"],
        cwd="/workspace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(0.5)

    try:
        sender_module = "sender.dctcp_sender" if args.algo == "dctcp" else "sender.hpcc_sender"
        print(f"== run {sender_module} for {args.duration}s ==")
        out = h1.cmd(
            f"cd /workspace && {sys.executable} -m {sender_module} "
            f"10.0.0.10 --duration {args.duration} --log {args.log} "
            f"--w-init {args.w_init} --padding {args.padding}"
        )
        print(out)
    finally:
        reflector_proc.terminate()
        try:
            reflector_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            reflector_proc.kill()

        # Snapshot switch counters before we tear down.
        for sw, port in (("s1", 9090), ("s2", 9091)):
            proc = subprocess.run(
                ["simple_switch_CLI", "--thrift-port", str(port)],
                input=(
                    b"register_read DctcpEgress.data_pkt_count 0\n"
                    b"register_read DctcpEgress.marked_pkt_count 0\n"
                ),
                capture_output=True, timeout=5,
            )
            print(f"-- {sw} counters --")
            for line in proc.stdout.decode().splitlines():
                if "=" in line and ("data_pkt_count" in line or "marked_pkt_count" in line):
                    print("  " + line.strip())

        net.stop()

    print(f"\nlog: {args.log}")


if __name__ == "__main__":
    main()
