# Presentation outline — 10 slides

Drawn from PLAN.md / docs/results.md / docs/discussion.md /
docs/walkthrough.md. Each bullet is one slide's content; the
prose in italics is the speaker note.

---

## Slide 1 — Title

**INT-Based Congestion Control: Reproducing HPCC on a Software P4 Testbed**

Dev Mehta, Shreyas, Yash · 2026

*Open with: "this project was about understanding congestion control
that uses precise in-network telemetry, by building it ourselves."*

---

## Slide 2 — The question

- Can in-network telemetry (INT) beat ECN for congestion control?
- HPCC paper (SIGCOMM 2019) says yes — at 100 Gbps in NIC hardware
- **Our question**: does the advantage hold up at software scale?

*Stress the "for our own edification" framing the prof approved.*

---

## Slide 3 — System overview

```
h1..h8 → s1 (BMv2) → [10 Mbps bottleneck] → s2 (BMv2) → r1
              ↓                                   ↑
        DCTCP: mark CE                    Reflector echoes
        HPCC: insert INT                  SHIM+hops + ECN bit
```

- Two algorithms over the **same UDP+ACK harness**
- Wire format frozen: `[shim 4B][int_hop × N (26B each)][payload 16B]`
- 8 senders, all P4_16 in BMv2, all Python sender/receiver

*Point to: harness is shared so the comparison is apples-to-apples.
Show the actual project tree if asked.*

---

## Slide 4 — Algorithms in one slide each (4a, 4b)

### 4a — DCTCP
P4 egress: `if enq_qdepth > K: ipv4.ecn = CE`
Sender: `α = (1−1/16)·α + (1/16)·F`; marks ⇒ `W = W(1 − α/2)`; else `W += 1`

### 4b — HPCC
P4 egress: append `int_hop_t(switch_id, qdepth, tx_bytes, link_bps)` to stack
Sender: `U = max_i (qdepth/B + tx_rate/link_bps)`; `W = W_ref/(U_smoothed/η) + W_AI`

*Emphasize: DCTCP is ONE bit of feedback per ACK. HPCC is ~200 bits.*

---

## Slide 5 — E1 single flow (the win for HPCC)

| | DCTCP | HPCC |
|---|---|---|
| Throughput | 9.99 Mbps | 9.54 Mbps |
| RTT p99 | 14.3 ms | **8.4 ms** |
| W stdev | 0.84 (sawtooth) | 0.59 |

**HPCC trades 5% throughput for 41% lower p99 latency.**

[insert results/compare_e1.png]

*This is the canonical HPCC claim, and we reproduce it.*

---

## Slide 6 — E2 + E4 (both algorithms play fair)

- E2 two-flow Jain: DCTCP 0.9999 / HPCC 0.9994
- E4 three flows joining at 0/20/40s: both converge, HPCC RTT stays flat across joins

[insert results/e4_dctcp.png + results/e4_hpcc.png side by side]

*Quick slide — both algorithms are stable in steady state.*

---

## Slide 7 — E3 incast (the surprise: HPCC loses)

| | DCTCP | HPCC |
|---|---|---|
| FCT p99 | **725 ms** | 1319 ms |

[insert results/compare_e3.png — CDF overlay]

**DCTCP wins our 8-way incast at 100 KB per flow. Why?**

*Set up the diagnosis slide.*

---

## Slide 8 — Why HPCC loses incast in software

The math:
```
W* = W_AI / (1 − η/U_eq)              (HPCC fixed-point window)
U = qdepth_bytes / B + tx_rate / link_bps
B = link_bps × baseRTT / 8 = BDP in bytes
```

- Paper: BDP ≈ 417 MTUs. 1-MTU queue → U += 0.002 (noise).
- Ours: BDP ≈ 5 MTUs. 1-MTU queue → U += 0.20 (dominant).

**At small BDP, HPCC can't operate "near zero queue" because zero
is a discrete packet boundary 20% of BDP away.** Conservative W per
flow → 8 × 3 = 24 packets in flight vs ideal 40 → 60% utilization.

DCTCP's binary "did anyone mark?" signal ignores absolute U → grows
window aggressively → fills buffer → saturates link.

*The key insight slide. Spend time here.*

---

## Slide 9 — The six gaps and what we'd fix

| Gap | Scale | Fix |
|---|---|---|
| Bandwidth | 10000× | C sender at 1 Gbps |
| BDP in MTUs | 80× | follows from above |
| W_AI granularity | 15–20× | fractional W, prob send gate |
| Sender pacing | 1000× | DPDK / raw socket |
| EWMA constants | ~100× | re-tune τ/T |
| Kernel feedback noise | — | bypass kernel UDP |

**INT-based control is favorable in the regime it was designed for,
but its precision is a liability at small scale.**

*This is the takeaway. Frame it as the honest result.*

---

## Slide 10 — Conclusions

- ✅ Reproduced HPCC's single-flow latency advantage in software
- ✅ Reproduced fairness (both algorithms, both 2-flow and 3-flow)
- ❌ Did NOT reproduce HPCC's incast FCT advantage
- The ❌ is **explained**, not random: precise feedback signals need
  matching precision in pacing / control granularity, which our
  software stack can't deliver

**Honest takeaway**: INT isn't a free win. It's a precision instrument
that needs the right environment to outperform a blunter instrument
like ECN. Pick the algorithm to match your regime.

Code + writeup: github.com/devm0807/INT-based-HPCC

*End on the precision instrument framing. Take questions.*

---

## Suggested figures to render

These are already in `results/`:

1. `results/compare_e1.png` — throughput + RTT CDF side-by-side
2. `results/e4_dctcp.png` / `e4_hpcc.png` — staggered-flow dynamics
3. `results/compare_e3.png` — incast FCT CDFs overlaid
4. `results/e1_dctcp.png` / `e1_hpcc.png` — 4-panel single-flow detail

If presenting from slides directly, embed as PNGs at slide build time.
If using a Markdown→PDF tool (pandoc, marp), just reference them by path.
