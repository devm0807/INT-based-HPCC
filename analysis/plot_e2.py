"""
Plot E2 — two concurrent flows. Renders per-flow throughput, queue depth
(RTT as proxy), and Jain fairness over time.

Run:
    python3 -m analysis.plot_e2 \
        --h1 /workspace/results/e2_dctcp_h1.csv \
        --h2 /workspace/results/e2_dctcp_h2.csv \
        --out /workspace/results/e2_dctcp.png \
        --algo dctcp
"""
from __future__ import annotations

import argparse
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from analysis.parse_logs import load, throughput_mbps  # noqa: E402


def jain_index(*throughputs: float) -> float:
    arr = np.array([t for t in throughputs if t is not None])
    if arr.size == 0 or float(np.sum(arr ** 2)) == 0.0:
        return 0.0
    return float(np.sum(arr) ** 2 / (arr.size * float(np.sum(arr ** 2))))


def aligned_throughput(t_a: np.ndarray, x_a: np.ndarray,
                       t_b: np.ndarray, x_b: np.ndarray
                       ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resample two time-series onto a common bin grid (zero-fill outside range)."""
    t_max = float(max(t_a[-1] if t_a.size else 0,
                       t_b[-1] if t_b.size else 0))
    bin_s = float(t_a[1] - t_a[0]) if t_a.size > 1 else 0.1
    n = int(t_max / bin_s) + 1
    grid = np.arange(n) * bin_s
    out_a = np.zeros(n)
    out_b = np.zeros(n)
    if t_a.size:
        out_a[: t_a.size] = x_a[: n]
    if t_b.size:
        out_b[: t_b.size] = x_b[: n]
    return grid, out_a, out_b


def main() -> None:
    p = argparse.ArgumentParser(description="E2 two-flow plot")
    p.add_argument("--h1", required=True)
    p.add_argument("--h2", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--algo", choices=("dctcp", "hpcc"), required=True)
    p.add_argument("--bin-s", type=float, default=0.5)
    p.add_argument("--pkt-size", type=int, default=1500)
    p.add_argument("--link-mbps", type=float, default=10.0)
    p.add_argument("--h2-delay", type=float, default=5.0)
    args = p.parse_args()

    tr1 = load(args.h1)
    tr2 = load(args.h2)

    # Shift h2's timeline so absolute t=0 corresponds to h1's start.
    tr2_t_ack_shifted = tr2.t_ack + args.h2_delay

    t1, tp1 = throughput_mbps(tr1, args.bin_s, args.pkt_size)
    t2, tp2 = throughput_mbps(tr2, args.bin_s, args.pkt_size)
    # Shift h2 throughput timeline.
    if t2.size:
        t2 = t2 + args.h2_delay
    grid, tp1_g, tp2_g = aligned_throughput(t1, tp1, t2, tp2)

    # Per-bin Jain — only meaningful in the overlap window.
    fair = np.array([jain_index(a, b) if a + b > 0 else np.nan
                     for a, b in zip(tp1_g, tp2_g)])

    fig, axs = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    axs[0].plot(grid, tp1_g, label="h1", lw=1.2)
    axs[0].plot(grid, tp2_g, label="h2", lw=1.2)
    axs[0].plot(grid, tp1_g + tp2_g, label="sum", lw=1.0, ls=":")
    axs[0].axhline(args.link_mbps, ls="--", color="gray",
                   alpha=0.5, label=f"link {args.link_mbps:g} Mbps")
    axs[0].set_ylabel("Throughput (Mbps)")
    axs[0].grid(True, alpha=0.3)
    axs[0].legend(loc="upper right", fontsize=9, ncol=4)

    axs[1].plot(tr1.t_ack, tr1.rtt_us / 1000.0, label="h1", lw=0.5, alpha=0.6)
    axs[1].plot(tr2_t_ack_shifted, tr2.rtt_us / 1000.0, label="h2", lw=0.5, alpha=0.6)
    axs[1].set_ylabel("RTT (ms)")
    axs[1].grid(True, alpha=0.3)
    axs[1].legend(loc="upper right", fontsize=9)

    axs[2].plot(grid, fair, color="tab:green", lw=1.4)
    axs[2].axhline(1.0, ls="--", color="black", alpha=0.4)
    axs[2].set_ylabel("Jain fairness")
    axs[2].set_xlabel("Time (s, h1's t=0)")
    axs[2].set_ylim(0.4, 1.05)
    axs[2].grid(True, alpha=0.3)

    # Steady-state stats: ignore first 5s after h2 joins.
    steady_mask = grid >= args.h2_delay + 5.0
    if steady_mask.sum() > 0:
        ss_tp1 = float(np.mean(tp1_g[steady_mask]))
        ss_tp2 = float(np.mean(tp2_g[steady_mask]))
        ss_fair = jain_index(ss_tp1, ss_tp2)
    else:
        ss_tp1 = ss_tp2 = ss_fair = 0.0

    fig.suptitle(
        f"E2 {args.algo.upper()} — steady-state h1={ss_tp1:.2f} Mbps  "
        f"h2={ss_tp2:.2f} Mbps  Jain={ss_fair:.3f}"
    )
    fig.tight_layout()
    fig.savefig(args.out, dpi=120)
    print(f"saved {args.out}")
    print(json.dumps({
        "h1_steady_mbps": ss_tp1,
        "h2_steady_mbps": ss_tp2,
        "jain_steady":   ss_fair,
        "sum_steady":    ss_tp1 + ss_tp2,
    }, indent=2))


if __name__ == "__main__":
    main()
