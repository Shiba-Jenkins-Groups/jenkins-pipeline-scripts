#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/docker.sh"

resolve_dockerfile() { printf '%s\n' /offline/Dockerfile; }
tag_base_image() { :; }
docker() { DOCKER_ARGS=("$@"); }
python3() { VERIFY_ARGS=("$@"); return "${VERIFY_EXIT:-0}"; }

export WORKSPACE=/offline
export CI_BUILDX_BUILDER=shiba-app-ci
export CI_BUILDX_PLATFORM=linux/arm64
docker_build example:1 go '--build-arg FOO=bar'
[[ "${DOCKER_ARGS[*]}" == *'buildx build --builder shiba-app-ci --platform linux/arm64 --load'* ]]
[[ "${DOCKER_ARGS[*]}" == *'-t example:1 /offline'* ]]

export CI_BUILDX_BUILDER=shiba-ci-components-buildkit
docker_build example:managed go
[[ "${VERIFY_ARGS[0]}" == */ci_builder_remote.py ]]
[[ "${VERIFY_ARGS[1]}" == */ci-builder.toml ]]
[[ "${DOCKER_ARGS[*]}" == *'buildx build --builder shiba-ci-components-buildkit --platform linux/arm64 --load'* ]]

VERIFY_EXIT=1
DOCKER_ARGS=()
if docker_build example:refused go || [[ "${#DOCKER_ARGS[@]}" != 0 ]]; then
    echo 'managed builder verification failure did not prevent the Docker build' >&2
    exit 1
fi
unset VERIFY_EXIT

unset CI_BUILDX_BUILDER
docker_build example:2 go
[[ "${DOCKER_ARGS[*]}" == 'build -f /offline/Dockerfile -t example:2 /offline' ]]

export CI_BUILDX_BUILDER='default;danger'
if docker_build example:3 go; then
    echo 'invalid builder unexpectedly accepted' >&2
    exit 1
fi
echo 'PASS: isolated Buildx --load, managed validation, legacy fallback, invalid-builder rejection'
