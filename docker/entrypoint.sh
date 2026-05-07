#!/usr/bin/env bash
# Container entrypoint.
#
# Mininet's default switch is Open vSwitch, which needs ovsdb-server and
# ovs-vswitchd running. Outside of systemd we have to start them ourselves.
# The two daemons are idempotent — if they're already up (e.g. from a
# previous attach), we skip.
#
# After bringing OvS up we exec into whatever CMD compose / `docker run`
# requested (default: `sleep infinity` so the container stays alive).

set -euo pipefail

start_ovs() {
    mkdir -p /var/run/openvswitch /var/log/openvswitch /etc/openvswitch

    if [[ ! -f /etc/openvswitch/conf.db ]]; then
        ovsdb-tool create /etc/openvswitch/conf.db \
            /usr/share/openvswitch/vswitch.ovsschema
    fi

    if ! pgrep -x ovsdb-server >/dev/null; then
        ovsdb-server /etc/openvswitch/conf.db \
            --remote=punix:/var/run/openvswitch/db.sock \
            --remote=db:Open_vSwitch,Open_vSwitch,manager_options \
            --private-key=db:Open_vSwitch,SSL,private_key \
            --certificate=db:Open_vSwitch,SSL,certificate \
            --bootstrap-ca-cert=db:Open_vSwitch,SSL,ca_cert \
            --pidfile --detach --log-file
        ovs-vsctl --no-wait init
    fi

    if ! pgrep -x ovs-vswitchd >/dev/null; then
        ovs-vswitchd --pidfile --detach --log-file
    fi
}

start_ovs
exec "$@"
