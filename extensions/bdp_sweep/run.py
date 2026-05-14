"""
BDP-hypothesis sweep — vary the bottleneck rate (10 / 50 / 100 Mbps)
and measure HPCC's behavior with the C sender.

Confirms or refutes the claim in docs/discussion.md that HPCC's
under-utilization at small BDP comes from the qdepth/B term
dominating U.

Run inside the container:
    python3 -m extensions.bdp_sweep.run
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
import statistics
import time

sys.path.insert(0, "/workspace")

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet

from topo.dumbbell import DumbbellTopo, cleanup_mn, configure_hosts

JSON = "/workspace/build/hpcc.json"
CSENDER = "/workspace/extensions/csender/hpcc_sender"
OUT_DIR = "/workspace/extensions/bdp_sweep"

# Each trial: (label, bottleneck_pps, link_bps).
# pps = link_bps / (1500 * 8) for MTU-sized packets.
TRIALS = [
    ("10mbps",   833, 10_000_000),
    ("50mbps",  4166, 50_000_000),
    ("100mbps", 8333, 100_000_000),
]
DURATION_S = 15
PADDING = 1400


def _set_bottleneck(thrift_port: int, pps: int, link_bps: int,
                    bottleneck_port: int = 9) -> None:
    cmds = (
        f"set_queue_rate {pps} {bottleneck_port}\n"
        f"register_write HpccEgress.link_bps_reg {bottleneck_port} {link_bps}\n"
        f"register_write HpccEgress.tx_byte_count_reg {bottleneck_port} 0\n"
    )
    proc = subprocess.run(
        ["simple_switch_CLI", "--thrift-port", str(thrift_port)],
        input=cmds.encode(),
        capture_output=True, timeout=10,
    )
    if proc.returncode != 0:
        sys.exit(f"thrift command failed: {proc.stderr.decode()}")


def _summarize(csv_path: str, duration: float, link_mbps: float) -> dict:
    rows = list(csv.DictReader(open(csv_path)))
    acks = [r for r in rows if r["event"] == "ack"]
    if not acks:
        return {"label": "(empty)", "throughput_mbps": 0, "n": 0}
    rtts = [float(r["rtt_us"]) for r in acks]
    ws = [float(r["w"]) for r in acks]
    n = len(acks)
    return {
        "n_ack":          n,
        "throughput_mbps": n * 1500 * 8 / duration / 1e6,
        "util_pct":       (n * 1500 * 8 / duration / 1e6) / link_mbps * 100,
        "rtt_us_mean":    statistics.mean(rtts),
        "rtt_us_p99":     sorted(rtts)[int(0.99 * n)],
        "w_mean":         statistics.mean(ws),
        "w_stdev":        statistics.stdev(ws) if n > 1 else 0,
    }


def main() -> None:
    if not os.path.isfile(CSENDER):
        sys.exit(f"missing {CSENDER} — run `make -C extensions/csender`")
    if not os.path.isfile(JSON):
        sys.exit(f"missing {JSON} — run `make p4-build-hpcc`")
    os.makedirs(OUT_DIR, exist_ok=True)

    setLogLevel("warning")
    cleanup_mn()

    print("== bring up dumbbell ==")
    net = Mininet(topo=DumbbellTopo(json_path=JSON), link=TCLink, controller=None)
    net.start()
    configure_hosts(net)
    time.sleep(0.5)

    print("== load HPCC tables (default 10 Mbps, will override per trial) ==")
    subprocess.run(
        [sys.executable, "-m", "controller.load_tables", "--algo", "hpcc"],
        check=True, cwd="/workspace",
    )

    h1, r1 = net.get("h1"), net.get("r1")

    results = []
    try:
        refl = r1.popen(
            [sys.executable, "-u", "-m", "receiver.reflector",
             "--bind", "10.0.0.10"],
            cwd="/workspace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        time.sleep(0.5)

        for label, pps, link_bps in TRIALS:
            print(f"\n== trial: {label} (pps={pps}, link_bps={link_bps}) ==")
            _set_bottleneck(9090, pps, link_bps)

            log = os.path.join(OUT_DIR, f"hpcc_{label}.csv")
            out = h1.cmd(
                f"{CSENDER} 10.0.0.10 --duration {DURATION_S} --log {log} "
                f"--padding {PADDING} --base-rtt 0.006 --w-init 8 --w-max 256"
            )
            print(out.strip().splitlines()[-1])

            stats = _summarize(log, DURATION_S, link_bps / 1e6)
            stats["label"] = label
            stats["link_mbps"] = link_bps / 1e6
            results.append(stats)

        refl.terminate()
        try: refl.wait(timeout=2)
        except subprocess.TimeoutExpired: refl.kill()
    finally:
        net.stop()

    # Print and save the summary.
    print("\n" + "=" * 60)
    print(f"{'rate':>8}  {'tput':>8}  {'util':>6}  "
          f"{'rtt-mean':>9}  {'rtt-p99':>9}  {'w-mean':>7}")
    print("-" * 60)
    for s in results:
        print(
            f"{s['label']:>8}  "
            f"{s['throughput_mbps']:>7.2f}M  "
            f"{s['util_pct']:>5.1f}%  "
            f"{s['rtt_us_mean'] / 1000:>7.2f}ms  "
            f"{s['rtt_us_p99'] / 1000:>7.2f}ms  "
            f"{s['w_mean']:>7.2f}"
        )

    summary_path = os.path.join(OUT_DIR, "summary.json")
    import json
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nwrote {summary_path}")


if __name__ == "__main__":
    main()
