#!/usr/bin/env bash
# cd.sh — CD 入口
# 用法：cd.sh [docker-build | harbor-push | deploy | all]
#   無參數或 all：依序執行全部
#   指定參數：只執行該 stage（供 ciPipeline.groovy 拆分 stage 使用）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"
source "${SCRIPT_DIR}/common/docker.sh"

STAGE="${1:-all}"

# ── 讀取 Archive 階段寫入的 build.env ────────────────────────────────────────
BUILD_ENV="${WORKSPACE:-$(pwd)}/.pipeline/build.env"
if [[ -f "${BUILD_ENV}" ]]; then
    # shellcheck source=/dev/null
    source "${BUILD_ENV}"
else
    echo "[cd] WARNING: .pipeline/build.env not found, falling back to env vars."
fi

BRANCH="${BRANCH:-${GIT_BRANCH:-unknown}}"
BRANCH="${BRANCH#origin/}"
APP_NAME="${APP_NAME:?APP_NAME is required}"
APP_VERSION="${APP_VERSION:?APP_VERSION is required}"
BUILD_NUMBER="${BUILD_NUMBER:?BUILD_NUMBER is required}"
LANGUAGE="${LANGUAGE:-java}"

IMAGE_TAG="${APP_NAME}:${APP_VERSION}-${BUILD_NUMBER}"

echo "[cd] Stage: ${STAGE}"
echo "[cd] Branch: ${BRANCH}"
echo "[cd] Image: ${IMAGE_TAG}"

# ── Docker Build ──────────────────────────────────────────────────────────────
docker_build_if_needed() {
    case "${BRANCH}" in
        develop|main|prod)
            local build_args="--build-arg APP_NAME=${APP_NAME} \
                              --build-arg APP_VERSION=${APP_VERSION} \
                              --build-arg BUILD_NUMBER=${BUILD_NUMBER} \
                              --build-arg BRANCH=${BRANCH}"
            docker_build "${IMAGE_TAG}" "${LANGUAGE}" "${build_args}"
            ;;
        *)
            echo "[cd] Branch '${BRANCH}' — skipping Docker build."
            ;;
    esac
}

# ── Harbor Push ───────────────────────────────────────────────────────────────
harbor_push_if_needed() {
    case "${BRANCH}" in
        main|prod)
            echo "[cd] TODO: Harbor push not yet implemented."
            ;;
        *)
            echo "[cd] Branch '${BRANCH}' — skipping Harbor push."
            ;;
    esac
}

# ── Deploy ────────────────────────────────────────────────────────────────────
deploy_if_needed() {
    case "${BRANCH}" in
        prod)
            echo "[cd] TODO: Deploy not yet implemented."
            ;;
        *)
            echo "[cd] Branch '${BRANCH}' — skipping deploy."
            ;;
    esac
}

# ── Stage 分派 ────────────────────────────────────────────────────────────────
case "${STAGE}" in
    docker-build)  docker_build_if_needed ;;
    harbor-push)   harbor_push_if_needed ;;
    deploy)        deploy_if_needed ;;
    all)           docker_build_if_needed; harbor_push_if_needed; deploy_if_needed ;;
    *)
        echo "[ERROR] Unknown stage: ${STAGE}. Use: docker-build | harbor-push | deploy | all" >&2
        exit 1
        ;;
esac

echo "[cd] ${STAGE} completed."
