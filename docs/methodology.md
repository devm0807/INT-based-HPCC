# Methodology

This document records the deliberate design choices and the software
artifacts you'd need to know to evaluate, extend, or contest the results.

## What is and isn't a reproduction

**Is**: a single-machine, software-only re-creation of the HPCC control
loop (Algorithm 1, SIGCOMM 2019) using BMv2 as the programmable data
plane and Python for the sender/receiver. DCTCP is reproduced as a
simplified ECN-AIMD scheme over the same UDP+ACK harness.

**Is not**: a faithful reproduction of the paper's quantitative results.
The paper runs at 100 Gbps in NIC hardware with µs-scale RTTs and
fractional-MTU window updates; we run at 10 Mbps in software with ~6 ms
RTTs and packet-granular window updates. Some HPCC behaviors that are
near-zero overhead at line rate become structurally limiting at our
scale (see [results.md](results.md)). This is the trade we made when
the professor approved framing it as "implementation for our own
edification."

## Deliberate simplifications

| Decision | Why |
|---|---|
| UDP+ACK for both DCTCP and HPCC (not real Linux TCP DCTCP) | Apples-to-apples comparison — any difference is from the control loop, not the transport stack. Real DCTCP atop TCP would confound the comparison. |
| Single Python sender process per host (no SACK, go-back-N retransmit) | At 10 Mbps with 1500 B packets, drops are rare; the simpler retransmit logic doesn't materially affect steady-state behavior we're measuring. |
| Thrift CLI for table population (not P4Runtime) | `simple_switch_grpc` speaks both. Thrift is a 4-line script per switch with no proto generation. Migrate to P4Runtime only if we hit limits. |
| BMv2's `set_queue_rate` as the bottleneck (not TCLink `bw`) | Required so `enq_qdepth` in P4 reflects the real congested queue. Otherwise the queue forms in tc qdisc downstream and BMv2 sees instantaneous queue depth = 0. |
| HPCC `η=0.99`, `W_AI=3` (paper defaults 0.95, 1) | At BDP=5 packets, the qdepth term in U is so dominant that paper defaults under-utilize. See "Tuning" below. |
| INT header in UDP payload, port-discriminated (`dst_port == 50000`) | Keeps receiver on plain UDP sockets; no raw-IP parsing. Switches only insert INT on data, not ACKs. |
| `MAX_HOPS = 4` (we use 2) | Bounds the P4 parser to keep compile fast; we have head room if we add a leaf-spine. |
| Receiver treats INT as opaque bytes | Means the same reflector serves both algorithms; less code to keep in sync with the wire format. |
| 8 senders in topology even when only 1-2 are used | Avoids the Mininet `Topo` rebuild overhead per experiment. Idle hosts cost ~10 ms of startup time each. |

## Software artifacts you will trip on

### veth UDP checksum offload

When a Mininet host sends a UDP packet via veth, the kernel sets
`CHECKSUM_PARTIAL` on the skb. BMv2 reads the raw bytes via pcap and
copies them to the peer veth without finalizing the checksum. The
receiving veth's kernel sees an invalid UDP checksum and silently
drops the packet. Symptom: ping works (ICMP checksum is computed
in software), UDP gets 0 ACKs back.

**Fix**: `ethtool --offload <iface> tx off sg off tso off gso off
gro off lro off ufo off` on **every** veth — host-side AND
switch-side (which live in the root netns). Done in
[topo/dumbbell.py](../topo/dumbbell.py) `configure_hosts`.

### tc qdisc vs BMv2 queue

Mininet's `TCLink(bw=10)` enforces the rate via tc qdisc on the egress
veth. With this enabled, BMv2 dequeues at unlimited rate and writes
to the veth; the kernel's qdisc holds the packets and drops them on
overflow. BMv2's `std_meta.enq_qdepth` always sees 0 because its
internal queue is empty.

**Fix**: drop `bw` from the bottleneck TCLink (keep `delay`), and
use BMv2's `set_queue_rate <pps> <port>` thrift command to make
BMv2 itself the rate limiter. Done in
[controller/load_tables.py](../controller/load_tables.py). At
~1500 B/packet, `set_queue_rate 833 9` ≈ 10 Mbps on s1's port 9
(the bottleneck egress).

### Stale Mininet interfaces between runs

A crashed Mininet leaves veth pairs in the root namespace. The next
`net.start()` fails with `Error creating interface pair ... File
exists`.

**Fix**: every experiment driver calls
[`topo.dumbbell.cleanup_mn()`](../topo/dumbbell.py) at startup,
which runs `mn -c && pkill -f simple_switch`. Idempotent.

### HPCC update gate fires too often when W shrinks

Pure ack-clocked gate (`cum_acked ≥ last_update_seq + W`) means: when
W shrinks to 2, the next update fires after only 2 more ACKs. That's
~2.5 ms in our setup, well below baseRTT. Multiplicative decrease
fires repeatedly within one congestion epoch, driving W to W_min.

**Fix**: gate by BOTH `cum_acked ≥ last + W` AND `wallclock ≥
last_time + baseRTT`. Caps the update rate at one per baseRTT,
which is what the paper's algorithm actually intends.

### tx_byte_count overflow / counter resets across runs

BMv2 registers persist across `simple_switch_CLI` connects but
**don't** persist across switch restarts. Our HPCC sender computes
`txRate = ΔB / Δt` from successive INT samples; a counter reset
between runs gives a negative ΔB. We guard against negative ΔB and
just skip that sample.

## Tuning rationale

The HPCC plan defaults (η=0.95, W_AI=1, τ/T=0.2) were derived for a
100 Gbps environment with BDP ≈ 417 MTUs. Our setup has BDP = 5 MTUs.
The fixed-point analysis:

```
W* = W_AI / (1 − η/U_eq)
```

`U_eq` at steady state ≈ `qdepth_bytes/B + tx_rate/link_bps`. At
saturated link with even 1 packet of queue: `U_eq ≈ 0.2 + 1.0 = 1.2`.
Plugging in:

| W_AI | η | W* (theoretical) | Observed steady W | Throughput |
|---|---|---|---|---|
| 1 | 0.95 | ≈ 4.8 | 4.7 | 7.2 Mbps |
| 1 | 0.99 | ≈ 4.7 | 4.9 | 7.0 Mbps |
| 3 | 0.99 | ≈ 14.3 | 5.6 (clamped to W_max?) | **9.5 Mbps** |

The W_AI=3 setting effectively raises the recovery rate after MD so
the sender spends more wall-clock time near BDP. We don't actually
hit `W ≈ 14` because U_eq drifts above 1.2 once queue grows; the
controller settles around `W ≈ 5–6` packets in practice.

We did NOT touch DCTCP's tuning — `K=5 packets` (≈ BDP), `g=1/16`,
W_init=4 — matches the plan defaults and gives clean sawtooth.

## What we'd do differently in a re-implementation

1. **Bigger BDP from the start**. Either 100 Mbps × 5 ms = 50-packet
   BDP (needs a C sender to keep up), or 10 Mbps × 50 ms RTT =
   50-packet BDP (but artificial delay hurts user feedback time).
   Either makes HPCC's qdepth contribution non-dominant.
2. **Float-granular window**. Currently `len(in_flight) < int(W)` —
   we lose sub-packet precision in growth. A probabilistic send gate
   would let W_AI < 1.
3. **Real W_ref / incarnation snapshot per packet**. Currently
   `self.w_ref` is just the W after the last update. Should be the
   W at the time the EARLIEST UNACKED packet was sent, frozen until
   that packet's ACK arrives. Matters for short flows where multiple
   MD cuts within one RTT cause overshoot.
4. **C sender**. The Python token-bucket + GIL combination caps us at
   ~5 kpps. C with `sendmmsg()` could push 100k+ pps and let us
   actually run at 100 Mbps or higher.
5. **In-data-plane queue snapshot register**. Currently we sample
   qdepth only via INT entries (per-packet). For ground-truth
   queue plots we'd want a control-plane thread reading a
   `qdepth_history` register array indexed by time. Skipped for
   week 5; would be a fast addition.

## Threats to validity

- All measurements are from a single machine (Apple M-series under
  Rosetta or native Linux). BMv2's CPU consumption distorts timing
  at high load. We mitigate by pinning to specific CPU cores in the
  Docker container; results are stable run-to-run within ~5%.
- Sender RTT measurements include Python event-loop latency
  (~100–500 µs). Baseline RTT measurement averages this out, but
  short-burst latency (E3 incast) is more sensitive.
- Switch counters (`marked_pkt_count`, `data_pkt_count`) use
  non-atomic `register.read + register.write` in v1model and may
  undercount under high concurrency. At our rates the error is < 1%.
