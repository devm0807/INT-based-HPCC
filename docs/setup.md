# Dev environment setup

The whole P4 toolchain (BMv2, p4c, Mininet, P4Runtime, scapy) lives inside a
Docker container. You don't install any of it on your host — you install
Docker, build the image once, and `docker exec` into it whenever you want to
run experiments.

## Prerequisites by platform

The container targets `linux/amd64` because the p4lang APT packages only
ship for amd64. Apple Silicon Macs run it under Rosetta emulation (fine for
this project, ~10–50 Mbps experiments).

### macOS (Apple Silicon — M1/M2/M3/M4)

1. Install [Docker Desktop for Mac (Apple Silicon)](https://www.docker.com/products/docker-desktop/).
2. Open Docker Desktop → **Settings → General** and enable
   **"Use Rosetta for x86_64/amd64 emulation on Apple Silicon"**.
3. Make sure Docker Desktop is *running* (whale icon in the menu bar).
4. Recommended resources (Settings → Resources): ≥ 4 CPUs, ≥ 6 GB RAM, ≥ 20 GB disk.

### macOS (Intel)

1. Install [Docker Desktop for Mac (Intel)](https://www.docker.com/products/docker-desktop/).
2. Same resource recommendation as above.

### Linux

1. Install Docker Engine + the Compose plugin via your distro's package
   manager (or `https://docs.docker.com/engine/install/`).
2. Add yourself to the `docker` group so you don't need `sudo`:
   ```bash
   sudo usermod -aG docker $USER && newgrp docker
   ```

### Windows

1. Install Docker Desktop with the WSL2 backend.
2. Clone the repo *inside* a WSL2 distro (e.g. Ubuntu 22.04), not on the
   Windows filesystem — bind-mount performance is much better that way.

## One-time setup

From the project root:

```bash
make env-build
```

This builds `int-cc/p4-dev:latest`. First build pulls Ubuntu 22.04, the
p4lang APT packages, and the pinned Python deps, then runs an in-image
sanity check. Expect 5–15 minutes depending on bandwidth (longer the first
time on Apple Silicon under Rosetta).

## Daily workflow

```bash
make env-up         # start the container (detached)
make env-shell      # drop into a bash inside it
# ... edit code on your host, `git` on your host, run experiments inside ...
make env-down       # stop the container when you're done
```

The project root is bind-mounted at `/workspace` inside the container, so
edits made on your host (in VS Code, vim, whatever) are immediately visible
to the container, and any logs/pcaps the container writes show up on your
host filesystem.

You can keep multiple shells attached — just run `make env-shell` in
another terminal, or `docker compose -f docker/docker-compose.yml exec p4 bash`.

## Smoke test

After the first build, verify everything works:

```bash
make env-smoke
```

This checks that `simple_switch`, `simple_switch_grpc`, `p4c`, and `mn` are
all on PATH, that the Python deps import, that p4c compiles a trivial P4
program, and that Mininet can build a one-host network. If this passes,
your environment is good.

## Plain-`docker` workflow (no Compose)

If you'd rather not use Compose:

```bash
docker/run.sh build    # build the image
docker/run.sh shell    # one-shot interactive shell (--rm)
docker/run.sh up       # start a long-running container
docker/run.sh exec     # attach a shell to the long-running container
docker/run.sh down     # remove it
```

## Troubleshooting

**"Cannot connect to the Docker daemon"** — Docker Desktop isn't running.
Start the app and wait for the whale icon to stop animating.

**"exec format error" / `simple_switch: not found`** on Apple Silicon —
Rosetta emulation isn't enabled. Enable it in Docker Desktop settings (see
above) and rebuild: `make env-rebuild`.

**Mininet `RTNETLINK answers: Operation not permitted`** — the container
isn't running with `--privileged`. The Compose file and `run.sh` both set
this; if you're invoking `docker run` by hand, add `--privileged
--cap-add=NET_ADMIN --cap-add=NET_RAW --cap-add=SYS_ADMIN`.

**Stale Mininet state after a crash** — inside the container:
```bash
mn -c
```
This cleans up leftover veths and namespaces from a hung run.

**Slow build on Apple Silicon** — Rosetta emulation is roughly 0.6× native
speed for the apt/pip steps; that's expected. The image only needs to be
built once per teammate (and rebuilt rarely, when the Dockerfile changes).
