"""
Plot E3 — incast FCT distribution per algorithm.

Reads all per-flow CSVs under results/e3/<algo>/, computes per-flow FCT
(first send_ts → last ack_ts), and renders:
  - empirical CDF of FCT
  - per-round bar chart of max FCT (incast completion time)

Run:
    python3 -m analysis.plot_e3 \
        --algo dctcp \
        --in /workspace/results/e3/dctcp \
        --out /workspace/results/e3_dctcp.png
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from analysis.parse_logs import load  # noqa: E402


def fct_us(path: str) -> float | None:
    tr = load(path)
    if tr.t_ack.size == 0:
        return None
    first_send = tr.t_s[(tr.event == "send") | (tr.event == "rtx")].min()
    last_ack = tr.t_ack.max()
    return float((last_ack - first_send) * 1e6)


def main() -> None:
    p = argparse.ArgumentParser(description="E3 incast plot")
    p.add_argument("--algo", choices=("dctcp", "hpcc"), required=True)
    p.add_argument("--in", dest="indir", required=True,
                   help="directory of e3 CSVs (per-round, per-flow)")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    paths = sorted(glob.glob(os.path.join(args.indir, "r*_h*.csv")))
    if not paths:
        raise SystemExit(f"no logs under {args.indir}")

    rx = re.compile(r"r(\d+)_h(\d+)\.csv$")
    rounds: dict[int, dict[int, float]] = {}
    fcts: list[float] = []
    for p_ in paths:
        m = rx.search(p_)
        if not m:
            continue
        rnd, flow = int(m.group(1)), int(m.group(2))
        f = fct_us(p_)
        if f is None:
            continue
        rounds.setdefault(rnd, {})[flow] = f
        fcts.append(f)

    arr_us = np.array(fcts)
    arr_ms = arr_us / 1000.0
    if arr_ms.size == 0:
        raise SystemExit("no completed flows in logs")

    stats = {
        "n_flows":      int(arr_ms.size),
        "n_rounds":     int(len(rounds)),
        "fct_mean_ms":  float(np.mean(arr_ms)),
        "fct_p50_ms":   float(np.percentile(arr_ms, 50)),
        "fct_p99_ms":   float(np.percentile(arr_ms, 99)),
        "fct_max_ms":   float(np.max(arr_ms)),
    }
    print(json.dumps(stats, indent=2))

    # CDF
    fig, axs = plt.subplots(1, 2, figsize=(12, 4.5))
    sorted_ms = np.sort(arr_ms)
    cdf = np.arange(1, sorted_ms.size + 1) / sorted_ms.size
    axs[0].plot(sorted_ms, cdf, lw=1.4, marker=".", ms=2)
    axs[0].set_xlabel("Per-flow FCT (ms)")
    axs[0].set_ylabel("CDF")
    axs[0].grid(True, alpha=0.3)
    axs[0].axvline(stats["fct_p50_ms"], ls="--", color="gray", alpha=0.6,
                   label=f"p50 {stats['fct_p50_ms']:.1f}")
    axs[0].axvline(stats["fct_p99_ms"], ls="--", color="red", alpha=0.6,
                   label=f"p99 {stats['fct_p99_ms']:.1f}")
    axs[0].legend(loc="lower right")

    # Per-round max FCT (incast completion time)
    round_ids = sorted(rounds)
    max_per_round = [max(rounds[r].values()) / 1000.0 for r in round_ids]
    axs[1].bar(round_ids, max_per_round, color="tab:blue")
    axs[1].set_xlabel("Round")
    axs[1].set_ylabel("max-flow FCT (ms)")
    axs[1].grid(True, alpha=0.3, axis="y")

    fig.suptitle(
        f"E3 {args.algo.upper()} incast — {stats['n_flows']} flows over "
        f"{stats['n_rounds']} rounds, FCT p50={stats['fct_p50_ms']:.1f} ms "
        f"p99={stats['fct_p99_ms']:.1f} ms"
    )
    fig.tight_layout()
    fig.savefig(args.out, dpi=120)
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
