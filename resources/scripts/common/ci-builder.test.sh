#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/docker.sh"

resolve_dockerfile() { printf '%s\n' /offline/Dockerfile; }
tag_base_image() { :; }
docker() { DOCKER_ARGS=("$@"); }

export WORKSPACE=/offline
export CI_BUILDX_BUILDER=shiba-app-ci
export CI_BUILDX_PLATFORM=linux/arm64
docker_build example:1 go '--build-arg FOO=bar'
[[ "${DOCKER_ARGS[*]}" == *'buildx build --builder shiba-app-ci --platform linux/arm64 --load'* ]]
[[ "${DOCKER_ARGS[*]}" == *'-t example:1 /offline'* ]]

unset CI_BUILDX_BUILDER
docker_build example:2 go
[[ "${DOCKER_ARGS[*]}" == 'build -f /offline/Dockerfile -t example:2 /offline' ]]

export CI_BUILDX_BUILDER='default;danger'
if docker_build example:3 go; then
    echo 'invalid builder unexpectedly accepted' >&2
    exit 1
fi
echo 'PASS: isolated Buildx --load, legacy fallback, invalid-builder rejection'
