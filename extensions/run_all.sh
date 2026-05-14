#!/usr/bin/env bash
#
# run_all.sh — regenerate every artifact in one command.
#
# Calls into the root Makefile for core experiments + into the extensions
# for the optional bits. Idempotent — every step can be re-run.
#
# Usage:
#   bash extensions/run_all.sh                  # everything
#   bash extensions/run_all.sh core             # just the core e1..e4 + compare
#   bash extensions/run_all.sh extensions       # just the extensions
#
# Expects the container to be up. Run `make env-up` first if not.
set -euo pipefail

cd "$(dirname "$0")/.."

WHAT="${1:-all}"

run_core() {
    echo "===================================================="
    echo "  CORE: e1 e2 e3 e4 compare"
    echo "===================================================="
    make p4-build-dctcp p4-build-hpcc
    make e1
    make e2
    make e3
    make e4
    make compare
}

run_extensions() {
    echo "===================================================="
    echo "  EXTENSIONS: csender + qsnap + hpcc_v2"
    echo "===================================================="

    # Build the C sender inside the container.
    docker compose -f docker/docker-compose.yml exec -T -w /workspace/extensions/csender p4 make
    # Build the qsnap P4 variant.
    docker compose -f docker/docker-compose.yml exec -T -w /workspace/extensions/qsnap p4 make

    # C-sender single-flow smoke at 10 Mbps (sanity).
    docker compose -f docker/docker-compose.yml exec -T -w /workspace p4 \
        python3 extensions/csender/smoke.py

    # HPCC v2 vs v1 A/B.
    docker compose -f docker/docker-compose.yml exec -T -w /workspace p4 \
        python3 extensions/hpcc_v2/smoke.py
}

case "$WHAT" in
    core)        run_core ;;
    extensions)  run_extensions ;;
    all)         run_core; run_extensions ;;
    *)           echo "usage: $0 [core|extensions|all]"; exit 2 ;;
esac

echo
echo "DONE. Results in results/ and /tmp/c_hpcc_smoke.csv etc."
