"""
Plot E1 results: throughput, RTT, window, α / mark fraction.

Run:
    python3 -m analysis.plot_e1 /workspace/results/e1_dctcp.csv \
        --out /workspace/results/e1_dctcp.png
"""
from __future__ import annotations

import argparse
import json

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


def main() -> None:
    p = argparse.ArgumentParser(description="E1 plot")
    p.add_argument("log")
    p.add_argument("--out", required=True, help="output PNG path")
    p.add_argument("--bin-s", type=float, default=0.1)
    p.add_argument("--pkt-size", type=int, default=1500,
                   help="L2 frame size in bytes for throughput calculation")
    p.add_argument("--link-mbps", type=float, default=10.0)
    p.add_argument("--title", default=None)
    args = p.parse_args()

    trace = load(args.log)
    stats = summarize(trace, pkt_size_bytes=args.pkt_size)
    print("== summary ==")
    print(json.dumps(stats, indent=2))

    bin_t, tput = throughput_mbps(trace, args.bin_s, args.pkt_size)
    _, mfrac = mark_fraction(trace, args.bin_s)

    fig, axs = plt.subplots(4, 1, figsize=(11, 11), sharex=True)

    axs[0].plot(bin_t, tput, lw=1.2)
    axs[0].axhline(args.link_mbps, ls="--", color="gray",
                   label=f"link cap {args.link_mbps:g} Mbps")
    axs[0].set_ylabel("Throughput (Mbps)")
    axs[0].set_ylim(bottom=0)
    axs[0].grid(True, alpha=0.3)
    axs[0].legend(loc="lower right")

    axs[1].plot(trace.t_ack, trace.rtt_us / 1000.0, lw=0.6, alpha=0.6)
    axs[1].set_ylabel("RTT (ms)")
    axs[1].set_ylim(bottom=0)
    axs[1].grid(True, alpha=0.3)

    axs[2].plot(trace.t_ack, trace.w_ack, lw=0.8, color="tab:purple")
    axs[2].set_ylabel("W (packets)")
    axs[2].set_ylim(bottom=0)
    axs[2].grid(True, alpha=0.3)

    if trace.algo == "dctcp":
        axs[3].plot(trace.t_ack, trace.alpha_ack, lw=0.9,
                    color="tab:red", label="α (DCTCP EWMA)")
        ax3b = axs[3].twinx()
        ax3b.plot(bin_t, mfrac, color="tab:orange", lw=0.8,
                  alpha=0.6, label="mark fraction (100 ms)")
        axs[3].set_ylabel("α", color="tab:red")
        ax3b.set_ylabel("mark fraction", color="tab:orange")
        axs[3].set_ylim(0, 1)
        ax3b.set_ylim(0, 1)
        algo_summary = (
            f"mark {stats.get('mark_frac', 0) * 100:.1f}%, "
            f"α {stats.get('alpha_mean', 0):.3f}"
        )
    else:  # hpcc
        axs[3].plot(trace.t_ack, trace.u_ack, lw=0.4, alpha=0.4,
                    color="tab:gray", label="u (per ACK)")
        axs[3].plot(trace.t_ack, trace.u_smoothed_ack, lw=1.0,
                    color="tab:red", label="U_smoothed")
        axs[3].axhline(0.99, ls="--", color="black", alpha=0.4, label="η=0.99")
        axs[3].set_ylabel("U")
        axs[3].set_ylim(0, max(3.0, float(np.percentile(trace.u_ack, 99)) * 1.1))
        axs[3].legend(loc="upper right", fontsize=8)
        algo_summary = (
            f"U_sm mean {float(np.mean(trace.u_smoothed_ack)):.2f}, "
            f"hops/ACK {int(np.median(trace.hop_count))}"
        )

    axs[3].set_xlabel("Time (s)")
    axs[3].grid(True, alpha=0.3)

    title = args.title or (
        f"E1 {trace.algo.upper()} — {stats.get('n_ack', 0)} ACKs, "
        f"throughput {stats.get('throughput_mbps', 0):.2f} Mbps, "
        f"RTT p99 {stats.get('rtt_us_p99', 0) / 1000:.1f} ms, {algo_summary}"
    )
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(args.out, dpi=120)
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
