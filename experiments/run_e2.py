"""
E2 — two long flows: h1 at t=0, h2 starts at t=delay, both run for `duration`.

Goal: per-flow throughput, queue depth, and Jain fairness over time.

Run inside the container:
    python3 -m experiments.run_e2 --algo dctcp --duration 60 --delay 5
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
SENDER_MODULES = {
    "dctcp": "sender.dctcp_sender",
    "hpcc":  "sender.hpcc_sender",
}


def main() -> None:
    p = argparse.ArgumentParser(description="E2: two long flows")
    p.add_argument("--algo", choices=("dctcp", "hpcc"), required=True)
    p.add_argument("--duration", type=float, default=60.0,
                   help="duration of h1's flow")
    p.add_argument("--delay", type=float, default=5.0,
                   help="seconds before h2 joins")
    p.add_argument("--out-dir", default="/workspace/results")
    p.add_argument("--ecn-k", type=int, default=5)
    p.add_argument("--bottleneck-pps", type=int, default=833)
    p.add_argument("--bottleneck-depth", type=int, default=40)
    p.add_argument("--w-init", type=int, default=4)
    p.add_argument("--padding", type=int, default=1400)
    args = p.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
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
         "--algo", args.algo, "--ecn-k", str(args.ecn_k),
         "--bottleneck-pps", str(args.bottleneck_pps),
         "--bottleneck-depth", str(args.bottleneck_depth)],
        cwd="/workspace",
    )
    if rc.returncode != 0:
        net.stop()
        sys.exit("load_tables failed")

    h1 = net.get("h1")
    h2 = net.get("h2")
    r1 = net.get("r1")

    reflector_proc = r1.popen(
        [sys.executable, "-u", "-m", "receiver.reflector", "--bind", "10.0.0.10"],
        cwd="/workspace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(0.5)

    h1_log = os.path.join(args.out_dir, f"e2_{args.algo}_h1.csv")
    h2_log = os.path.join(args.out_dir, f"e2_{args.algo}_h2.csv")
    sender_mod = SENDER_MODULES[args.algo]

    print(f"== start h1 flow ({args.duration}s) ==")
    h1_proc = h1.popen(
        [sys.executable, "-u", "-m", sender_mod, "10.0.0.10",
         "--duration", str(args.duration),
         "--log", h1_log, "--w-init", str(args.w_init),
         "--padding", str(args.padding)],
        cwd="/workspace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    print(f"== wait {args.delay}s, then start h2 ==")
    time.sleep(args.delay)
    h2_duration = max(1.0, args.duration - args.delay)
    h2_proc = h2.popen(
        [sys.executable, "-u", "-m", sender_mod, "10.0.0.10",
         "--duration", str(h2_duration),
         "--log", h2_log, "--w-init", str(args.w_init),
         "--padding", str(args.padding)],
        cwd="/workspace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    try:
        h2_proc.wait(timeout=args.duration + 30)
        h1_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        h1_proc.kill()
        h2_proc.kill()
    finally:
        reflector_proc.terminate()
        try:
            reflector_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            reflector_proc.kill()
        net.stop()

    # Quick summary.
    print(f"\nlogs: {h1_log}  {h2_log}")


if __name__ == "__main__":
    main()
