# Top-level Makefile for the INT-CC project.
#
# Today this only exposes environment-management targets. Experiment targets
# (`make e1-hpcc`, etc.) are added later per the plan.
#
# All targets are .PHONY because none of them produce a tracked file artifact
# at this point; Make is being used as a task runner, not a build system.

COMPOSE := docker compose -f docker/docker-compose.yml
SERVICE := p4

.PHONY: help env-build env-up env-shell env-down env-smoke env-rebuild env-logs

help:
	@echo "Environment targets:"
	@echo "  env-build    Build the dev container image."
	@echo "  env-up       Start the dev container in the background."
	@echo "  env-shell    Drop into a shell in the running container."
	@echo "                 (auto-starts the container if it's not up)"
	@echo "  env-down     Stop and remove the dev container."
	@echo "  env-rebuild  Rebuild the image from scratch (no cache)."
	@echo "  env-smoke    Run a smoke test verifying BMv2 + p4c + Mininet."
	@echo "  env-logs     Tail container logs."

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
