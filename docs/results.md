# INT-CC results — HPCC vs DCTCP on BMv2/Mininet

Software-only reproduction of HPCC and DCTCP over a UDP+ACK harness on
the two-switch dumbbell defined in [topo/dumbbell.py](../topo/dumbbell.py).

Bottleneck: 10 Mbps (BMv2 `set_queue_rate 833`), 1 ms one-way delay,
buffer 40 packets, base RTT ≈ 6 ms.

Reproduce with `make e1 e2 e3 e4 compare`.

## E1 — single long flow (60 s)

| | DCTCP | HPCC |
|---|---|---|
| Throughput | 9.99 Mbps | 9.54 Mbps |
| RTT p50 | 10.9 ms | 6.1 ms |
| RTT p99 | 14.3 ms | 8.4 ms |
| Window (mean) | 9.88 pkt | 5.59 pkt |
| Mark fraction | 24.2% | n/a |

HPCC delivers 95.5% of DCTCP's throughput at 41% lower p99 latency —
the canonical HPCC trade-off. Throughout E1 HPCC's window stays in a
narrow 4–8 packet band while DCTCP's sawtooth between ~7 and ~13.

## E2 — two flows, h2 joins 5 s after h1 (60 s)

| | DCTCP | HPCC |
|---|---|---|
| h1 steady | 5.02 Mbps | 4.88 Mbps |
| h2 steady | 4.98 Mbps | 5.12 Mbps |
| Sum | 10.00 Mbps | 10.00 Mbps |
| Jain | 0.9999 | 0.9994 |

Both algorithms achieve near-perfect fairness, well above the 0.95
target. The bottleneck is saturated in both cases. HPCC takes a few
RTTs longer to converge after h2 joins (visible in the per-flow
throughput plot) but settles cleanly.

## E3 — incast: 8 short flows × 100 KB, 5 rounds (40 flows total)

| | DCTCP | HPCC |
|---|---|---|
| FCT mean | 679 ms | 1223 ms |
| FCT p50 | 687 ms | 1245 ms |
| FCT p99 | 725 ms | 1319 ms |

**DCTCP wins incast in our setup.** HPCC's conservative steady-state
window (~3 packets per flow under heavy contention) leaves the
bottleneck pipe under-utilized when 8 flows compete — total in-flight
~24 packets vs ideal ~40. DCTCP's larger sawtooth tolerates the
overshoot and fills the buffer, completing flows faster.

This inverts the HPCC paper's result; the gap is from our software-only
setting: small BDP (≈5 packets), bursty BMv2 dequeue, and a sender pacing
overhead (Python token bucket) that the paper's hardware NIC avoids. With
W_AI tuned higher specifically for incast, HPCC could likely close most
of the gap.

## E4 — flow dynamics (3 flows join at t=0, 20, 40 s)

Convergence plots in [results/e4_dctcp.png](../results/e4_dctcp.png) and
[results/e4_hpcc.png](../results/e4_hpcc.png).

Both algorithms reach fair sharing after each new flow joins. DCTCP shows
visible RTT spikes at each join (buffer briefly overshoots before α
catches up); HPCC's RTT stays nearly flat through the joins, again
reflecting its low-queue operating point.

## Tuning that made it work

HPCC's defaults (η=0.95, W_AI=1, τ/T=0.2) under-utilize the link at
10 Mbps because BDP=5 packets means even 1 packet of queue pushes
u well above η. Bumped to:

| Param | Plan | Used | Why |
|---|---|---|---|
| η | 0.95 | 0.99 | small-BDP setup needs a more aggressive target |
| W_AI | 1 | 3 | fixed-point analysis: W* = W_AI/(1 − η/U_eq); W_AI=1 → W*~3 (below BDP) |
| τ/T | 0.2 | 0.05 | per-packet tx_rate samples are bursty at low link rate |

DCTCP runs with plan defaults (K=5, g=1/16). All in [PLAN.md](../PLAN.md).

## Known software artifacts

- **veth checksum offload**: must be disabled on every interface
  (host- and switch-side veth peers), otherwise BMv2 forwards packets
  with CHECKSUM_PARTIAL markers and the destination kernel silently
  drops as bad UDP csum. Fix in [topo/dumbbell.py](../topo/dumbbell.py)
  `configure_hosts` → `ethtool --offload <iface> ... off`.
- **BMv2 vs tc qdisc rate limiting**: TCLink's `bw=10mbit` puts the
  queue in tc, leaving BMv2's enq_qdepth at 0. Solution: drop `bw`
  from TCLink, use BMv2's `set_queue_rate` on the bottleneck egress.
- **HPCC update gate**: ack-clocked alone (`cum_acked ≥ last + W`)
  fires arbitrarily often when W shrinks, causing oscillation. Plan
  was clarified to gate on both ack-window AND wall-clock baseRTT;
  see [sender/hpcc_sender.py](../sender/hpcc_sender.py)
  `_maybe_update_window_locked`.
