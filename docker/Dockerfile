# syntax=docker/dockerfile:1.7
#
# P4/BMv2/Mininet/P4Runtime dev environment for the INT-CC project.
#
# Targets linux/amd64 explicitly because the p4lang openSUSE Build Service
# only ships amd64 .debs. On Apple Silicon, Docker Desktop runs this under
# Rosetta emulation (enable "Use Rosetta for x86_64/amd64 emulation" in
# Docker Desktop > Settings > General).

FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    TZ=Etc/UTC

# Base system + Mininet + tcpdump/iperf3 for experiments.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg sudo procps \
        git vim less make \
        iproute2 iputils-ping net-tools tcpdump iperf3 ethtool \
        bridge-utils socat \
        python3 python3-pip python3-dev python3-venv \
        build-essential pkg-config \
        mininet openvswitch-switch \
    && rm -rf /var/lib/apt/lists/*

# Add the p4lang openSUSE Build Service APT repository (provides p4lang-bmv2,
# p4lang-p4c, and the simple_switch / simple_switch_grpc binaries we need).
RUN echo 'deb [signed-by=/etc/apt/keyrings/p4lang.gpg] http://download.opensuse.org/repositories/home:/p4lang/xUbuntu_22.04/ /' \
        > /etc/apt/sources.list.d/home_p4lang.list \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://download.opensuse.org/repositories/home:p4lang/xUbuntu_22.04/Release.key \
        | gpg --dearmor -o /etc/apt/keyrings/p4lang.gpg \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        p4lang-bmv2 \
        p4lang-p4c \
    && rm -rf /var/lib/apt/lists/*

# Python tooling for sender / receiver / controller / analysis.
# Pinned for reproducibility across teammates.
#
# Note on P4Runtime: we install grpcio + grpcio-tools and let the controller
# code compile the .proto files from p4lang/p4runtime at first use (see
# controller/regen_protos.sh, added in week 1). We deliberately do NOT
# install `p4runtime-shell` here because its 0.0.4 release pins protobuf
# <3.21, which conflicts with every modern grpcio.
RUN pip3 install --no-cache-dir \
        scapy==2.5.0 \
        grpcio==1.62.1 \
        grpcio-tools==1.62.1 \
        protobuf==4.25.3 \
        pyyaml==6.0.1 \
        numpy==1.26.4 \
        pandas==2.2.2 \
        matplotlib==3.8.4 \
        pytest==8.2.0

# Mininet installs an `mn` CLI that requires root; the container runs as
# root by default (needed for `ip netns`/veth). The project tree is mounted
# at /workspace by docker-compose / run.sh.
WORKDIR /workspace

# Entrypoint brings up Open vSwitch daemons (ovsdb-server + ovs-vswitchd)
# which Mininet's default switch driver needs. Without these, even
# `mn --topo single,1` fails with "ovs-vsctl: database connection failed".
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Sanity self-check: every binary we depend on must be on PATH.
RUN simple_switch --version \
    && simple_switch_grpc --version \
    && p4c --version \
    && which mn \
    && python3 -c "import scapy, grpc, grpc_tools.protoc, google.protobuf; print('python deps ok')"

CMD ["/bin/bash"]
