# INT-Based Congestion Control on P4/BMv2

Software-only reproduction of HPCC (SIGCOMM 2019) on a P4/BMv2/Mininet
testbed, with a simplified DCTCP as the apples-to-apples baseline.
Both algorithms run over the same UDP+ACK harness so any observed
difference is attributable to the control loop, not the transport.

See [PLAN.md](PLAN.md) for the design plan and [docs/results.md](docs/results.md)
for the headline numbers and methodology.

## Quickstart

Apple Silicon Mac (uses Docker Desktop + Rosetta) or Linux:

```sh
make env-build         # build the dev container (one-time, ~10-20 min on Rosetta)
make env-up            # start the container in the background
make env-smoke         # verify p4c / BMv2 / Mininet are all working
make smoke-dctcp       # end-to-end smoke: topology + tables + UDP roundtrip
```

If `smoke-dctcp` reports `ALL DCTCP SMOKE CHECKS PASSED`, the entire
stack works. To run the full experiment suite:

```sh
make e1                # single long flow, both algorithms
make e2                # two flows, fairness
make e3                # 8-way incast (5 rounds, 100 KB each)
make e4                # 3-flow dynamics (joins at 0/20/40 s)
make compare           # comparison plots + results/summary.json
```

Plots and CSVs land in `results/`.

## What's in this repo

```
.
├── PLAN.md                # the design plan
├── docs/                  # methodology, INT header spec, results
├── docker/                # Dockerfile + compose (Ubuntu 22.04 + p4lang + Mininet)
├── p4src/
│   ├── common/
│   │   ├── headers.p4    # frozen wire format
│   │   └── parsers.p4    # shared parser / deparser / checksum
│   ├── dctcp.p4          # ECN marking on enq_qdepth > K
│   └── hpcc.p4           # INT shim insertion: switch_id, qdepth, tx_byte_count, link_bps
├── topo/
│   ├── p4switch.py       # Mininet Switch subclass for simple_switch_grpc
│   └── dumbbell.py       # h1..h8 → s1 → [bottleneck] → s2 → r1
├── controller/
│   └── load_tables.py    # populates ipv4_lpm + BMv2 queue rate via thrift
├── sender/
│   ├── packet_format.py  # codecs for shim, int_hop, data_payload, ack_payload
│   ├── open_loop.py      # Week-1 gate harness (fixed rate, no CC)
│   ├── dctcp_sender.py   # α-EWMA + AIMD controller
│   └── hpcc_sender.py    # per-hop U computation + per-RTT update
├── receiver/
│   └── reflector.py      # opaque INT echo + IP_TOS → shim ECN_ECHO mirror
├── experiments/          # run_e1..e4: experiment drivers
├── analysis/             # parse_logs + plot_e1..e4 + compare
├── scripts/              # smoke_dctcp + diagnostic helpers
└── tests/                # pytest: wire-format + INT round-trip
```

## How the pieces fit

```
[h1 sender] -- UDP data --> [s1 (BMv2)] -- INT-tagged data --> [s2 (BMv2)] -- data --> [r1 reflector]
     ^                                                                                       |
     |                                                                                       |
     +------- UDP ACK with reflected SHIM+hops <-----------------------------------/--------+
                                                                                  /
            (DCTCP: switches mark ipv4.ecn=CE on egress when enq_qdepth > K
             HPCC: switches push int_hop_t with qdepth + tx_byte_count + link_bps)
```

Both algorithms speak the same SHIM-in-UDP-payload wire format
(pinned in [p4src/common/headers.p4](p4src/common/headers.p4)):

```
ethernet | ipv4 | udp | shim(4B) | int_hop_t × N (26B each) | data_or_ack_payload(16B)
                       │           │
                       │           └─ inserted/echoed; sender computes U=max_i u_i
                       └─ shim.flags carries ECN echo for DCTCP, hop_count for HPCC
```

The receiver treats `SHIM + INT-hop stack` as opaque bytes and copies
them byte-for-byte into ACKs (no parsing on r1). The switches only
INSERT INT on `udp.dst_port == DATA_PORT` (50000), so ACKs pass through
unmodified.

## Tested gates

| Gate | What it verifies |
|---|---|
| `pytest tests/` | wire-format codecs round-trip; INT echoes through reflector byte-exact |
| `make env-smoke` | p4c / BMv2 / mininet present; trivial P4 compiles; `mn` runs |
| `make smoke-dctcp` | h1 → r1 over 2× BMv2 switches; 600/600 ACKs at 200 pps |
| `make e1` | DCTCP saturates 10 Mbps with sawtooth; HPCC at 95% with low RTT |

## Headline results

| Experiment | DCTCP | HPCC |
|---|---|---|
| E1 throughput / RTT p99 | 9.99 Mbps / 14.3 ms | 9.54 Mbps / **8.4 ms** |
| E2 Jain (two flows) | 0.9999 | 0.9994 |
| E3 incast FCT p99 | **725 ms** | 1319 ms |

Full discussion in [docs/results.md](docs/results.md). HPCC wins
single-flow latency (the canonical "near-zero queueing" claim);
DCTCP wins incast in our software setup — explained by HPCC's
conservative window at small BDP under-utilizing when many flows
compete. See [docs/methodology.md](docs/methodology.md) for the
tuning rationale and known software artifacts.

## Reproducibility

`make compare` regenerates every figure under `results/` from raw
CSVs. CSVs are in `results/` (single flows) and `results/e3/<algo>/`
(incast rounds). Raw logs are tagged with their experiment id so a
fresh `make e1 e2 e3 e4 compare` overwrites them in place.

If a run fails with `Error creating interface pair ... File exists`,
a previous Mininet crashed without cleaning up — the experiment
drivers call `topo.dumbbell.cleanup_mn()` at startup, which runs
`mn -c && pkill simple_switch`.

## Background reading

- HPCC paper: <https://liyuliang001.github.io/publications/hpcc.pdf>
- DCTCP paper: <https://people.csail.mit.edu/alizadeh/papers/dctcp-sigcomm10.pdf>
- P4-INT spec: <https://p4.org/p4-spec/docs/INT_v2_1.pdf>
