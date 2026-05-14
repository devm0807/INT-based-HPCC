"""End-to-end smoke for the DCTCP path.

Brings up the dumbbell topology programmatically (no mininet CLI),
loads tables via controller.load_tables, runs ping h1->r1, then runs
the reflector on r1 + open_loop sender on h1 for a few seconds and
validates the resulting CSV.

This is the Week-2-gate one-button check: if it goes green, the wire
format, the P4 data plane, the topology, and the controller are all
working together.

Run inside the dev container (or via `make smoke-dctcp`):
    python3 -m scripts.smoke_dctcp
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet

from topo.dumbbell import DumbbellTopo, configure_hosts


JSON_PATH = "/workspace/build/dctcp.json"
WORKDIR = "/workspace"
LOG_PATH = "/tmp/smoke_h1.csv"

RATE_PPS = 200
DURATION_S = 3
# 200 pps * 3 s = 600 expected; allow slack for startup + 1st-RTT pacing.
MIN_EXPECTED_ACKS = 500


def _fail(msg: str) -> None:
    print(f"\nFAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"  ok   {msg}")


def main() -> None:
    if not os.path.isfile(JSON_PATH):
        _fail(f"missing {JSON_PATH} — run `make p4-build-dctcp` first")

    setLogLevel("warning")

    print("== bring up dumbbell ==")
    topo = DumbbellTopo(json_path=JSON_PATH)
    net = Mininet(topo=topo, link=TCLink, controller=None)
    net.start()
    configure_hosts(net)
    _ok("topology up")

    try:
        time.sleep(0.5)  # let BMv2 finish init

        print("== load DCTCP tables ==")
        rc = subprocess.run(
            [sys.executable, "-m", "controller.load_tables",
             "--algo", "dctcp", "--ecn-k", "5"],
            capture_output=True, cwd=WORKDIR, timeout=15,
        )
        if rc.returncode != 0:
            _fail(
                f"load_tables failed (rc={rc.returncode}):\n"
                f"--- stdout ---\n{rc.stdout.decode()}\n"
                f"--- stderr ---\n{rc.stderr.decode()}"
            )
        _ok("tables loaded")

        print("== ping h1 -> r1 (3 packets) ==")
        h1 = net.get("h1")
        r1 = net.get("r1")
        out = h1.cmd("ping -c 3 -W 1 10.0.0.10")
        if "3 received" not in out:
            _fail(f"ping failed:\n{out}")
        _ok("ping 3/3")

        print(f"== UDP open-loop h1 -> r1 for {DURATION_S}s @ {RATE_PPS} pps ==")
        h1.cmd(f"rm -f {LOG_PATH}")
        reflector_proc = r1.popen(
            [sys.executable, "-u", "-m", "receiver.reflector",
             "--bind", "10.0.0.10", "-v"],
            cwd=WORKDIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        time.sleep(0.5)

        if reflector_proc.poll() is not None:
            out = reflector_proc.stdout.read().decode(errors="replace")
            _fail(f"reflector exited early (rc={reflector_proc.returncode}):\n{out}")

        # tcpdump on r1's interface so we can see what actually arrives.
        pcap_path = "/tmp/smoke_r1.pcap"
        tcpdump_proc = r1.popen(
            ["tcpdump", "-i", "r1-eth0", "-w", pcap_path,
             "-U", "udp", "and", "not", "port", "53"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        time.sleep(0.2)

        sender_out = h1.cmd(
            f"cd {WORKDIR} && {sys.executable} -m sender.open_loop "
            f"10.0.0.10 --rate-pps {RATE_PPS} --duration {DURATION_S} "
            f"--log {LOG_PATH}"
        )

        tcpdump_proc.terminate()
        try:
            tcpdump_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            tcpdump_proc.kill()

        reflector_proc.terminate()
        try:
            reflector_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            reflector_proc.kill()
        reflector_out = reflector_proc.stdout.read().decode(errors="replace")

        if not os.path.isfile(LOG_PATH):
            _fail(f"sender produced no log:\n{sender_out}")
        with open(LOG_PATH) as f:
            n_rows = sum(1 for _ in f) - 1
        if n_rows < MIN_EXPECTED_ACKS:
            pcap_summary = ""
            try:
                pcap_summary = subprocess.check_output(
                    ["tcpdump", "-nn", "-r", pcap_path, "-c", "10"],
                    stderr=subprocess.STDOUT, timeout=5,
                ).decode(errors="replace")
            except Exception as e:  # noqa: BLE001
                pcap_summary = f"<tcpdump replay failed: {e}>"
            _fail(
                f"only {n_rows} ACKs (expected >= {MIN_EXPECTED_ACKS})\n"
                f"--- sender stdout ---\n{sender_out}\n"
                f"--- reflector output ---\n{reflector_out}\n"
                f"--- pcap on r1-eth0 (first 10) ---\n{pcap_summary}\n"
                f"(full pcap at {pcap_path} inside container)"
            )
        _ok(f"open-loop returned {n_rows} ACKs")

    finally:
        net.stop()

    print("\nALL DCTCP SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
