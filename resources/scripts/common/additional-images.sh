#!/usr/bin/env bash
# additional-images.sh — 同一個 CI commit 的附加無狀態 image 生命週期。
#
# ADDITIONAL_IMAGES 格式：name=dockerfile[,name=dockerfile]
# 例：receipt-recognition=Dockerfile-recognition
#
# 主 app 仍由 cd.sh 管理；本檔只讓專案 opt-in 產生第二顆以上的 independently deployable
# image。未宣告 ADDITIONAL_IMAGES 時完全 no-op，避免改變既有專案行為。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=error-handler.sh
source "${SCRIPT_DIR}/error-handler.sh"
# shellcheck source=docker.sh
source "${SCRIPT_DIR}/docker.sh"

STAGE="${1:-}"
case "${STAGE}" in build|scan|push) ;; *) echo "用法：additional-images.sh <build|scan|push>" >&2; exit 2 ;; esac

SPEC="${ADDITIONAL_IMAGES:-}"
[[ -n "${SPEC}" ]] || { echo "[additional-images] 未宣告，略過 ${STAGE}"; exit 0; }

BUILD_ENV="${WORKSPACE:-$(pwd)}/.pipeline/build.env"
[[ -f "${BUILD_ENV}" ]] || { echo "[additional-images] 找不到 ${BUILD_ENV}" >&2; exit 1; }
# shellcheck source=/dev/null
source "${BUILD_ENV}"

BRANCH="${BRANCH:-${GIT_BRANCH:-unknown}}"
BRANCH="${BRANCH#origin/}"
: "${APP_NAME:?APP_NAME is required}"
: "${APP_VERSION:?APP_VERSION is required}"
: "${BUILD_NUMBER:?BUILD_NUMBER is required}"

IFS=',' read -r -a ENTRIES <<<"${SPEC}"

validate_entry() {
    local name="$1" dockerfile="$2"
    [[ "${name}" =~ ^[a-z0-9][a-z0-9-]*$ ]] \
        || { echo "[additional-images] 非法 image 名稱：${name}" >&2; exit 1; }
    [[ -n "${dockerfile}" && "${dockerfile}" != /* && "${dockerfile}" != *".."* ]] \
        || { echo "[additional-images] 非法 Dockerfile 路徑：${dockerfile}" >&2; exit 1; }
    [[ -f "${WORKSPACE}/${dockerfile}" ]] \
        || { echo "[additional-images] Dockerfile 不存在：${dockerfile}" >&2; exit 1; }
}

local_ref() { printf '%s-%s:%s-%s' "${APP_NAME}" "$1" "${APP_VERSION}" "${BUILD_NUMBER}"; }
harbor_ref() {
    harbor_image_ref "${HARBOR_REGISTRY:-localhost:9290}" "${APP_NAME}/$1" \
        "${BRANCH}" "${APP_VERSION}" "${BUILD_NUMBER}"
}

if [[ "${STAGE}" == "push" ]]; then
    : "${HARBOR_USER:?HARBOR_USER is required}"
    : "${HARBOR_PASS:?HARBOR_PASS is required}"
    printf '%s' "${HARBOR_PASS}" | docker login "${HARBOR_REGISTRY:-localhost:9290}" \
        --username "${HARBOR_USER}" --password-stdin
    trap 'docker logout "${HARBOR_REGISTRY:-localhost:9290}" >/dev/null 2>&1 || true' EXIT
fi

for entry in "${ENTRIES[@]}"; do
    name="${entry%%=*}"
    dockerfile="${entry#*=}"
    [[ "${name}" != "${dockerfile}" ]] || { echo "[additional-images] 缺少 =：${entry}" >&2; exit 1; }
    validate_entry "${name}" "${dockerfile}"
    local_image="$(local_ref "${name}")"

    case "${STAGE}" in
        build)
            echo "[additional-images] Building ${name}: ${local_image}"
            DOCKER_BUILDKIT=0 docker build \
                -f "${WORKSPACE}/${dockerfile}" \
                --build-arg "APP_NAME=${APP_NAME}" \
                --build-arg "APP_VERSION=${APP_VERSION}" \
                --build-arg "BUILD_NUMBER=${BUILD_NUMBER}" \
                --build-arg "BRANCH=${BRANCH}" \
                -t "${local_image}" "${WORKSPACE}"
            ;;
        scan)
            report="${WORKSPACE}/trivy-results-${name}.xml"
            echo "[additional-images] Scanning ${name}: ${local_image}"
            if [[ -f "${WORKSPACE}/.trivyignore" ]]; then
                trivy image --exit-code "${SCAN_EXIT_CODE:-0}" --severity HIGH,CRITICAL \
                    --cache-dir "${WORKSPACE}/.trivy-cache-${name}" --format template \
                    --template '@/usr/local/share/trivy/templates/junit.tpl' --output "${report}" \
                    --ignorefile "${WORKSPACE}/.trivyignore" "${local_image}"
            else
                trivy image --exit-code "${SCAN_EXIT_CODE:-0}" --severity HIGH,CRITICAL \
                    --cache-dir "${WORKSPACE}/.trivy-cache-${name}" --format template \
                    --template '@/usr/local/share/trivy/templates/junit.tpl' --output "${report}" \
                    "${local_image}"
            fi
            ;;
        push)
            remote_image="$(harbor_ref "${name}")"
            echo "[additional-images] Pushing ${name}: ${remote_image}"
            docker tag "${local_image}" "${remote_image}"
            docker push "${remote_image}"
            write_image_ref_file "${remote_image}" "${WORKSPACE}/image-ref-${name}.txt"
            ;;
    esac
done
