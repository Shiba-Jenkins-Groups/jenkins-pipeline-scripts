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

    # build 完成後，為 base image 建立 shiba/base/ 別名，方便 docker images 識別用途
    tag_base_image "${dockerfile}" "${build_args}"
}

# 解析 Dockerfile 的 FROM 行，為 base image 打 shiba/base/ 識別別名
# 別名格式：shiba/base/{image-name}:{tag}，例：shiba/base/eclipse-temurin:17-jre-jammy
tag_base_image() {
    local dockerfile="${1}"
    local build_args="${2:-}"

    # 讀取 FROM 行樣板（第一個 FROM，跳過多階段後段）
    local from_template
    from_template="$(awk '/^FROM/{print $2; exit}' "${dockerfile}")"
    [[ -z "${from_template}" ]] && return 0

    # 讀取 Dockerfile ARG 定義的預設版本號
    local runtime_version
    runtime_version="$(awk '/^ARG RUNTIME_VERSION/{split($2,a,"="); print a[2]; exit}' "${dockerfile}")"
    runtime_version="${runtime_version:-17}"

    # 若 build_args 有傳入 RUNTIME_VERSION，優先覆蓋
    if echo "${build_args}" | grep -q "RUNTIME_VERSION="; then
        runtime_version="$(echo "${build_args}" | grep -oE 'RUNTIME_VERSION=[^ ]+' | cut -d= -f2)"
    fi

    # REGISTRY_PREFIX：build_args 傳入值優先，否則取環境變數（空＝Docker Hub 直抓）
    local registry_prefix="${REGISTRY_PREFIX:-}"
    if echo "${build_args}" | grep -q "REGISTRY_PREFIX="; then
        registry_prefix="$(echo "${build_args}" | grep -oE 'REGISTRY_PREFIX=[^ ]*' | cut -d= -f2-)"
    fi

    # 實際 image（可能含 registry 前綴，docker tag 的來源）
    local base_image="${from_template//\$\{REGISTRY_PREFIX\}/${registry_prefix}}"
    base_image="${base_image//\$\{RUNTIME_VERSION\}/${runtime_version}}"

    # 邏輯名（不含來源前綴）：別名以邏輯名組成，避免 registry 帶 port 時
    # cut -d: 誤切（localhost:9290/... 的 port 冒號會被當成 tag 分隔）
    local base_image_logical="${from_template//\$\{REGISTRY_PREFIX\}/}"
    base_image_logical="${base_image_logical//\$\{RUNTIME_VERSION\}/${runtime_version}}"

    # 拆解 image name 與 tag，建立 shiba/base/ 別名
    local image_name image_tag alias_tag
    image_name="$(echo "${base_image_logical}" | cut -d: -f1 | awk -F/ '{print $NF}')"
    image_tag="$(echo "${base_image_logical}" | cut -d: -f2-)"
    alias_tag="shiba/base/${image_name}:${image_tag}"

    echo "[docker] Tagging base: ${base_image} → ${alias_tag}"
    docker tag "${base_image}" "${alias_tag}" 2>/dev/null \
        || echo "[docker] WARNING: base image tag skipped (${base_image} not in local cache)"
}
