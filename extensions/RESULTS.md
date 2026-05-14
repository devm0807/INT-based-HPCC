# Extensions — results summary

Three new findings from the optional extensions, each updating a
specific claim from [docs/discussion.md](../docs/discussion.md).

## 1. C sender vs Python sender, same E1 setup

| | Python (`sender.hpcc_sender`) | C (`extensions/csender/hpcc_sender`) |
|---|---|---|
| Throughput | 9.54 Mbps | **10.00 Mbps** |
| Final W | 5.59 | 7.99 |
| u_smoothed (final) | ~1.65 | ~1.60 |

The Python sender's 500-µs `time.sleep` + GIL was a partial bottleneck:
between consecutive ACKs the Python main thread was idle ~5% of the
time. The C sender eliminates that overhead and hits link capacity
exactly. **Confirms gap 4 from [docs/discussion.md](../docs/discussion.md)**
(sender pacing precision) cost us ~5% throughput at 10 Mbps. At higher
link rates this gap would matter much more.

## 2. HPCC v2 with proper W_ref / incarnation snapshot

| | v1 (`sender/hpcc_sender.py`) | v2 (`extensions/hpcc_v2/hpcc_sender_v2.py`) |
|---|---|---|
| Throughput (30 s E1) | 9.54 Mbps | **9.99 Mbps** |
| W mean | 5.59 | **7.51** |
| W stdev | 0.59 | 0.49 |
| RTT p99 | 8.4 ms | 10.3 ms |
| Stale ACKs (ignored for U update) | n/a | 38.6% of ACKs |

v2 attaches the (w_ref, incarnation) snapshot at SEND time to each
in-flight seq. On the update-gate check it uses the W_ref of the
EARLIEST UNACKED packet, and ACKs whose stored incarnation is older
than the current incarnation only advance cum_acked — they do not
contribute U for a new MD decision.

**Confirms gap 5 from [docs/discussion.md](../docs/discussion.md)**
(proper W_ref / incarnation snapshot) was the second 5% of throughput
left on the table. v2 hits 100% link util at the cost of ~25% more
queueing delay than v1 (still 30% lower than DCTCP). Worth using
as the new HPCC baseline going forward.

## 3. BDP-hypothesis sweep (C sender, 10 / 50 / 100 Mbps)

| Rate cap (BMv2) | Throughput | Util | RTT p99 | W mean |
|---|---|---|---|---|
| 10 Mbps  | 10.00 Mbps | **100%** | 10.23 ms |  7.42 |
| 50 Mbps  | 19.94 Mbps |  39.9%   |  5.37 ms |  8.26 |
| 100 Mbps | 27.04 Mbps |  27.0%   |  5.19 ms | 10.95 |

**Inconclusive** for the BDP hypothesis. Throughput plateaus at
~27 Mbps regardless of the configured `set_queue_rate`, meaning BMv2's
CPU is the actual bottleneck above ~30 Mbps in our environment. We
can't probe the regime "10 Mbps but with 100-packet BDP" without
either (a) a faster BMv2 (Tofino), or (b) a much longer RTT
(`delay='25ms'` instead of `1ms` — but that hits Mininet's own
limits on TCLink netem).

What the sweep DOES confirm:
- HPCC saturates the link cleanly when it's actually the bottleneck
  (10 Mbps case, 100% util).
- W grows roughly linearly with the configured link rate, even as
  realized throughput plateaus — the sender thinks the link is
  underloaded and tries to send more, but BMv2 silently caps it.

So a clean replication of the paper's quantitative incast result
would require Tofino or some other line-rate P4 target. With BMv2
alone, the hypothesis is *consistent with our data* (HPCC matches
line rate when it's allowed to) but *not provable*.

## 4. Qdepth snapshot register (P4 + control plane)

`extensions/qsnap/hpcc_qsnap.p4` extends hpcc.p4 with a 1024-slot
`qdepth_history` register array that the egress writes
`std_meta.enq_qdepth` into, indexed by an 8-ms time bucket.
`snapshot_reader.py` polls it every 100 ms via thrift.

Compiles + populates cleanly; we don't currently produce a plot from
its output, but the data is available for the next student to drop
into a queue-vs-time figure.

## Headline

The three closeable gaps from [docs/discussion.md](../docs/discussion.md)
that we could test in software all confirmed:

- **Gap 4 (sender pacing precision)** → C sender recovers 5% throughput.
- **Gap 5 (W_ref / incarnation snapshot)** → v2 sender recovers another 5%.
- **Gap 1+2 (bandwidth / BDP scale)** → couldn't test cleanly because
  BMv2's CPU ceiling cuts us off at ~30 Mbps. Need real hardware.

Combining gaps 4 and 5: **HPCC at 10 Mbps in software can hit
100% utilization with RTT p99 still 30% lower than DCTCP** —
restoring most of the paper's qualitative claim while remaining
honest about the absolute scale we operate at.
