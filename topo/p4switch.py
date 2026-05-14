"""
BMv2 switch launcher for Mininet.

Subclasses mininet.node.Switch to spawn `simple_switch_grpc` with the
compiled P4 JSON loaded. Each instance opens its own thrift port (for
simple_switch_CLI / runtime_CLI) and gRPC port (for P4Runtime), so two
or more switches can run side-by-side in one Mininet.
"""
from __future__ import annotations

import os
import socket
import time

from mininet.log import error, info
from mininet.node import Switch


class P4Switch(Switch):
    """Mininet Switch that runs simple_switch_grpc as the dataplane.

    Args:
        json_path:    path to the compiled BMv2 .json (must already exist).
        device_id:    BMv2 device id; must be unique per process.
        thrift_port:  CLI/runtime thrift port (default 9090).
        grpc_port:    P4Runtime gRPC port (default 50051).
        log_dir:      directory for per-switch stderr/stdout logs.
        sw_path:      override the simple_switch_grpc binary path.
        pcap_dump:    pass --pcap so BMv2 dumps every port to a .pcap.
    """

    def __init__(
        self,
        name,
        json_path,
        device_id,
        thrift_port=9090,
        grpc_port=50051,
        log_dir="/tmp",
        sw_path="simple_switch_grpc",
        pcap_dump=False,
        **kwargs,
    ):
        Switch.__init__(self, name, **kwargs)
        if not os.path.isfile(json_path):
            raise FileNotFoundError(f"P4 JSON not found: {json_path}")
        self.json_path = json_path
        self.device_id = device_id
        self.thrift_port = thrift_port
        self.grpc_port = grpc_port
        self.log_file = os.path.join(log_dir, f"{name}.log")
        self.sw_path = sw_path
        self.pcap_dump = pcap_dump

    def start(self, _controllers):  # noqa: ARG002 (mininet API)
        info(f"*** starting P4 switch {self.name} (device-id={self.device_id})\n")

        args = [self.sw_path]
        for port_no, intf in self.intfs.items():
            if not intf.IP():
                args.extend(["-i", f"{port_no}@{intf.name}"])

        if self.pcap_dump:
            args.append("--pcap")

        args.extend([
            "--thrift-port", str(self.thrift_port),
            "--device-id",   str(self.device_id),
            self.json_path,
            "--",
            "--grpc-server-addr", f"0.0.0.0:{self.grpc_port}",
        ])

        cmd = " ".join(args)
        info(f"    {cmd}\n")
        self.cmd(f"{cmd} >{self.log_file} 2>&1 &")

        if not self._wait_for_thrift(timeout=5):
            error(f"P4 switch {self.name} did not open thrift port "
                  f"{self.thrift_port}; see {self.log_file}\n")
            raise RuntimeError(f"BMv2 failed to start for {self.name}")

        info(f"    {self.name} ready "
             f"(thrift={self.thrift_port}, grpc={self.grpc_port})\n")

    def _wait_for_thrift(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.thrift_port), 0.2):
                    return True
            except OSError:
                time.sleep(0.1)
        return False

    def stop(self, deleteIntfs=True):  # noqa: N803 (mininet API)
        # pkill matches our own simple_switch_grpc by its unique thrift port.
        self.cmd(
            f"pkill -f 'simple_switch_grpc.*--thrift-port {self.thrift_port}'"
        )
        super().stop(deleteIntfs)
