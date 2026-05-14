"""
E4 — flow dynamics: N flows joining at scheduled times.

By default: h1 starts at t=0, h2 joins at t=20s, h3 joins at t=40s.
All flows persist to t=duration. Shows how the algorithm reacts to
the arrival of new flows (convergence, queue spikes, fairness rebuild).

Run inside the container:
    python3 -m experiments.run_e4 --algo dctcp \
        --duration 60 --start-times 0,20,40
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
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
    p = argparse.ArgumentParser(description="E4: flow dynamics")
    p.add_argument("--algo", choices=("dctcp", "hpcc"), required=True)
    p.add_argument("--duration", type=float, default=60.0,
                   help="total wallclock window; each flow runs until this end")
    p.add_argument("--start-times", type=str, default="0,20,40",
                   help="comma-separated seconds after t=0 when each sender joins")
    p.add_argument("--out-dir", default="/workspace/results")
    p.add_argument("--ecn-k", type=int, default=5)
    p.add_argument("--bottleneck-pps", type=int, default=833)
    p.add_argument("--bottleneck-depth", type=int, default=40)
    p.add_argument("--w-init", type=int, default=4)
    p.add_argument("--padding", type=int, default=1400)
    args = p.parse_args()

    starts = [float(s) for s in args.start_times.split(",")]
    n_flows = len(starts)
    if n_flows > 8:
        sys.exit("max 8 flows (h1..h8)")

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

    r1 = net.get("r1")
    reflector_proc = r1.popen(
        [sys.executable, "-u", "-m", "receiver.reflector", "--bind", "10.0.0.10"],
        cwd="/workspace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(0.5)

    procs: list[subprocess.Popen] = []
    sender_mod = SENDER_MODULES[args.algo]

    def _start_flow(i: int) -> None:
        host = net.get(f"h{i+1}")
        log_path = os.path.join(args.out_dir,
                                f"e4_{args.algo}_h{i+1}.csv")
        flow_duration = max(1.0, args.duration - starts[i])
        proc = host.popen(
            [sys.executable, "-u", "-m", sender_mod, "10.0.0.10",
             "--duration", str(flow_duration),
             "--log", log_path,
             "--w-init", str(args.w_init),
             "--padding", str(args.padding)],
            cwd="/workspace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        print(f"  flow h{i+1} started at +{starts[i]:.1f}s")

    print(f"== start {n_flows} flows ==")
    t0 = time.time()
    for i, st in enumerate(starts):
        # Sleep until the scheduled time.
        wait = (t0 + st) - time.time()
        if wait > 0:
            time.sleep(wait)
        _start_flow(i)

    try:
        # Wait for all to finish.
        for proc in procs:
            proc.wait(timeout=args.duration + 30)
    except subprocess.TimeoutExpired:
        for proc in procs:
            proc.kill()
    finally:
        reflector_proc.terminate()
        try:
            reflector_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            reflector_proc.kill()
        net.stop()

    print(f"\nlogs in {args.out_dir}/e4_{args.algo}_h*.csv")


if __name__ == "__main__":
    main()
