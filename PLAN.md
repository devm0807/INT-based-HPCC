# INT-Based Congestion Control on P4/BMv2 — Implementation Plan

## Context

Six-week, three-person project to implement a software prototype of HPCC (High Precision Congestion Control, SIGCOMM 2019) on BMv2 + Mininet, with a simplified DCTCP as the primary baseline. The goal is *educational reproduction*, not a new algorithm: demonstrate that in-band telemetry from a programmable switch yields lower queueing and faster convergence than ECN-only feedback, using only open-source tools.

The professor's feedback steers us toward a real congestion-responsive baseline (DCTCP, with AIMD as a fallback) instead of a trivial constant-rate UDP straw man. To keep the comparison clean, both algorithms run over the **same UDP+ACK harness** so any difference observed is from the control loop, not the transport.

Repo is currently empty (`.git` only). Week 1 includes a fresh BMv2/p4c/Mininet/P4Runtime install.

## Confirmed Decisions

- **DCTCP harness**: UDP+ACK with ECN echo (apples-to-apples with HPCC). The receiver echoes the IP ECN bit in ACKs; the sender runs the DCTCP α-EWMA + AIMD update over those ACKs. Documented as a deliberate simplification.
- **Topology scope**: two-switch dumbbell only. Two BMv2 switches in series so HPCC's per-hop `max_i` actually has multiple hops. No leaf-spine.
- **Sender/receiver language**: Python, pinned to dedicated cores; pre-built packet templates; escape-hatch to C only if pacing breaks above ~5 kpps.
- **Environment**: fresh install in Week 1; target a Vagrant box or Docker image so all three teammates work on the same toolchain.

## Architecture

### P4 programs — two separate programs sharing common headers

- `p4src/common/headers.p4` — Ethernet, IPv4, UDP, INT shim, INT hop entry. Single source of truth for the wire format.
- `p4src/common/parsers.p4` — shared parser fragments.
- `p4src/hpcc.p4` — egress pipeline pushes a per-hop INT entry (`switch_id`, `qdepth`, `egress_tstamp_us`, `tx_byte_count`, `link_bps`); never marks ECN.
- `p4src/dctcp.p4` — egress pipeline marks `ipv4.ecn = CE` when `enq_qdepth > K`; never inserts INT.

Switch model: `simple_switch_grpc` for both, populated via P4Runtime so the same control-plane code works for both programs.

### INT wire format (frozen — see [docs/int_header_spec.md](docs/int_header_spec.md))

INT lives **inside the UDP payload**, not between IPv4 and UDP. Discrimination is by UDP destination port: `50000` for data, `50001` for ACK; switches insert INT only on `dst_port == 50000`. (The earlier draft said `ipv4.protocol = 0xFD/0xFE`, but that would force the receiver to use raw IP sockets — port-based discrimination keeps the host stack as plain UDP.)

Shim header (4 bytes): `flags:8` (bit 0 = ECN_ECHO), `hop_count:8`, `max_hops:8` (=4), `reserved:8`.

Per-hop entry (26 bytes): `switch_id:16`, `ingress_port:16`, `egress_port:16`, `qdepth:32` (BMv2 packet units), `egress_tstamp_us:48`, `tx_byte_count:48`, `link_bps:32`. Sum = 208 bits = 26 B (the earlier "22 B" was a miscount).

Pinned byte-for-byte by [p4src/common/headers.p4](p4src/common/headers.p4), [sender/packet_format.py](sender/packet_format.py), and [tests/test_packet_format.py](tests/test_packet_format.py).

ACKs reflect INT bytes verbatim; the data plane does NOT add INT to ACKs. The receiver treats the INT block as opaque bytes — no parsing on the receiver host.

### Senders, receiver, control plane

- `sender/hpcc_sender.py` — windowed UDP sender, ack-clocked per-RTT update, U-EWMA, token-bucket pacer, per-packet CSV log.
- `sender/dctcp_sender.py` — windowed UDP sender, α-EWMA on ECN-marked fraction per RTT, AI/MD.
- `sender/pacer.py`, `sender/packet_format.py` — token bucket; `struct` codecs mirroring `headers.p4` (single source of truth).
- `receiver/reflector.py` — single binary used by both algorithms. Copies INT bytes verbatim if present, echoes ECN bit if present, returns small fixed ACK payload (seq + recv_ts).
- `controller/load_tables.py` — P4Runtime client; loads forwarding table, `link_bps` per port, `K` per port (DCTCP only).
- `controller/snapshot_queue.py` — background thread reading a `qdepth_history` register every 10 ms via thrift, for ground-truth queue plots.
- `mininet/topo_dumbbell.py`, `mininet/p4switch.py` — two-switch dumbbell, BMv2 launcher.
- `experiments/run_experiment.py` + `experiments/configs/*.yaml` — one-command experiment driver.
- `analysis/parse_logs.py`, `analysis/metrics.py`, `analysis/plot.py` — throughput, queue depth, FCT, Jain fairness, convergence.

### HPCC sender controller (BMv2-scaled constants)

- 10 Mbps × ~5 ms baseRTT → BDP ≈ 4–5 packets.
- `W_init = 8 packets`, `η = 0.95`, `W_AI = 1 pkt/RTT`, `MIN_W = 1`, `MAX_W = 64`.
- For each ACK: per-hop `txRate_i = ΔB_i·8 / Δt_i`, `u_i = qdepth_bytes / B_i + txRate_i / link_bps_i`. Then `U = max_i u_i`, `U_smoothed = (1-τ/T)·U_smoothed + (τ/T)·U` with `τ/T = 0.2`.
- Update gate: per-RTT, ack-clocked (when `cum_acked ≥ last_update_seq + W`). `W_ref` and `incarnation` are **sender-side bookkeeping only — not on the wire**: keep a per-(seq → W_ref, incarnation) map indexed at send time and looked up on ACK so the controller doesn't apply MD twice within the same congestion epoch.
- Window: `W_new = W_ref / (U_smoothed/η) + W_AI`.

### DCTCP sender controller (BMv2-scaled)

- `K = 5 packets` at 10 Mbps (~1× BDP); buffer ceiling 40 packets. Document as scaling choice.
- Per RTT: `F = marked / total`, `α = (1 - 1/16)·α + (1/16)·F`. Marks present ⇒ `W = W·(1 - α/2)`. No marks ⇒ `W += 1`.
- Loss recovery: go-back-N on a 3× baseRTT timeout. No SACK.

## Topology and Experiments

Two-switch dumbbell: `h1, h2 → s1 ──[bottleneck 10 Mbps, 1 ms, buf=40 pkt]── s2 → r1`. Edge links 100 Mbps so `s1↔s2` is unambiguously the bottleneck.

| ID | Setup | Metrics | Capture |
|----|-------|---------|---------|
| E1 | 1 long flow, 60 s | throughput, qdepth, RTT | sender CSV + register snapshots + bottleneck pcap |
| E2 | 2 flows staggered 5 s, 60 s | per-flow tput, Jain, qdepth | same |
| E3 | 8 senders × 100 KB incast, 50 reps | FCT mean/p99, drops | sender FCT log, drop counter |
| E4 | 1 flow then add 2nd at t=20s, 3rd at t=40s | convergence, qdepth spikes | per-pkt log |

Run each experiment for **both** HPCC and DCTCP. Optional E5 (leaf-spine) explicitly out of scope per topology decision.

## Six-Week Timeline

Roles: **A**=data-plane (P4), **B**=sender/control, **C**=orchestration/topology/analysis.

**Week 1 — Environment + scaffolding**
- All: install BMv2, p4c, Mininet, P4Runtime, scapy, gRPC libs in a shared Vagrant/Docker image. Smoke-test on each laptop.
- A: `headers.p4`, trivial L2-forwarding stubs of `hpcc.p4` and `dctcp.p4` compile and load.
- B: open-loop UDP sender, reflector, ACK loop (no control yet).
- C: dumbbell topology, P4Runtime table loader stub, end-to-end ping.
- **Gate**: h1 sends UDP to r1 through both BMv2 switches and gets an ACK back.

**Week 2 — DCTCP (debugging the harness with the simpler algorithm)**
- A: ECN marking in `dctcp.p4`, parameterize K.
- B: DCTCP α-EWMA + AIMD, ECN echo in reflector.
- C: experiment driver runs E1 for DCTCP; first throughput + qdepth plot.
- **Gate**: DCTCP single-flow shows expected sawtooth, converges near 10 Mbps.

**Week 3 — HPCC core**
- A: INT shim insertion, per-port byte-counter register, `link_bps` table.
- B: U computation, EWMA, per-RTT ack-clocked update.
- C: queue-snapshot register + per-packet CSV logger.
- **Hard gate**: HPCC E1 converges. If not, drop E3 and tuning week, focus remaining weeks on side-by-side single/two-flow comparison only.

**Week 4 — Two-flow + tuning**
- A: bug-fix INT under contention; drop counters.
- B: tune η, τ/T, W_AI; verify Jain > 0.95 in E2 for both algorithms.
- C: run E2 + E4 for both, comparison plots.

**Week 5 — Incast + analysis pipeline**
- A: any remaining P4 fixes for short-flow handling.
- B: short-flow mode (skip slow-start, burst W_init), FCT logging.
- C: run E3 for both algorithms; full analysis pipeline end-to-end.

**Week 6 — Writeup + reproducibility**
- All: README, methodology doc (especially the UDP-DCTCP caveat), one-command runners (`make e1-hpcc`, etc.), final figures, slide deck. Buffer for re-runs and presentation.

Parallelism: A and B work independently weeks 1–3 once the INT/ACK wire format is frozen end-of-week-1.

## Risks and Mitigations

- **BMv2 throughput ceiling**: cap experiments at 10–50 Mbps; document.
- **INT parser edge cases**: fix `max_hops = 4` at compile time, drop packets exceeding it; round-trip unit test through reflector.
- **Python sender pacing jitter**: pin to a core via `taskset`, pre-build packet templates, escape to C if pps > 5k.
- **Reflector mangling INT**: opaque-bytes only on receiver, never parses; `tests/test_int_roundtrip.py` enforces.
- **HPCC fails to converge by end of week 3**: hard gate; fall back to single-flow HPCC + DCTCP comparison; final fallback is AIMD-over-UDP per professor's suggestion.
- **Mininet host CPU contention skewing RTT**: `taskset` senders/receivers; run on a quiet machine.

## Critical Files

1. `p4src/common/headers.p4` — INT/ECN wire format; the bit-exact contract.
2. `p4src/hpcc.p4` — INT insertion, byte counter, qdepth.
3. `p4src/dctcp.p4` — ECN marking on `enq_qdepth > K`.
4. `sender/packet_format.py` — Python codecs mirroring headers.p4.
5. `sender/hpcc_sender.py` — U computation, EWMA, per-RTT update.
6. `sender/dctcp_sender.py` — α-EWMA, AIMD-over-UDP+ACK.
7. `receiver/reflector.py` — INT-opaque echo + ECN echo.
8. `mininet/topo_dumbbell.py` — two-switch dumbbell, link rates, buffer sizing.
9. `controller/load_tables.py` — P4Runtime table population.
10. `experiments/run_experiment.py` — orchestration entrypoint.

## Repo Layout

```
project/
├── README.md
├── Makefile
├── docs/{methodology.md, int_header_spec.md, results.md}
├── p4src/
│   ├── common/{headers.p4, parsers.p4}
│   ├── hpcc.p4
│   └── dctcp.p4
├── mininet/{topo_dumbbell.py, p4switch.py}
├── sender/{hpcc_sender.py, dctcp_sender.py, pacer.py, packet_format.py}
├── receiver/reflector.py
├── controller/{load_tables.py, snapshot_queue.py}
├── experiments/{run_experiment.py, configs/*.yaml}
├── analysis/{parse_logs.py, metrics.py, plot.py}
└── tests/{test_packet_format.py, test_int_roundtrip.py}
```

## Verification

- **Unit**: `pytest tests/test_packet_format.py tests/test_int_roundtrip.py` — wire-format codecs and INT round-trip through the reflector.
- **Integration smoke** (end of week 1): `make smoke` brings up Mininet + both P4 programs, runs `ping h1 r1`, then a 1-second open-loop UDP burst; expects ACKs returning with INT and ECN bits respectively.
- **End-to-end per algorithm**:
  - `make e1-dctcp` and `make e1-hpcc` — single-flow throughput should reach ≥ 9.0 Mbps (90% utilization), HPCC's avg qdepth should be visibly lower than DCTCP's in the resulting plot.
  - `make e2-{dctcp,hpcc}` — Jain fairness > 0.95 for both.
  - `make e3-{dctcp,hpcc}` — HPCC's incast p99 FCT should be lower; if not, document and analyze.
  - `make e4-{dctcp,hpcc}` — convergence-time plot after each new flow arrives.
- **Reproducibility**: `make all` regenerates every figure in `docs/results.md` from raw logs.
