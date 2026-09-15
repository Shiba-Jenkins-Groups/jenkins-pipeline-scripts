#!/usr/bin/env bash
# Provision only the App's isolated, persistent BuildKit worker after capacity admission.
set -euo pipefail

builder="${CI_BUILDX_BUILDER:?dedicated builder name required}"
config="${1:?BuildKit config required}"
[[ "${builder}" =~ ^[a-z0-9][a-z0-9_-]*$ && "${builder}" != default && "${builder}" != desktop-linux ]]
[[ -f "${config}" && -n "${BUILDX_CONFIG:-}" ]]
mkdir -p "${BUILDX_CONFIG}"
exec 9>"${BUILDX_CONFIG}/.ci-builder-setup.lock"
flock -w 120 9

policy_hash="$(sha256sum "${config}" | awk '{print $1}')"
marker="${BUILDX_CONFIG}/.${builder}.policy-sha256"
image='moby/buildkit@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8'

if docker buildx inspect "${builder}" >/dev/null 2>&1; then
    inspect="$(docker buildx inspect "${builder}")"
    [[ "${inspect}" == *'Driver:        docker-container'* ]] || {
        echo '[ci-builder] existing builder has wrong driver' >&2
        exit 1
    }
    [[ -f "${marker}" && "$(<"${marker}")" == "${policy_hash}" ]] || {
        echo '[ci-builder] existing builder policy is unknown or drifted; refusing to reuse' >&2
        exit 1
    }
else
    [[ ! -e "${marker}" ]] || {
        echo '[ci-builder] policy marker exists without builder; refusing implicit replacement' >&2
        exit 1
    }
    docker buildx create --name "${builder}" --driver docker-container \
        --driver-opt "image=${image}" --config "${config}"
    printf '%s\n' "${policy_hash}" > "${marker}"
fi

docker buildx inspect --builder "${builder}" --bootstrap
runtime_image="$(docker container inspect --format '{{.Config.Image}}' "buildx_buildkit_${builder}0")"
[[ "${runtime_image}" == "${image}" ]] || {
    echo '[ci-builder] running BuildKit image differs from pinned digest' >&2
    exit 1
}
echo "[ci-builder] ready: ${builder}, config-sha256=${policy_hash}, image=${image}"
