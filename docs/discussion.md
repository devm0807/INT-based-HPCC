# Discussion — Is INT favorable, and why we can't fully replicate HPCC

This document is the "what does it all mean" companion to [results.md](results.md)
(the numbers) and [methodology.md](methodology.md) (the how). Use this for
the writeup / slide deck framing.

## The takeaway, in one sentence

**INT-based congestion control is favorable in the regime it was designed
for — large BDP, line-rate hardware, microsecond RTTs — but its precision
becomes a liability when you scale it down to small-BDP software, and our
experiments demonstrate exactly that failure mode.**

## What we actually proved

| Claim from the HPCC paper | Our result | Verdict |
|---|---|---|
| Near-zero queueing | RTT p99 **8.4 ms** vs DCTCP **14.3 ms** (41% lower) | ✅ confirmed |
| Comparable throughput | **9.54 Mbps** vs DCTCP **9.99 Mbps** (within 5%) | ✅ confirmed |
| Stable W (low variance) | W stdev **0.59** vs DCTCP **0.84** (sawtooth) | ✅ confirmed |
| Faster incast FCT | DCTCP p99 **725 ms** vs HPCC **1319 ms** | ❌ **inverted** |
| Fast convergence after flow joins | both converge; HPCC keeps RTT flat | ≈ tied |

3.5 out of 5 of the paper's claims hold up in software. The interesting
failure (incast) is what tells us where INT actually buys you something
vs where it doesn't.

## Is INT favorable?

It depends on what you're optimizing for. Three regimes:

### When INT wins (paper's home turf, and our E1)
- Latency-sensitive workloads at high speed
- Single or few concurrent flows per bottleneck
- Large BDPs (≥100 MTU)
- You can tolerate ~5% throughput loss in exchange for half the queueing delay

**Mechanism**: INT gives you `qdepth + utilization + tx_rate` per hop.
You can drive utilization to exactly η < 1 because you see the queue
building before it overflows. ECN only tells you *after* the queue is
full enough to mark.

### When INT loses to ECN (our E3)
- Many concurrent short flows sharing a small-BDP bottleneck
- Hardware-paced senders missing (Python / userspace)
- Bursty traffic where instantaneous queue swings dominate the qdepth signal

**Mechanism**: HPCC's `U = qdepth/B + util` means even *one packet of
queue* drags U above η when BDP is 5 packets. The controller responds
by shrinking W. With 8 flows each holding 3 packets, the bottleneck sits
at 60% utilization. DCTCP's "did anyone mark me this RTT?" signal doesn't
care — it grows the window until it gets marked, fills the buffer
aggressively, drains 8 × 100 KB faster.

### The honest framing
INT gives you *more information* per ACK; it doesn't automatically give
you *better control*. The control loop that consumes the information has
to be tuned for the regime. **The paper's HPCC algorithm is tuned for one
regime; for ours, DCTCP's coarser signal happens to be the right level
of abstraction.**

## Why we can't replicate the HPCC paper in detail

Six independent gaps between our setup and the paper's. Each compresses
the regime where INT shines, and they compound multiplicatively.

### 1. Bandwidth scale (10,000×)
- **Paper**: 100 Gbps. Each MTU is `1500 B × 8 / 1e11 = 120 ns` on the wire.
- **Ours**: 10 Mbps. Each MTU is `1500 B × 8 / 1e7 = 1.2 ms` on the wire.

A "packet" is the same byte count but the time it occupies on the link
is 10,000× longer for us. That changes everything downstream — RTT, queue
dynamics, control-loop latency.

### 2. BDP in packet units (80×)
- **Paper**: BDP = `100 Gbps × 50 µs / 8 = 625 KB = 417 MTUs`. A 1-MTU
  queue is **0.24%** of BDP.
- **Ours**: BDP = `10 Mbps × 6 ms / 8 = 7.5 KB = 5 MTUs`. A 1-MTU queue
  is **20%** of BDP.

This is *the* killer for HPCC. The U formula has `qdepth_bytes / B` as
one term. For the paper, this term is rounding noise. For us, it's the
dominant signal even at minimum queue depth. We literally cannot operate
"near zero queue" because zero is a discrete packet boundary that's a
full 20% of BDP away.

### 3. W_AI granularity (15–20×)
- **Paper**: `W_AI ≈ 80 bytes` per RTT. That's `80/1500 ≈ 0.053 MTU` —
  fractional packet additive increase.
- **Ours**: `W_AI ≥ 1 packet` per RTT (we use 3). Integer comparisons
  in the send-while loop mean we can't actually use 0.05.

Why this matters: HPCC steady-state W is `W_AI / (1 − η/U_eq)`. The
paper picks W_AI specifically so that, given their U_eq, W* lands at
full BDP. We can't tune W_AI that finely; we either overshoot (W_AI=3
lands W around 5-6 packets when it should be 4) or undershoot (W_AI=1
lands W at 3).

### 4. Sender hardware (1000×+ in pacing precision)
- **Paper**: HPCC runs in the NIC's hardware. It paces packets at line
  rate with sub-µs accuracy. The window controller and the packet
  emitter share a clock.
- **Ours**: Python in user space. `time.sleep(0.0005)` has ~50–500 µs
  jitter. The GIL serializes the ack listener thread with the main send
  loop. We can sustain maybe 5 kpps before pacing collapses.

Implication: even when the controller computes "send another packet now,"
there's a 50–500 µs window where Python is doing something else. At our
833 pps the per-packet interval is 1.2 ms — so 5–40% of every interval
is pure scheduling noise. The control loop *can't* react faster than
the noise floor.

### 5. RTT and EWMA constants
- **Paper**: RTT ≈ 50 µs. Their EWMA constants (`τ/T = 0.2`) average
  over ~5 ACKs ≈ 250 µs — fast enough to track real congestion changes.
- **Ours**: RTT ≈ 6 ms. `τ/T = 0.2` still averages over ~5 ACKs but now
  that's ~30 ms — slow enough that the queue can fill and drain twice
  while U_smoothed catches up.

We compensated by dropping τ/T to 0.05 (smoothing over ~20 ACKs ≈ 120 ms),
which reduced variance but didn't restore the paper's regime — the
constants don't time-scale the same way.

### 6. Hardware feedback loop, no kernel
- **Paper**: The NIC reads INT directly from the L2 frame. ACKs are
  generated by the receiver NIC. End-to-end, the control loop never
  traverses the kernel.
- **Ours**: Every packet goes through h1's kernel UDP stack, two BMv2
  userspace processes, r1's kernel, back through h1's kernel. Each
  crossing adds variable latency (CPU load, interrupt coalescing,
  sched_yield).

Effect: noise in every ACK's `rtt_us` and `egress_tstamp_us`. The
tx_rate computation `(Δbytes × 8 / Δt)` is especially sensitive — if
Δt is jittery by 200 µs and Δbytes is small (e.g., 1 packet at our
rate), instantaneous tx_rate spikes to ~12 Mbps between two consecutive
ACKs even when the long-run rate is 10 Mbps. The EWMA smooths this but
at the cost of responsiveness.

## What it would take to actually reproduce

Each gap above is fixable; the question is engineering cost.

| Gap | Fix | Effort |
|---|---|---|
| BDP scale | Run at 1 Gbps × 1 ms = 80-pkt BDP | Need C sender; BMv2 also tops out around 100 Mbps in CPU |
| W_AI granularity | Float W, probabilistic send gate | ~50 lines of code in `hpcc_sender.py` |
| Sender hardware | C sender with `sendmmsg()`, busy-loop pacing, pinned core | ~500 lines |
| Kernel feedback noise | DPDK / raw socket, bypass kernel UDP | Significant — rewrite UDP layer |
| Per-flow snapshot W_ref | Implement proper incarnation tagging per packet | ~100 lines |

The MOST impactful single change would be **moving to a C sender with
raw sockets at 1 Gbps**. That alone fixes gaps 1, 2, 3, 4 simultaneously.
With BDP = 80 packets, even with our Python control logic the qdepth
term in U becomes manageable (1-packet queue = 1.25% of BDP), and HPCC's
incast advantage should reappear.

But there's an important meta-point: **we can't fully reproduce because
BMv2 is a software emulator running on a single CPU**. There's no
realistic path to 100 Gbps with BMv2; that's why production INT
deployments use Tofino ASICs. The honest framing — and what the
professor approved when he said "implementation for your own
edification" — is:

> We demonstrate HPCC's underlying mechanism works correctly in software
> (INT injection + controller adapting W to U) and validate its
> single-flow latency claim. Reproducing the paper's quantitative incast
> advantage requires hardware that operates outside the regime where
> INT's strengths surface; in our regime, ECN-based DCTCP happens to be
> the better-tuned algorithm for the same task.

That's the real takeaway. INT-based control isn't a free win — it's a
precision instrument that needs the right environment to outperform a
blunter instrument like ECN.
