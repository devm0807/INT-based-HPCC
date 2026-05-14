"""
Side-by-side HPCC vs DCTCP comparison across E1, E2, E3.

Reads the existing per-experiment CSVs and emits:
  - results/compare_e1.png — throughput + RTT side-by-side for the
    single-flow case.
  - results/compare_e3.png — incast FCT CDFs overlaid.
  - results/summary.json   — machine-readable headline numbers for the
    writeup.

Run:
    python3 -m analysis.compare --results-dir /workspace/results
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

from analysis.parse_logs import (  # noqa: E402
    load,
    mark_fraction,
    summarize,
    throughput_mbps,
)


def _fct_us(path: str) -> float | None:
    tr = load(path)
    if tr.t_ack.size == 0:
        return None
    first_send = tr.t_s[(tr.event == "send") | (tr.event == "rtx")].min()
    return float((tr.t_ack.max() - first_send) * 1e6)


def compare_e1(results_dir: str) -> dict:
    out_path = os.path.join(results_dir, "compare_e1.png")
    fig, axs = plt.subplots(1, 2, figsize=(12, 4.5))
    headline: dict[str, dict] = {}
    for algo, color in (("dctcp", "tab:blue"), ("hpcc", "tab:red")):
        log = os.path.join(results_dir, f"e1_{algo}.csv")
        if not os.path.isfile(log):
            continue
        tr = load(log)
        stats = summarize(tr)
        headline[algo] = {
            "throughput_mbps": stats["throughput_mbps"],
            "rtt_us_p50":      stats["rtt_us_p50"],
            "rtt_us_p99":      stats["rtt_us_p99"],
            "w_mean":          stats["w_mean"],
        }
        t, tp = throughput_mbps(tr, 0.5, 1500)
        axs[0].plot(t, tp, label=algo.upper(), color=color, lw=1.2)
        # RTT CDF on the right.
        sorted_rtt = np.sort(tr.rtt_us) / 1000.0
        cdf = np.arange(1, sorted_rtt.size + 1) / sorted_rtt.size
        axs[1].plot(sorted_rtt, cdf, label=algo.upper(), color=color, lw=1.4)

    axs[0].axhline(10, ls="--", color="gray", alpha=0.5, label="link cap")
    axs[0].set_xlabel("Time (s)")
    axs[0].set_ylabel("Throughput (Mbps)")
    axs[0].set_ylim(0, 11)
    axs[0].legend(loc="lower right")
    axs[0].grid(True, alpha=0.3)
    axs[1].set_xlabel("RTT (ms)")
    axs[1].set_ylabel("CDF")
    axs[1].legend(loc="lower right")
    axs[1].grid(True, alpha=0.3)
    fig.suptitle("E1 single-flow — HPCC vs DCTCP")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    print(f"saved {out_path}")
    return headline


def compare_e3(results_dir: str) -> dict:
    out_path = os.path.join(results_dir, "compare_e3.png")
    rx = re.compile(r"r(\d+)_h(\d+)\.csv$")
    fig, ax = plt.subplots(figsize=(7, 5))
    headline: dict[str, dict] = {}
    for algo, color in (("dctcp", "tab:blue"), ("hpcc", "tab:red")):
        indir = os.path.join(results_dir, "e3", algo)
        paths = sorted(glob.glob(os.path.join(indir, "r*_h*.csv")))
        if not paths:
            continue
        fcts = []
        for p in paths:
            if rx.search(p) is None:
                continue
            v = _fct_us(p)
            if v is not None:
                fcts.append(v / 1000.0)
        arr = np.sort(np.array(fcts))
        if arr.size == 0:
            continue
        cdf = np.arange(1, arr.size + 1) / arr.size
        ax.plot(arr, cdf, label=algo.upper(), color=color, lw=1.4)
        headline[algo] = {
            "fct_mean_ms":  float(np.mean(arr)),
            "fct_p50_ms":   float(np.percentile(arr, 50)),
            "fct_p99_ms":   float(np.percentile(arr, 99)),
            "n_flows":      int(arr.size),
        }
    ax.set_xlabel("Per-flow FCT (ms)")
    ax.set_ylabel("CDF")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right")
    ax.set_title("E3 incast — HPCC vs DCTCP FCT distribution")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    print(f"saved {out_path}")
    return headline


def compare_e2(results_dir: str) -> dict:
    headline: dict[str, dict] = {}
    for algo in ("dctcp", "hpcc"):
        h1 = os.path.join(results_dir, f"e2_{algo}_h1.csv")
        h2 = os.path.join(results_dir, f"e2_{algo}_h2.csv")
        if not (os.path.isfile(h1) and os.path.isfile(h2)):
            continue
        tr1, tr2 = load(h1), load(h2)
        s1 = summarize(tr1, warmup_s=10)
        s2 = summarize(tr2, warmup_s=5)
        sum_tp = s1.get("throughput_mbps", 0) + s2.get("throughput_mbps", 0)
        # Jain
        x = np.array([s1.get("throughput_mbps", 0), s2.get("throughput_mbps", 0)])
        jain = float(np.sum(x) ** 2 / (x.size * float(np.sum(x ** 2)))) if x.sum() > 0 else 0
        headline[algo] = {
            "h1_mbps":   s1.get("throughput_mbps", 0),
            "h2_mbps":   s2.get("throughput_mbps", 0),
            "sum_mbps":  sum_tp,
            "jain":      jain,
        }
    return headline


def main() -> None:
    p = argparse.ArgumentParser(description="HPCC vs DCTCP comparison")
    p.add_argument("--results-dir", default="/workspace/results")
    p.add_argument("--out", default="/workspace/results/summary.json")
    args = p.parse_args()

    summary = {
        "e1": compare_e1(args.results_dir),
        "e2": compare_e2(args.results_dir),
        "e3": compare_e3(args.results_dir),
    }
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"saved {args.out}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
