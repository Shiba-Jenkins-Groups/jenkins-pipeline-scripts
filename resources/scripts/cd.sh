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
ARTIFACT_NAME="${ARTIFACT_NAME:-}"
RUNTIME_VERSION="${RUNTIME_VERSION:-17}"
LANGUAGE="${LANGUAGE:-java}"
ARTIFACTS_ROOT="${ARTIFACTS_ROOT:-/var/jenkins_home/artifacts}"

IMAGE_TAG="${APP_NAME}:${APP_VERSION}-${BUILD_NUMBER}"

echo "[cd] Stage: ${STAGE}"
echo "[cd] Branch: ${BRANCH}"
echo "[cd] Image: ${IMAGE_TAG}"

# ── Docker Build ──────────────────────────────────────────────────────────────
docker_build_if_needed() {
    case "${BRANCH}" in
        develop|main|prod)
            # JAR 存放於 ARTIFACTS_ROOT，需複製至 .pipeline/（Docker build context 內）
            # 才能被 Dockerfile 的 COPY ${JAR_FILE} app.jar 正確引用
            local jar_source="${ARTIFACTS_ROOT}/${APP_NAME}/release/${ARTIFACT_NAME}"
            local jar_dest="${WORKSPACE}/.pipeline/${ARTIFACT_NAME}"

            echo "[cd] Copying JAR to build context: ${jar_source}"
            cp "${jar_source}" "${jar_dest}"

            local build_args="--build-arg APP_NAME=${APP_NAME} \
                              --build-arg APP_VERSION=${APP_VERSION} \
                              --build-arg BUILD_NUMBER=${BUILD_NUMBER} \
                              --build-arg BRANCH=${BRANCH} \
                              --build-arg RUNTIME_VERSION=${RUNTIME_VERSION} \
                              --build-arg JAR_FILE=.pipeline/${ARTIFACT_NAME}"
            docker_build "${IMAGE_TAG}" "${LANGUAGE}" "${build_args}"

            # build context 用完後清理臨時 JAR
            rm -f "${jar_dest}"
            ;;
        *)
            echo "[cd] Branch '${BRANCH}' — skipping Docker build."
            ;;
    esac
}

# ── Harbor Push ───────────────────────────────────────────────────────────────
harbor_push_if_needed() {
    case "${BRANCH}" in
        # TODO(暫時)：develop 開啟 harbor push 供 CI/CD 串接驗證，驗證完成後移除 develop
        develop|main|prod)
            local registry="${HARBOR_REGISTRY:-localhost:9290}"
            local harbor_image="${registry}/${APP_NAME}/${APP_NAME}:${BRANCH}-${APP_VERSION}-${BUILD_NUMBER}"

            # Harbor credentials 由 ciPipeline.groovy withCredentials 注入
            echo "${HARBOR_PASS}" | docker login "${registry}" \
                --username "${HARBOR_USER}" \
                --password-stdin

            echo "[cd] Tagging: ${IMAGE_TAG} → ${harbor_image}"
            docker tag "${IMAGE_TAG}" "${harbor_image}"

            echo "[cd] Pushing: ${harbor_image}"
            docker push "${harbor_image}"

            docker logout "${registry}"
            echo "[cd] Harbor push completed: ${harbor_image}"
            ;;
        *)
            echo "[cd] Branch '${BRANCH}' — skipping Harbor push."
            ;;
    esac
}

# ── Deploy ────────────────────────────────────────────────────────────────────
deploy_if_needed() {
    case "${BRANCH}" in
        # TODO(暫時)：develop 開啟 deploy 佔位供 CI/CD 串接驗證，驗證完成後移除 develop
        develop|prod)
            echo "[cd] TODO: Deploy to k3s not yet implemented. (branch: ${BRANCH})"
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
