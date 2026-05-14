"""
E3 — incast: N short flows fire concurrently to one receiver, R rounds total.

Each round: N senders simultaneously transmit `--bytes-per-flow` bytes
(default 100 KB) to r1. Each sender writes a CSV per round. Analysis
extracts per-flow Flow Completion Time (FCT = first-send to last-ack)
and aggregates over rounds.

Run inside the container:
    python3 -m experiments.run_e3 --algo dctcp --n-flows 8 \
        --rounds 5 --bytes-per-flow 100000
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

# Approximate L2 frame size for byte/packet conversion.
_PKT_BYTES = 1500


def main() -> None:
    p = argparse.ArgumentParser(description="E3: incast")
    p.add_argument("--algo", choices=("dctcp", "hpcc"), required=True)
    p.add_argument("--n-flows", type=int, default=8,
                   help="number of concurrent senders (≤ 8)")
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--bytes-per-flow", type=int, default=100_000)
    p.add_argument("--out-dir", default="/workspace/results/e3")
    p.add_argument("--ecn-k", type=int, default=5)
    p.add_argument("--bottleneck-pps", type=int, default=833)
    p.add_argument("--bottleneck-depth", type=int, default=40)
    p.add_argument("--w-init", type=int, default=4)
    p.add_argument("--padding", type=int, default=1400)
    p.add_argument("--inter-round-s", type=float, default=0.5,
                   help="quiet time between rounds")
    args = p.parse_args()

    if args.n_flows > 8:
        sys.exit("E3 supports up to 8 flows (h1..h8)")

    max_packets = max(1, (args.bytes_per_flow + _PKT_BYTES - 1) // _PKT_BYTES)
    out_dir = os.path.join(args.out_dir, args.algo)
    os.makedirs(out_dir, exist_ok=True)

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

    sender_mod = SENDER_MODULES[args.algo]
    sender_hosts = [net.get(f"h{i+1}") for i in range(args.n_flows)]

    try:
        for rnd in range(args.rounds):
            print(f"== round {rnd+1}/{args.rounds} "
                  f"({args.n_flows} × {args.bytes_per_flow} B) ==")
            procs: list[subprocess.Popen] = []
            for i, host in enumerate(sender_hosts):
                log_path = os.path.join(
                    out_dir, f"r{rnd:02d}_h{i+1}.csv"
                )
                proc = host.popen(
                    [sys.executable, "-u", "-m", sender_mod, "10.0.0.10",
                     "--duration", "30",  # safety cap; max_packets is the real limit
                     "--max-packets", str(max_packets),
                     "--log", log_path,
                     "--w-init", str(args.w_init),
                     "--padding", str(args.padding),
                     "--base-rtt", "0.006"],
                    cwd="/workspace",
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                procs.append(proc)
            for proc in procs:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
            time.sleep(args.inter_round_s)
    finally:
        reflector_proc.terminate()
        try:
            reflector_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            reflector_proc.kill()
        net.stop()

    print(f"\nlogs in {out_dir}/")


if __name__ == "__main__":
    main()
