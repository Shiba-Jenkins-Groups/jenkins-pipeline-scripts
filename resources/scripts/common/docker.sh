#!/usr/bin/env bash
# common/docker.sh — Docker Build 共通

source "$(dirname "${BASH_SOURCE[0]}")/error-handler.sh"

# 優先順序：
# 1. 專案根目錄 Dockerfile-{language}
# 2. 專案根目錄 Dockerfile
# 3. pipeline 預設 Dockerfile（由 ciPipeline.groovy 寫入 .pipeline/dockerfiles/）
resolve_dockerfile() {
    local language="${1}"
    local workspace="${WORKSPACE}"
    local lib_root="${WORKSPACE}/.pipeline/dockerfiles"

    if [[ -f "${workspace}/Dockerfile-${language}" ]]; then
        echo "${workspace}/Dockerfile-${language}"
    elif [[ -f "${workspace}/Dockerfile" ]]; then
        echo "${workspace}/Dockerfile"
    else
        echo "${lib_root}/Dockerfile-${language}"
    fi
}

docker_build() {
    local image_name="${1}"
    local language="${2}"
    local build_args="${3:-}"

    local dockerfile
    dockerfile="$(resolve_dockerfile "${language}")"

    echo "[docker] Using Dockerfile: ${dockerfile}"
    echo "[docker] Building image: ${image_name}"

    DOCKER_BUILDKIT=0 docker build \
        -f "${dockerfile}" \
        ${build_args} \
        -t "${image_name}" \
        "${WORKSPACE}"
}
