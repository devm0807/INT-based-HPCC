"""
Read a sender CSV log and produce time-series arrays for plotting and
metric computation.

The log schema is the one DctcpSender / HpccSender writes:
    ts_us, event, seq, rtt_us, ecn_echo, w, alpha, cum_acked, in_flight

Events:
    send    - data packet sent for the first time
    rtx     - data packet retransmitted (RTO)
    ack     - ACK received
    update  - per-RTT window update fired
    rto     - retransmission timeout fired
"""
from __future__ import annotations

import csv
from dataclasses import dataclass

import numpy as np


@dataclass
class Trace:
    """Sender per-event trace.

    Supports both the DCTCP schema (with ecn_echo / alpha columns) and the
    HPCC schema (with hop_count / u / u_smoothed / w_ref / incarnation
    columns). Whichever columns aren't present in the source CSV land as
    zero-filled arrays so downstream code can probe `algo` to decide what
    to plot.
    """
    # Per-event arrays.
    t_s: np.ndarray
    event: np.ndarray

    # ACK-only arrays (always present).
    t_ack: np.ndarray
    rtt_us: np.ndarray
    w_ack: np.ndarray

    # DCTCP-specific (zeros for HPCC traces).
    ecn_echo: np.ndarray
    alpha_ack: np.ndarray

    # HPCC-specific (zeros for DCTCP traces).
    hop_count: np.ndarray
    u_ack: np.ndarray
    u_smoothed_ack: np.ndarray

    t_send: np.ndarray
    algo: str               # "dctcp" or "hpcc" — inferred from columns
    duration_s: float

    @property
    def n_ack(self) -> int:
        return int(self.t_ack.size)


def load(path: str) -> Trace:
    rows: list[dict] = []
    with open(path) as f:
        rdr = csv.DictReader(f)
        rows.extend(rdr)
    if not rows:
        raise ValueError(f"empty log: {path}")
    cols = set(rows[0].keys())
    algo = "hpcc" if "u_smoothed" in cols else "dctcp"

    t0 = int(rows[0]["ts_us"])
    t_s = np.array([(int(r["ts_us"]) - t0) / 1e6 for r in rows])
    event = np.array([r["event"] for r in rows])

    ack_mask = event == "ack"
    send_mask = np.isin(event, ["send", "rtx"])

    def col_or_zero(name: str, mask, dtype=float) -> np.ndarray:
        if name in cols:
            return np.array([dtype(r[name]) for r in rows])[mask]
        return np.zeros(int(mask.sum()), dtype=dtype)

    return Trace(
        t_s=t_s,
        event=event,
        t_ack=t_s[ack_mask],
        rtt_us=col_or_zero("rtt_us", ack_mask),
        w_ack=col_or_zero("w", ack_mask),
        ecn_echo=col_or_zero("ecn_echo", ack_mask, dtype=int),
        alpha_ack=col_or_zero("alpha", ack_mask),
        hop_count=col_or_zero("hop_count", ack_mask, dtype=int),
        u_ack=col_or_zero("u", ack_mask),
        u_smoothed_ack=col_or_zero("u_smoothed", ack_mask),
        t_send=t_s[send_mask],
        algo=algo,
        duration_s=float(t_s[-1]) if len(t_s) else 0.0,
    )


def throughput_mbps(trace: Trace, bin_s: float, pkt_size_bytes: int) -> tuple[np.ndarray, np.ndarray]:
    """Goodput in Mbps using ACK arrival times and a nominal frame size."""
    if trace.n_ack == 0:
        return np.array([]), np.array([])
    t_end = max(trace.t_ack[-1], trace.duration_s)
    n_bins = max(1, int(t_end / bin_s) + 1)
    counts = np.zeros(n_bins)
    idx = np.clip((trace.t_ack / bin_s).astype(int), 0, n_bins - 1)
    np.add.at(counts, idx, 1)
    bin_t = np.arange(n_bins) * bin_s
    bps = counts * pkt_size_bytes * 8 / bin_s
    return bin_t, bps / 1e6


def mark_fraction(trace: Trace, bin_s: float) -> tuple[np.ndarray, np.ndarray]:
    if trace.n_ack == 0:
        return np.array([]), np.array([])
    t_end = max(trace.t_ack[-1], trace.duration_s)
    n_bins = max(1, int(t_end / bin_s) + 1)
    counts = np.zeros(n_bins)
    marks = np.zeros(n_bins)
    idx = np.clip((trace.t_ack / bin_s).astype(int), 0, n_bins - 1)
    np.add.at(counts, idx, 1)
    np.add.at(marks, idx, trace.ecn_echo)
    frac = np.divide(marks, counts, out=np.zeros_like(marks), where=counts > 0)
    return np.arange(n_bins) * bin_s, frac


def summarize(trace: Trace, pkt_size_bytes: int = 1500,
              warmup_s: float = 5.0) -> dict:
    """Steady-state stats from a long-flow trace."""
    if trace.n_ack == 0:
        return {"n_ack": 0}
    mask = trace.t_ack >= warmup_s
    rtt = trace.rtt_us[mask]
    ecn = trace.ecn_echo[mask]
    w = trace.w_ack[mask]
    alpha = trace.alpha_ack[mask]

    # Throughput over the steady-state window only.
    t_ack_warm = trace.t_ack[mask]
    if t_ack_warm.size == 0:
        return {"n_ack": int(trace.n_ack), "steady_n_ack": 0}
    span = t_ack_warm[-1] - t_ack_warm[0]
    tput_mbps = (t_ack_warm.size * pkt_size_bytes * 8) / max(span, 1e-6) / 1e6

    return {
        "n_ack": int(trace.n_ack),
        "steady_n_ack": int(t_ack_warm.size),
        "throughput_mbps": float(tput_mbps),
        "rtt_us_mean": float(np.mean(rtt)),
        "rtt_us_p50":  float(np.percentile(rtt, 50)),
        "rtt_us_p99":  float(np.percentile(rtt, 99)),
        "mark_frac":   float(np.mean(ecn)),
        "w_mean":      float(np.mean(w)),
        "w_p50":       float(np.percentile(w, 50)),
        "w_p99":       float(np.percentile(w, 99)),
        "alpha_mean":  float(np.mean(alpha)),
    }
