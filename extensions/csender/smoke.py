"""Quick smoke for the C HPCC sender against the standard dumbbell."""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, "/workspace")

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet

from topo.dumbbell import DumbbellTopo, cleanup_mn, configure_hosts

JSON = "/workspace/build/hpcc.json"
LOG = "/tmp/c_hpcc_smoke.csv"
BIN = "/workspace/extensions/csender/hpcc_sender"


def main() -> None:
    if not os.path.isfile(BIN):
        sys.exit(f"missing {BIN} — run `make -C extensions/csender`")
    if not os.path.isfile(JSON):
        sys.exit(f"missing {JSON} — run `make p4-build-hpcc`")

    setLogLevel("warning")
    cleanup_mn()

    print("== bring up dumbbell ==")
    net = Mininet(topo=DumbbellTopo(json_path=JSON), link=TCLink, controller=None)
    net.start()
    configure_hosts(net)
    time.sleep(0.5)

    subprocess.run(
        [sys.executable, "-m", "controller.load_tables", "--algo", "hpcc"],
        check=True, cwd="/workspace",
    )

    h1, r1 = net.get("h1"), net.get("r1")
    refl = r1.popen(
        [sys.executable, "-u", "-m", "receiver.reflector", "--bind", "10.0.0.10"],
        cwd="/workspace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(0.5)

    try:
        out = h1.cmd(
            f"{BIN} 10.0.0.10 --duration 5 --log {LOG} "
            f"--padding 1400 --base-rtt 0.006 --w-init 8"
        )
        print(out)
    finally:
        refl.terminate()
        try: refl.wait(timeout=2)
        except subprocess.TimeoutExpired: refl.kill()
        net.stop()

    print("== event breakdown ==")
    os.system(f"awk -F, 'NR>1 {{print $2}}' {LOG} | sort | uniq -c")
    print("== throughput estimate ==")
    os.system(
        "awk -F, 'NR>1 && $2==\"ack\" {n++} "
        "END {printf \"acks=%d  est_mbps=%.2f\\n\", n, n*1500*8/5/1e6}' "
        + LOG
    )


if __name__ == "__main__":
    main()
