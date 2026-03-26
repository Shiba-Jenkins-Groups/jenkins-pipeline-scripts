#!/usr/bin/env bash
# smoke-test.sh — Smoke Test 入口
# Harbor Push 後自動驗證 image 可正常啟動
# 依語言呼叫對應的 {language}-smoke-test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

# ── 讀取 Archive 階段寫入的 build.env ────────────────────────────────────────
BUILD_ENV="${WORKSPACE:-$(pwd)}/.pipeline/build.env"
if [[ -f "${BUILD_ENV}" ]]; then
    # shellcheck source=/dev/null
    source "${BUILD_ENV}"
else
    echo "[smoke-test] WARNING: .pipeline/build.env not found, falling back to env vars."
fi

BRANCH="${BRANCH:-${GIT_BRANCH:-unknown}}"
BRANCH="${BRANCH#origin/}"
APP_NAME="${APP_NAME:?APP_NAME is required}"
APP_VERSION="${APP_VERSION:?APP_VERSION is required}"
BUILD_NUMBER="${BUILD_NUMBER:?BUILD_NUMBER is required}"
LANGUAGE="${LANGUAGE:-java}"
HARBOR_REGISTRY="${HARBOR_REGISTRY:-localhost:9290}"

HARBOR_IMAGE="${HARBOR_REGISTRY}/${APP_NAME}/${APP_NAME}:${BRANCH}-${APP_VERSION}-${BUILD_NUMBER}"

echo "[smoke-test] Language: ${LANGUAGE}"
echo "[smoke-test] Image: ${HARBOR_IMAGE}"

# ── 依語言呼叫對應實作 ────────────────────────────────────────────────────────
bash "${SCRIPT_DIR}/${LANGUAGE}/${LANGUAGE}-smoke-test.sh" "${HARBOR_IMAGE}"

echo "[smoke-test] Completed."
