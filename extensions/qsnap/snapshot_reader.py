"""
Control-plane thread that polls hpcc_qsnap.p4's `qdepth_history`
register array every `--period-ms` and dumps the time-series to CSV.

Use as a sidecar alongside an experiment driver:

    # terminal 1
    python3 -m extensions.qsnap.snapshot_reader --thrift-port 9090 \
        --duration 30 --period-ms 100 --out /tmp/qsnap.csv

    # terminal 2
    python3 -m experiments.run_e1 --algo hpcc_qsnap --duration 30 ...
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import time

QSNAP_SLOTS = 1024
RE_VALS = re.compile(r"qdepth_history\[\d+\]=\s*(\d+)")


def read_all(thrift_port: int) -> list[int]:
    """Read every slot of the qdepth_history register array."""
    proc = subprocess.run(
        ["simple_switch_CLI", "--thrift-port", str(thrift_port)],
        input=b"register_read HpccQsnapEgress.qdepth_history\n",
        capture_output=True, timeout=5,
    )
    if proc.returncode != 0:
        return []
    out = proc.stdout.decode()
    return [int(x) for x in RE_VALS.findall(out)][:QSNAP_SLOTS]


def main() -> None:
    p = argparse.ArgumentParser(description="qdepth_history poller")
    p.add_argument("--thrift-port", type=int, default=9090)
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--period-ms", type=int, default=100)
    p.add_argument("--out", required=True, help="output CSV path")
    args = p.parse_args()

    rows: list[tuple[float, int, int]] = []  # (wallclock_s, bucket, qdepth)
    period_s = args.period_ms / 1000.0
    t0 = time.time()
    end = t0 + args.duration

    print(f"polling thrift {args.thrift_port} every {args.period_ms} ms "
          f"for {args.duration} s → {args.out}")
    seen_max = {}  # bucket -> last qdepth seen, used to dedupe
    while time.time() < end:
        ts = time.time() - t0
        vals = read_all(args.thrift_port)
        for bucket, q in enumerate(vals):
            if seen_max.get(bucket) != q:
                seen_max[bucket] = q
                rows.append((ts, bucket, q))
        time.sleep(max(0.0, period_s - 0.005))

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "bucket", "qdepth_pkts"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows")


if __name__ == "__main__":
    main()
