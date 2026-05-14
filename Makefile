# Top-level Makefile for the INT-CC project.
#
# Today this only exposes environment-management targets. Experiment targets
# (`make e1-hpcc`, etc.) are added later per the plan.
#
# All targets are .PHONY because none of them produce a tracked file artifact
# at this point; Make is being used as a task runner, not a build system.

COMPOSE := docker compose -f docker/docker-compose.yml
SERVICE := p4

.PHONY: help env-build env-up env-shell env-down env-smoke env-rebuild env-logs \
        p4-build-dctcp p4-clean topo-dctcp controller-dctcp smoke-dctcp

P4SRC      := /workspace/p4src
BUILD_DIR  := /workspace/build
P4C        := p4c-bm2-ss --target bmv2 --arch v1model --p4v 16

help:
	@echo "Environment targets:"
	@echo "  env-build         Build the dev container image."
	@echo "  env-up            Start the dev container in the background."
	@echo "  env-shell         Drop into a shell in the running container."
	@echo "                      (auto-starts the container if it's not up)"
	@echo "  env-down          Stop and remove the dev container."
	@echo "  env-rebuild       Rebuild the image from scratch (no cache)."
	@echo "  env-smoke         Run a smoke test verifying BMv2 + p4c + Mininet."
	@echo "  env-logs          Tail container logs."
	@echo ""
	@echo "P4 / topology targets (run AFTER env-up):"
	@echo "  p4-build-dctcp    Compile p4src/dctcp.p4 -> build/dctcp.json."
	@echo "  p4-clean          Remove build/."
	@echo "  topo-dctcp        Bring up the dumbbell topology with dctcp.json."
	@echo "                      Drops to mininet CLI; populate tables in another shell."
	@echo "  controller-dctcp  Populate dctcp tables on the running switches."
	@echo "  smoke-dctcp       End-to-end smoke: topology + tables + ping + UDP roundtrip."

env-build:
	$(COMPOSE) build

env-rebuild:
	$(COMPOSE) build --no-cache

env-up:
	$(COMPOSE) up -d

env-down:
	$(COMPOSE) down

env-logs:
	$(COMPOSE) logs -f

env-shell: env-up
	$(COMPOSE) exec $(SERVICE) /bin/bash

env-smoke: env-up
	$(COMPOSE) exec $(SERVICE) bash /workspace/docker/smoke.sh

p4-build-dctcp: env-up
	$(COMPOSE) exec $(SERVICE) bash -c "mkdir -p $(BUILD_DIR) && \
	    $(P4C) -o $(BUILD_DIR)/dctcp.json $(P4SRC)/dctcp.p4"

p4-build-hpcc: env-up
	$(COMPOSE) exec $(SERVICE) bash -c "mkdir -p $(BUILD_DIR) && \
	    $(P4C) -o $(BUILD_DIR)/hpcc.json $(P4SRC)/hpcc.p4"

p4-clean:
	rm -rf build/

topo-dctcp: env-up
	$(COMPOSE) exec -it $(SERVICE) python3 -m topo.dumbbell --json $(BUILD_DIR)/dctcp.json

controller-dctcp: env-up
	$(COMPOSE) exec $(SERVICE) python3 -m controller.load_tables --algo dctcp --ecn-k 5

smoke-dctcp: p4-build-dctcp env-up
	$(COMPOSE) exec $(SERVICE) python3 -m scripts.smoke_dctcp

# Experiments — each target runs both algorithms back-to-back, then plots.
.PHONY: e1 e2 e3 e4 compare results-clean

e1: p4-build-dctcp p4-build-hpcc env-up
	$(COMPOSE) exec $(SERVICE) python3 -m experiments.run_e1 --algo dctcp --duration 60 --log /workspace/results/e1_dctcp.csv
	$(COMPOSE) exec $(SERVICE) python3 -m experiments.run_e1 --algo hpcc  --duration 60 --log /workspace/results/e1_hpcc.csv
	$(COMPOSE) exec $(SERVICE) python3 -m analysis.plot_e1 /workspace/results/e1_dctcp.csv --out /workspace/results/e1_dctcp.png
	$(COMPOSE) exec $(SERVICE) python3 -m analysis.plot_e1 /workspace/results/e1_hpcc.csv  --out /workspace/results/e1_hpcc.png

e2: p4-build-dctcp p4-build-hpcc env-up
	$(COMPOSE) exec $(SERVICE) python3 -m experiments.run_e2 --algo dctcp --duration 60 --delay 5
	$(COMPOSE) exec $(SERVICE) python3 -m experiments.run_e2 --algo hpcc  --duration 60 --delay 5
	$(COMPOSE) exec $(SERVICE) python3 -m analysis.plot_e2 --algo dctcp --h1 /workspace/results/e2_dctcp_h1.csv --h2 /workspace/results/e2_dctcp_h2.csv --out /workspace/results/e2_dctcp.png
	$(COMPOSE) exec $(SERVICE) python3 -m analysis.plot_e2 --algo hpcc  --h1 /workspace/results/e2_hpcc_h1.csv  --h2 /workspace/results/e2_hpcc_h2.csv  --out /workspace/results/e2_hpcc.png

e3: p4-build-dctcp p4-build-hpcc env-up
	$(COMPOSE) exec $(SERVICE) python3 -m experiments.run_e3 --algo dctcp --n-flows 8 --rounds 5 --bytes-per-flow 100000
	$(COMPOSE) exec $(SERVICE) python3 -m experiments.run_e3 --algo hpcc  --n-flows 8 --rounds 5 --bytes-per-flow 100000
	$(COMPOSE) exec $(SERVICE) python3 -m analysis.plot_e3 --algo dctcp --in /workspace/results/e3/dctcp --out /workspace/results/e3_dctcp.png
	$(COMPOSE) exec $(SERVICE) python3 -m analysis.plot_e3 --algo hpcc  --in /workspace/results/e3/hpcc  --out /workspace/results/e3_hpcc.png

e4: p4-build-dctcp p4-build-hpcc env-up
	$(COMPOSE) exec $(SERVICE) python3 -m experiments.run_e4 --algo dctcp --duration 60 --start-times 0,20,40
	$(COMPOSE) exec $(SERVICE) python3 -m experiments.run_e4 --algo hpcc  --duration 60 --start-times 0,20,40
	$(COMPOSE) exec $(SERVICE) python3 -m analysis.plot_e4 --algo dctcp --start-times 0,20,40 --logs /workspace/results/e4_dctcp_h1.csv,/workspace/results/e4_dctcp_h2.csv,/workspace/results/e4_dctcp_h3.csv --out /workspace/results/e4_dctcp.png
	$(COMPOSE) exec $(SERVICE) python3 -m analysis.plot_e4 --algo hpcc  --start-times 0,20,40 --logs /workspace/results/e4_hpcc_h1.csv,/workspace/results/e4_hpcc_h2.csv,/workspace/results/e4_hpcc_h3.csv  --out /workspace/results/e4_hpcc.png

compare: env-up
	$(COMPOSE) exec $(SERVICE) python3 -m analysis.compare

results-clean:
	rm -rf results/
