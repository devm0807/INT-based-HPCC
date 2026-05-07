#!/usr/bin/env bash
# Smoke test: confirm the dev container has every tool we need.
#
# Run inside the container via `make env-smoke`, or directly:
#   docker compose -f docker/docker-compose.yml exec p4 bash docker/smoke.sh

set -euo pipefail

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

echo "== binaries on PATH =="
command -v simple_switch       >/dev/null && pass simple_switch       || fail simple_switch
command -v simple_switch_grpc  >/dev/null && pass simple_switch_grpc  || fail simple_switch_grpc
command -v p4c                 >/dev/null && pass p4c                 || fail p4c
command -v mn                  >/dev/null && pass mn                  || fail mn
command -v tcpdump             >/dev/null && pass tcpdump             || fail tcpdump

echo "== versions =="
simple_switch --version
p4c --version | head -1

echo "== python imports =="
python3 - <<'PY'
import importlib, sys
mods = ["scapy", "grpc", "grpc_tools.protoc", "google.protobuf", "yaml",
        "numpy", "pandas", "matplotlib", "pytest"]
missing = [m for m in mods if importlib.util.find_spec(m) is None]
if missing:
    print(f"missing: {missing}", file=sys.stderr); sys.exit(1)
print("  all python deps importable")
PY

echo "== p4c can compile a trivial program =="
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/empty.p4" <<'P4'
#include <core.p4>
#include <v1model.p4>

header h_t { bit<8> x; }
struct headers   { h_t h; }
struct metadata  { }

parser P(packet_in pkt, out headers hdr, inout metadata m,
         inout standard_metadata_t s) { state start { transition accept; } }
control VC(inout headers hdr, inout metadata m) { apply { } }
control IC(inout headers hdr, inout metadata m,
           inout standard_metadata_t s) { apply { } }
control EC(inout headers hdr, inout metadata m,
           inout standard_metadata_t s) { apply { } }
control CC(inout headers hdr, inout metadata m) { apply { } }
control D(packet_out pkt, in headers hdr) { apply { } }

V1Switch(P(), VC(), IC(), EC(), CC(), D()) main;
P4
p4c-bm2-ss --p4v 16 -o "$TMP/empty.json" "$TMP/empty.p4" >/dev/null
[[ -s "$TMP/empty.json" ]] && pass "p4c-bm2-ss compiled empty.p4 -> JSON" || fail "p4c output empty"

echo "== mininet sanity (single host, no switch) =="
mn --test pingall --topo single,1 >/dev/null 2>&1 \
    && pass "mn smoke ran" \
    || fail "mn smoke failed (is the container --privileged?)"

echo
echo "All smoke checks passed."
