#!/usr/bin/env bash
# common/docker.sh — Docker Build 共通

source "$(dirname "${BASH_SOURCE[0]}")/error-handler.sh"

# harbor_image_ref — Harbor image 完整參照的**單一產生點**（2026-07-19 Shiba 定案的階層命名）
#
#   <registry>/<app>/<branch>/<version>:<build>
#   例：localhost:9290/shiba-go-ditch-api-project/prod/0.67.2:12
#
# 為何把 branch 與 version 放進 repository 路徑而非全部塞進 tag：
#   舊格式 `<app>/<app>:<branch>-<version>-<build>` 使每個專案的所有 image 全擠在單一
#   repository 底下平鋪（實測 claude-project 累積 58 個 artifact），既看不出環境與版本的
#   層次，也無法用 Harbor 的 Tag Retention 對「同一版本的歷次 build」做保留規則
#   （保留規則是 per-repository 評估的）。改成路徑分層後，`develop/0.0.1-snapshot` 這個
#   repository 保留最近 N 個 build tag 即可，不再無止盡長。
#
# ⚠ 一律轉小寫：OCI/Docker 規範要求 **repository 名必須全小寫**（tag 才容許大寫）。
#   舊格式把版本放在 tag 裡所以 `0.0.1-SNAPSHOT` 沒問題；移進路徑後不轉小寫會直接
#   `invalid reference format` ——claude-project 正是 SNAPSHOT 版號，這行是它的成敗關鍵。
# ⚠ branch 的斜線轉為 `-`：feature/xxx 會多切出一層路徑，破壞「第二層＝環境」的語意。
#   （現行政策 DO_PUSH 只對 develop/main/prod 為 true，此為防禦性處理。）
harbor_image_ref() {
    local registry="$1" app="$2" branch="$3" version="$4" build="$5"
    local safe_branch="${branch//\//-}"
    local path
    path="$(printf '%s/%s/%s/%s' "${registry}" "${app}" "${safe_branch}" "${version}" | tr '[:upper:]' '[:lower:]')"
    printf '%s:%s' "${path}" "${build}"
}

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
