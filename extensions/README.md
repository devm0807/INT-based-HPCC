# Extensions — optional follow-ups beyond the 6-week plan

Everything in here is **additive**: nothing modifies the core repo,
so it can be skipped or removed without affecting the main pipeline.
Run targets from the root project's Makefile still work unchanged.

## What's here

| Dir | Purpose |
|---|---|
| `slides/` | 10-slide presentation outline drawn from the four core docs |
| `csender/` | C reimplementation of the HPCC sender, capable of pushing higher pps to test the BDP hypothesis from docs/discussion.md |
| `qsnap/` | hpcc.p4 extension that snapshots qdepth into a 1024-slot register array; Python control-plane reader for ground-truth queue plots |
| `hpcc_v2/` | HPCC sender with proper W_ref/incarnation snapshot logic per packet (fixes gap 5 from docs/discussion.md) |
| `bdp_sweep/` | Script that runs E1 at 10, 50, 100 Mbps using csender, plots throughput / RTT / FCT to confirm or refute the BDP hypothesis |
| `run_all.sh` | Item 5 — one-command "regenerate everything", chains the core experiments plus these extensions |

## How to use

```sh
# build the C sender
make -C extensions/csender

# run a single C-sender HPCC experiment
make -C extensions/csender smoke

# run the BDP sweep (10/50/100 Mbps); produces extensions/bdp_sweep/*.png
bash extensions/bdp_sweep/run.sh

# compile + run the qdepth-snapshot variant of hpcc.p4
make -C extensions/qsnap build run
```

Each subdirectory has its own README with deeper notes.

## Why these specifically

Items 2 (C sender) and 4 (HPCC v2) directly attack the "we couldn't
reproduce the paper" question from [docs/discussion.md](../docs/discussion.md).
Item 3 (qdepth snapshot) gives us an independent ground-truth measurement
to validate the sender's view of queue depth. Item 1 (slides) is the
presentation deliverable; item 5 (run-all) makes the whole pipeline
one-click reproducible.
