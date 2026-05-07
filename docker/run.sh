#!/usr/bin/env bash
# Plain `docker run` wrapper for teammates who'd rather not use Compose.
# Equivalent to docker-compose.yml; pick whichever workflow you prefer.
#
# Usage:
#   docker/run.sh build         # build the image
#   docker/run.sh shell         # drop into a shell in a fresh container
#   docker/run.sh exec          # exec into the long-running container (if up)
#   docker/run.sh up            # start the long-running container detached
#   docker/run.sh down          # stop + remove the long-running container

set -euo pipefail

IMAGE="int-cc/p4-dev:latest"
CONTAINER="int-cc-p4"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

build() {
    docker build \
        --platform linux/amd64 \
        -f "${PROJECT_ROOT}/docker/Dockerfile" \
        -t "${IMAGE}" \
        "${PROJECT_ROOT}"
}

# Common docker-run flags shared by `shell` and `up`.
docker_flags=(
    --platform linux/amd64
    --privileged
    --cap-add NET_ADMIN
    --cap-add NET_RAW
    --cap-add SYS_ADMIN
    --tmpfs /run
    --tmpfs /run/lock
    -v "${PROJECT_ROOT}:/workspace"
    -w /workspace
)

shell() {
    docker run --rm -it "${docker_flags[@]}" "${IMAGE}" /bin/bash
}

up() {
    docker run -d --name "${CONTAINER}" "${docker_flags[@]}" "${IMAGE}" sleep infinity
    echo "Container '${CONTAINER}' is up. Attach with: $0 exec"
}

exec_shell() {
    docker exec -it "${CONTAINER}" /bin/bash
}

down() {
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
    echo "Container '${CONTAINER}' removed."
}

case "${1:-}" in
    build) build ;;
    shell) shell ;;
    up)    up ;;
    exec)  exec_shell ;;
    down)  down ;;
    *)
        echo "Usage: $0 {build|shell|up|exec|down}" >&2
        exit 2
        ;;
esac
