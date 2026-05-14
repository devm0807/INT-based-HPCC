"""
Plot E4 — N flows joining at staggered times.

Per-flow throughput stacked, per-flow window, queue/RTT, and Jain
fairness over time. Highlights convergence dynamics after each join.

Run:
    python3 -m analysis.plot_e4 \
        --logs /workspace/results/e4_dctcp_h1.csv,...h2.csv,...h3.csv \
        --start-times 0,20,40 \
        --algo dctcp \
        --out /workspace/results/e4_dctcp.png
"""
from __future__ import annotations

import argparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from analysis.parse_logs import load, throughput_mbps  # noqa: E402


def jain_index(values: np.ndarray) -> float:
    arr = np.asarray(values, dtype=float)
    arr = arr[arr > 0]
    if arr.size == 0 or float(np.sum(arr ** 2)) == 0.0:
        return 0.0
    return float(np.sum(arr) ** 2 / (arr.size * float(np.sum(arr ** 2))))


def main() -> None:
    p = argparse.ArgumentParser(description="E4 plot")
    p.add_argument("--logs", required=True,
                   help="comma-separated per-flow CSV paths in start order")
    p.add_argument("--start-times", required=True,
                   help="comma-separated start times in seconds")
    p.add_argument("--algo", choices=("dctcp", "hpcc"), required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--bin-s", type=float, default=0.5)
    p.add_argument("--pkt-size", type=int, default=1500)
    p.add_argument("--link-mbps", type=float, default=10.0)
    args = p.parse_args()

    log_paths = args.logs.split(",")
    start_times = [float(s) for s in args.start_times.split(",")]
    if len(log_paths) != len(start_times):
        raise SystemExit("--logs and --start-times must have equal length")

    traces = [load(p) for p in log_paths]
    t_max = max(start_times[i] + traces[i].duration_s for i in range(len(traces)))
    n_bins = int(t_max / args.bin_s) + 1
    grid = np.arange(n_bins) * args.bin_s
    per_flow_tp = np.zeros((len(traces), n_bins))

    for i, tr in enumerate(traces):
        t_local, tp_local = throughput_mbps(tr, args.bin_s, args.pkt_size)
        offset_bins = int(start_times[i] / args.bin_s)
        end_bin = min(n_bins, offset_bins + tp_local.size)
        n_copy = end_bin - offset_bins
        if n_copy > 0:
            per_flow_tp[i, offset_bins:end_bin] = tp_local[:n_copy]

    sum_tp = per_flow_tp.sum(axis=0)
    fair = np.array([jain_index(per_flow_tp[:, k]) for k in range(n_bins)])

    fig, axs = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    for i, tr in enumerate(traces):
        axs[0].plot(grid, per_flow_tp[i], label=f"h{i+1} (joins {start_times[i]:.0f}s)",
                    lw=1.2)
    axs[0].plot(grid, sum_tp, ls=":", color="black", lw=0.9, label="sum")
    axs[0].axhline(args.link_mbps, ls="--", color="gray", alpha=0.5,
                   label=f"link {args.link_mbps:g} Mbps")
    axs[0].set_ylabel("Throughput (Mbps)")
    axs[0].legend(loc="upper right", fontsize=8, ncol=2)
    axs[0].grid(True, alpha=0.3)

    for i, tr in enumerate(traces):
        t_shift = tr.t_ack + start_times[i]
        axs[1].plot(t_shift, tr.rtt_us / 1000.0, lw=0.4, alpha=0.5,
                    label=f"h{i+1}")
    axs[1].set_ylabel("RTT (ms)")
    axs[1].grid(True, alpha=0.3)
    axs[1].legend(loc="upper right", fontsize=8)

    axs[2].plot(grid, fair, color="tab:green", lw=1.4)
    axs[2].axhline(1.0, ls="--", color="black", alpha=0.4)
    axs[2].set_ylabel("Jain fairness")
    axs[2].set_xlabel("Time (s)")
    axs[2].set_ylim(0, 1.05)
    axs[2].grid(True, alpha=0.3)

    # Vertical lines at flow joins.
    for ax in axs:
        for st in start_times:
            ax.axvline(st, ls=":", color="purple", alpha=0.4)

    fig.suptitle(
        f"E4 {args.algo.upper()} — {len(traces)} flows, joins at "
        + ", ".join(f"{s:.0f}s" for s in start_times)
    )
    fig.tight_layout()
    fig.savefig(args.out, dpi=120)
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
