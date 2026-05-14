# Walkthrough — how this project works, end to end

Read this top to bottom and you'll have everything you need to re-build
the project from scratch. It assumes you've already read [PLAN.md](../PLAN.md)
for the goal and [results.md](results.md) for the numbers.

## Mental model

A congestion-control algorithm is a *control loop*. It has:
- A **sensor** in the network (queue depth, ECN bits, link utilization)
- A **transport** that carries the sensor reading back to the sender
- A **controller** at the sender that maps sensor reading → window/rate
- A **traffic generator** that paces packets according to the controller

DCTCP and HPCC differ only in the sensor + controller pair:

| | DCTCP | HPCC |
|---|---|---|
| Sensor (in P4) | binary CE mark when `enq_qdepth > K` | per-hop tuple (qdepth, tx bytes, timestamp, link bps) |
| Controller (in Python) | α-EWMA on marked fraction, AIMD on W | U=qdepth/B + txRate/linkBps, EWMA on U, multiplicative window update |

The **transport** (UDP forward + UDP ACK back), the **topology** (h1..hN → s1 → s2 → r1),
and the **traffic generator** (Python windowed sender with token bucket) are shared.

## End-to-end packet life

Follow one HPCC data packet from sender to receiver and back:

```
1. h1 (Python) builds UDP payload:
       [shim(4)] [data_payload(16)] [padding(1400)]
       shim.hop_count = 0; shim.max_hops = 4

2. Kernel adds IP+UDP+Ethernet headers and sets:
       ipv4.ecn = ECT(0) = 0x02   (set via setsockopt IP_TOS)
       udp.dst_port = 50000 (DATA_PORT)
       udp.src_port = 50001 (ACK_PORT — host bound here)

3. veth → s1 (BMv2):
       Parser extracts ethernet, ipv4, udp, shim, data_payload
       Ingress: ipv4_lpm hits 10.0.0.10/32 → action ipv4_forward(s2_gw_mac, port=9)
                std_meta.egress_spec = 9; TTL--; dst_mac rewritten
       Queueing: BMv2's egress queue on port 9 (rate-limited via set_queue_rate)
       Egress (HpccEgress):
                read tx_byte_count_reg[9], add packet_length+26, write back
                read link_bps_reg[9] (= 10_000_000)
                read switch_id_reg[0] (= 1)
                hdr.hops.push_front(1); hdr.hops[0].setValid()
                fill hops[0] with (switch_id=1, qdepth=enq_qdepth,
                                   tstamp=egress_global_timestamp, tx_bytes, link_bps)
                shim.hop_count = 1
                udp.length += 26; ipv4.total_len += 26; udp.checksum = 0
       ComputeChecksum: update ipv4.hdr_checksum
       Deparser emits: ethernet, ipv4, udp, shim, hops[0], data_payload

4. veth → s2 (BMv2):
       Parser extracts ethernet, ipv4, udp, shim, hops[0..0], data_payload
       Ingress: ipv4_lpm hits → forward on port 2, dst_mac=r1_mac
       Egress (HpccEgress): push_front another int_hop with switch_id=2
       shim.hop_count = 2; packet now 26+26 = 52 bytes longer than original

5. veth → r1 (Python reflector):
       Kernel delivers UDP payload to socket bound to 10.0.0.10:50000
       reflector parses shim, treats hops as opaque bytes (52 bytes for 2 hops)
       reads data_payload to extract seq + send_ts
       builds ACK: [shim'] [hops_blob_verbatim] [ack_payload]
                   shim'.flags = SHIM_FLAG_ECN_ECHO if recvmsg cmsg has ECN bit
                   shim'.hop_count = 2 (echoed)
                   ack_payload = (ack_seq=seq, recv_ts_us=now)
       sock.sendto(ack_bytes, ("10.0.0.1", 50001))

6. Reverse path through s2 → s1 (HPCC does NOT add hops to ACKs):
       Egress checks `meta.is_data == 1`. ACKs have udp.dst_port = 50001 (ACK_PORT)
       so meta.is_ack = 1 from parser; egress returns without modifying.
       Standard ipv4_lpm forwarding back to h1 via port 1.

7. h1 ACK listener thread receives UDP on its bound 50001:
       Parse shim → hop_count, parse hops → list[IntHop], parse ack_payload
       Look up in_flight[ack_seq] to recover send_ts → compute rtt_us
       For each IntHop:
           key = (switch_id, egress_port)
           last_ts, last_bytes = hop_last[key]  (or None on first packet)
           if last:
               dt = hop.egress_tstamp_us - last_ts
               dbytes = hop.tx_byte_count - last_bytes
               tx_rate = dbytes * 8e6 / dt
               B = hop.link_bps * baseRTT / 8
               u_i = (hop.qdepth * 1500) / B + tx_rate / hop.link_bps
           hop_last[key] = (hop.egress_tstamp_us, hop.tx_byte_count)
       U = max(u_i)
       U_smoothed = (1-τ/T)·U_smoothed + (τ/T)·U
       Update gate: if cum_acked ≥ last_update_seq + W AND now ≥ last_update_time + baseRTT:
           W_new = W_ref / (U_smoothed / η) + W_AI
           W_new = clamp(W_new, W_min, W_max)
           If W_new < W_ref: incarnation += 1
           W_ref = W; last_update_seq = cum_acked; last_update_time = now

8. h1 main thread (in lock):
       while len(in_flight) < int(W):
           build packet bytes; sock.sendto(...); in_flight[next_seq] = (ts, W, inc)
           next_seq += 1
       sleep(0.5 ms); loop.
```

## Wire format: the single most important contract

`p4src/common/headers.p4` defines:

```c
header shim_t {
    bit<8> flags;       // bit0 = ECN_ECHO; bits 1-7 reserved
    bit<8> hop_count;   // 0..MAX_HOPS (=4)
    bit<8> max_hops;    // = MAX_HOPS, echoed back unmodified
    bit<8> reserved;
}

header int_hop_t {     // 208 bits = 26 bytes
    bit<16> switch_id;
    bit<16> ingress_port;
    bit<16> egress_port;
    bit<32> qdepth;            // BMv2 packet units
    bit<48> egress_tstamp_us;
    bit<48> tx_byte_count;
    bit<32> link_bps;
}

header data_payload_t { bit<32> seq; bit<64> send_ts_us; bit<32> reserved; }
header ack_payload_t  { bit<32> ack_seq; bit<64> recv_ts_us; bit<32> reserved; }
```

Layout, top to bottom on the wire:

```
ethernet | ipv4 | udp | shim(4) | int_hop_t × N (26 each) | data_payload OR ack_payload (16)
```

**The shim + hops live INSIDE the UDP payload.** Discrimination is by
UDP destination port: 50000 = data (switches insert INT here), 50001 = ack
(switches pass through unmodified).

`sender/packet_format.py` mirrors this layout with `struct.pack` codecs:
- `_SHIM_FMT = "!BBBB"` → 4 bytes
- `_INT_HOP_FMT = "!HHHI6s6sI"` → 26 bytes (`6s` for the 48-bit fields, packed manually)
- `_DATA_FMT = _ACK_FMT = "!IQI"` → 16 bytes

`tests/test_packet_format.py` pins byte-exact layout with golden hex
strings. If you change a field width in `headers.p4`, the test fails
in `packet_format.py` and you fix it in the same commit.

The reflector and senders **never re-implement** parsing. They use
the codecs from `packet_format.py`, which guarantees the host-side
view exactly matches what BMv2 reads/writes.

## P4 data plane

### dctcp.p4 (the easy one)

```p4
control DctcpIngress {
    table ipv4_lpm {
        key            = { hdr.ipv4.dst_addr: lpm; }
        actions        = { ipv4_forward; drop; NoAction; }
        default_action = drop();
    }
    apply { if (hdr.ipv4.isValid() && hdr.ipv4.ttl > 1) ipv4_lpm.apply(); }
}

control DctcpEgress {
    register<bit<32>>(1) ecn_threshold;
    apply {
        if (!hdr.ipv4.isValid() || meta.is_data != 1) return;
        if (hdr.ipv4.ecn == ECN_NOT_ECT) return;      // sender didn't opt in
        bit<32> K;
        ecn_threshold.read(K, 0);
        if ((bit<32>)std_meta.enq_qdepth > K) {
            hdr.ipv4.ecn = ECN_CE;                    // mark
        }
    }
}
```

Tables and the K register are populated by `controller/load_tables.py`
via simple_switch_CLI thrift commands. No P4Runtime needed at this
stage — `simple_switch_grpc` listens on both thrift and gRPC.

### hpcc.p4 (the meaty one)

Ingress is identical to DCTCP. The interesting bit is egress INT insertion:

```p4
register<bit<48>>(BMV2_MAX_PORTS) tx_byte_count_reg;
register<bit<32>>(BMV2_MAX_PORTS) link_bps_reg;
register<bit<16>>(1) switch_id_reg;

apply {
    if (!hdr.ipv4.isValid() || meta.is_data != 1) return;
    if (hdr.shim.hop_count >= (bit<8>)MAX_HOPS) return;

    bit<32> egport = (bit<32>)std_meta.egress_port;

    bit<48> bytes_so_far;
    tx_byte_count_reg.read(bytes_so_far, egport);
    bytes_so_far = bytes_so_far + (bit<48>)std_meta.packet_length + 48w26;
    tx_byte_count_reg.write(egport, bytes_so_far);

    bit<32> bps;   link_bps_reg.read(bps, egport);
    bit<16> swid;  switch_id_reg.read(swid, 32w0);

    hdr.hops.push_front(1);              // shifts existing entries
    hdr.hops[0].setValid();
    hdr.hops[0].switch_id        = swid;
    hdr.hops[0].ingress_port     = (bit<16>)std_meta.ingress_port;
    hdr.hops[0].egress_port      = (bit<16>)std_meta.egress_port;
    hdr.hops[0].qdepth           = (bit<32>)std_meta.enq_qdepth;
    hdr.hops[0].egress_tstamp_us = (bit<48>)std_meta.egress_global_timestamp;
    hdr.hops[0].tx_byte_count    = bytes_so_far;
    hdr.hops[0].link_bps         = bps;

    hdr.shim.hop_count = hdr.shim.hop_count + 1;

    /* packet grew by 26 bytes — fix lengths, zero UDP checksum */
    hdr.udp.length     = hdr.udp.length     + 16w26;
    hdr.ipv4.total_len = hdr.ipv4.total_len + 16w26;
    hdr.udp.checksum   = 16w0;
}
```

Note on `push_front`: this shifts the existing stack right by 1 and
opens a new slot at index 0. After packet traverses s1 then s2, the
stack reads `[s2_hop, s1_hop, ...]` — reverse path order. The sender
takes max over hops so order doesn't matter.

Why `udp.checksum = 0`: legal in IPv4 (RFC 768 — "all zeros means
disabled"). Recomputing the UDP checksum in P4 over a payload that
just grew is possible but adds complexity. The cost of skipping is
that the receiver kernel doesn't verify the UDP checksum — fine for
our purposes.

## Topology + controller

`topo/dumbbell.py` builds:

```
h1 (10.0.0.1, port 1 on s1)
h2 (10.0.0.2, port 2)
...
h8 (10.0.0.8, port 8)
                       \
                        s1 (BMv2, device_id=1, thrift 9090, grpc 50051)
                        |  ↑ port 9 = bottleneck
                        | delay=1ms
                        ↓
                        s2 (BMv2, device_id=2, thrift 9091, grpc 50052)
                        |
                        r1 (10.0.0.10, port 2 on s2)
```

Bottleneck rate + buffer are enforced **inside BMv2** via
`set_queue_rate 833 9` and `set_queue_depth 40 9` on s1. We
deliberately don't use TCLink's `bw` — that puts the queue in tc
qdisc downstream where P4 can't see it.

`topo/p4switch.py` is a Mininet `Switch` subclass that spawns:

```sh
simple_switch_grpc \
    -i 1@s1-eth1 -i 2@s1-eth2 ... -i 9@s1-eth9 \
    --thrift-port 9090 --device-id 1 \
    /workspace/build/hpcc.json \
    -- --grpc-server-addr 0.0.0.0:50051
```

and waits for the thrift port to bind before declaring the switch
"started". Without that wait, `controller/load_tables.py` races the
switch coming up and times out.

`configure_hosts(net)` after `net.start()`:
1. Preloads ARP entries: every host knows every other host's MAC.
   Required because BMv2's L3 forwarding doesn't handle ARP.
2. Disables IPv6 and tunes `net.ipv4.tcp_ecn=2` on each host.
3. Disables NIC offloads on every veth (host- and switch-side).
   This is the **CRITICAL FIX** without which UDP gets silently
   dropped by the receiving kernel on checksum verification.

`controller/load_tables.py`:
- Builds a multi-line simple_switch_CLI command string per switch.
- For each sender host h_i, adds `table_add ipv4_lpm ipv4_forward 10.0.0.i/32 => <h_i_mac> i`.
- For r1, adds `table_add ipv4_lpm ipv4_forward 10.0.0.10/32 => <gw_mac> <bottleneck_port>`.
- Sets register values: ECN threshold (DCTCP) or switch_id + link_bps (HPCC).
- Calls `set_queue_rate / set_queue_depth` on s1's bottleneck port.
- Pipes the command string to `simple_switch_CLI --thrift-port <port>`.

## Reflector

`receiver/reflector.py`:

```python
sock = socket.socket(AF_INET, SOCK_DGRAM)
sock.setsockopt(IPPROTO_IP, IP_RECVTOS, 1)       # ask kernel for TOS via cmsg
sock.bind((bind_ip, DATA_PORT))
while True:
    data, ancdata, _, src = sock.recvmsg(2048, ANCBUF)
    ecn = _read_ecn(ancdata)                     # extract 2-bit ECN from cmsg
    ack = _build_ack(data, ecn)                  # opaque echo of shim+hops
    sock.sendto(ack, (src[0], ACK_PORT))
```

`_build_ack(payload, ecn_codepoint)` parses just shim and data_payload;
treats the hops blob as opaque bytes and copies it verbatim into the
ACK. Sets `shim.flags = SHIM_FLAG_ECN_ECHO` iff `ecn_codepoint == 0x03`
(CE). The same reflector serves both DCTCP and HPCC because it never
parses INT.

## Senders

Both have the same skeleton:

```python
class Sender:
    def __init__(...):
        self.sock = bind UDP socket on ACK_PORT, set IP_TOS=ECT(0)
        self.in_flight = {}              # seq → (send_ts, ...)
        self.cum_acked_seq = -1
        self.w = W_INIT

    def run(self, duration_s, max_packets=None):
        spawn ack listener thread
        spawn rto checker thread
        while time < end:
            with lock:
                while len(in_flight) < int(self.w):
                    sock.sendto(build packet); in_flight[next_seq] = (...)
                    next_seq += 1
            sleep(0.0005)
        drain ACKs; dump CSV

    def _ack_listener(self):
        while not stop:
            data, _ = sock.recvfrom(...)
            parse shim + hops + ack_payload
            with lock:
                rtt_us = now - in_flight[ack_seq].send_ts
                # cumulative ACK semantics
                for s in range(cum_acked+1, ack_seq+1): in_flight.pop(s)
                cum_acked_seq = ack_seq
                update controller state (α for DCTCP, U for HPCC)
                maybe fire window update

    def _rto_checker(self):
        while not stop:
            sleep(rto/4)
            with lock:
                if any in_flight older than rto:
                    cut W; retransmit all (go-back-N)
```

### DCTCP controller state

```python
self.alpha = 0.0
self.rtt_window_total = 0
self.rtt_window_marked = 0
self.last_update_seq = -1

def _on_ack(ack_seq, ecn_echo, rtt_us):
    self.rtt_window_total += 1
    if ecn_echo: self.rtt_window_marked += 1
    if cum_acked_seq >= last_update_seq + int(self.w):
        F = marked / total
        self.alpha = (1 - g) * self.alpha + g * F
        if marked > 0:  self.w = max(W_MIN, self.w * (1 - self.alpha / 2))
        else:           self.w = min(W_MAX, self.w + 1)
        last_update_seq = cum_acked_seq
        rtt_window_total = rtt_window_marked = 0
```

### HPCC controller state

```python
self.u_smoothed = 1.0
self.hop_last = {}            # (switch_id, egress_port) → (last_ts, last_bytes)
self.w_ref = W_INIT
self.incarnation = 0
self.last_update_time = monotonic()

def _on_ack(ack_seq, hops, rtt_us):
    u_max = 0
    for hop in hops:
        last = self.hop_last.get((hop.switch_id, hop.egress_port))
        self.hop_last[...] = (hop.tstamp, hop.tx_bytes)
        if last is None: continue
        dt = hop.tstamp - last.ts
        dbytes = hop.tx_bytes - last.bytes
        tx_rate = (dbytes * 8 * 1e6) / dt
        B = hop.link_bps * baseRTT / 8
        u_i = (hop.qdepth * 1500) / B + tx_rate / hop.link_bps
        u_max = max(u_max, u_i)
    self.u_smoothed = (1 - τ/T) * self.u_smoothed + (τ/T) * u_max

    if cum_acked >= last_seq + W AND wallclock >= last_time + baseRTT:
        u_eff = max(self.u_smoothed, 1e-3)
        w_new = self.w_ref / (u_eff / η) + W_AI
        w_new = clamp(w_new, W_MIN, W_MAX)
        if w_new < self.w_ref: self.incarnation += 1
        self.w_ref = w_new
        self.w = w_new
        last_update_time = now
        last_update_seq = cum_acked_seq
```

## Experiment drivers

All four (`run_e1.py`, `run_e2.py`, `run_e3.py`, `run_e4.py`) follow
this pattern:

```python
1. cleanup_mn()                      # mn -c + pkill simple_switch
2. Mininet(topo=DumbbellTopo(json=...), link=TCLink).start()
3. configure_hosts(net)              # ARP, IPv6 off, offloads off
4. subprocess.run([load_tables ...]) # populate tables via thrift
5. r1.popen([reflector ...])         # start reflector on r1
6. (per-experiment) host.popen([sender ...]) one or more times
7. wait for senders to exit
8. reflector.terminate(); net.stop()
9. (analysis is a separate step: python3 -m analysis.plot_eN)
```

`run_e4.py` adds scheduled starts: each sender starts at `t0 + start_times[i]`.

`run_e3.py` adds rounds: launches N senders simultaneously, waits for
all to complete (`--max-packets` cuts each off after K packets), then
sleeps before the next round.

## Analysis pipeline

`analysis/parse_logs.py` exposes `load(path) → Trace`. The Trace dataclass
has both DCTCP-specific fields (`alpha_ack`, `ecn_echo`) and HPCC-specific
fields (`u_smoothed_ack`, `hop_count`); whichever isn't in the CSV is
zero-filled, and `trace.algo` tells consumers which to use.

Helper functions:
- `throughput_mbps(trace, bin_s, pkt_size) → (t, mbps)` — bins ACK arrivals
- `mark_fraction(trace, bin_s) → (t, frac)` — for DCTCP
- `summarize(trace) → dict` — steady-state stats (warmup_s defaults to 5)

Per-experiment plot scripts (`plot_e1.py`, `plot_e2.py`, etc.) build on
these helpers. `analysis/compare.py` reads all `e1_*.csv`, `e2_*_h*.csv`,
and `e3/<algo>/r*_h*.csv`, and emits side-by-side comparison plots plus
`results/summary.json`.

## Things you'd do differently re-implementing

In hindsight:

1. **Freeze the wire format first.** We did this and it paid off. Every
   single component (P4 program, parser, sender, reflector, tests)
   speaks one byte-exact layout. Adding HPCC after DCTCP needed zero
   changes to packet_format.py.

2. **Discover the veth offload trap before writing the sender.** The
   8-hour debugging session for "0 ACKs received" was preventable.
   Add `ethtool --offload <iface> ... off` to your topology bring-up
   before any UDP traffic.

3. **Use BMv2 queue rate, not tc qdisc.** This is the second invisible
   trap: enq_qdepth in P4 is meaningless if rate-limiting happens
   downstream. Documented in [methodology.md](methodology.md).

4. **Build the smoke before the controller.** `make smoke-dctcp`
   exercises the full pipeline (topology + tables + sender + reflector)
   in one command. We added it after the controller and the sender,
   but it would have caught the offload bug earlier.

5. **Make experiments produce dual logs**: sender CSV + switch register
   snapshots. Currently sender CSV is the only ground truth for queue
   depth. A control-plane thread reading BMv2's qdepth register every
   10 ms would give an independent measurement and would unmask
   feedback-loop subtleties.

## Order of operations to rebuild from scratch

If you wiped the repo tomorrow, here's the order I'd rebuild:

1. **Plan + wire format** (1 hour): write `headers.p4` and
   `packet_format.py` together; write `test_packet_format.py` with
   golden hex strings. Goal: codecs round-trip locally.

2. **Docker env** (1-2 hours): Dockerfile with p4lang/OBS packages +
   Mininet + Python deps. Smoke = compile a trivial P4 program inside.

3. **Topology + L2 dummy P4 program** (2 hours): `topo/p4switch.py`
   that launches simple_switch_grpc; `topo/dumbbell.py` for 2 hosts +
   1 switch; a tiny "drop everything" P4 program. Smoke = `mn -c`
   then `python -m topo.dumbbell --json ...` and verify `pingall` fails
   (no forwarding).

4. **L3 forwarding P4** (2 hours): `dctcp.p4` ingress only — `ipv4_lpm`
   + `ipv4_forward` action. `controller/load_tables.py` for table
   entries. Smoke = `h1 ping r1` returns 3/3.

5. **Open-loop sender + reflector** (3 hours): `open_loop.py` +
   `reflector.py`. Smoke = h1 sends 200 pps for 1s, gets ≥600/600 ACKs.
   **This is where you discover the veth offload trap.** Fix with
   `ethtool --offload` in `configure_hosts`.

6. **DCTCP ECN marking** (2 hours): add egress to `dctcp.p4`. Wire ECT(0)
   on the sender. Update reflector to echo CE via `IP_RECVTOS` cmsg
   into shim.flags. Smoke = sender sees `ecn_echo` events in CSV when
   driving the link to saturation. **This is where you discover the
   tc qdisc vs BMv2 queue trap.** Fix with `set_queue_rate` in
   load_tables.

7. **DCTCP controller + E1** (3 hours): `dctcp_sender.py` with α-EWMA + AIMD.
   `run_e1.py`. `parse_logs.py` + `plot_e1.py`. **Week-2 gate**: E1
   sawtooth converging near 10 Mbps with α ≈ 0.2.

8. **HPCC P4 + sender + E1** (1 day): `hpcc.p4` with INT insertion
   (push_front, fix UDP/IP length, zero UDP checksum). `hpcc_sender.py`
   with per-hop U computation, EWMA, ack+time-gated update.
   **Week-3 gate**: HPCC E1 with throughput within 10% of DCTCP and
   p99 latency significantly lower. **This is where you discover
   that paper defaults (η=0.95, W_AI=1) under-utilize at small BDP.**
   Bump to η=0.99, W_AI=3, τ/T=0.05.

9. **Multi-flow infrastructure** (1 day): extend topology to 8 senders,
   write `run_e2.py` (two-flow), `run_e3.py` (incast), `run_e4.py`
   (staggered joins). Per-experiment plots. `analysis/compare.py`.

10. **Documentation + commits** (1 day): README, methodology, results,
    walkthrough. Logical commits. Reproducibility: `make e1 e2 e3 e4`.

Total: ~5 working days for a single competent dev. The HPCC tuning
phase (step 8) is where most of the time goes — debugging the control
loop with traces and figuring out which parameter actually moves the
needle.
