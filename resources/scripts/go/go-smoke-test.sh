#!/usr/bin/env bash
# go/go-smoke-test.sh — Go Smoke Test
# Harbor image 起臨時容器，輪詢 health endpoint 回 200 才算通過
# 專案根目錄 smoke-test.env（選用）：
#   SMOKE_* 開頭 → 設定本腳本（SMOKE_HEALTH_PATH / SMOKE_APP_PORT / SMOKE_MAX_WAIT）
#   其他 KEY=VALUE → 以 -e 注入容器（應用程式啟動所需環境變數）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

HARBOR_IMAGE="${1:?HARBOR_IMAGE is required}"
CONTAINER_NAME="${APP_NAME:-app}-smoke-${BUILD_NUMBER:-0}"

# ── 讀取專案自訂 smoke test 設定（選用）─────────────────────────────────────
SMOKE_ENV_FILE="${WORKSPACE:-$(pwd)}/smoke-test.env"
EXTRA_ENV_ARGS=""
if [[ -f "${SMOKE_ENV_FILE}" ]]; then
    echo "[go-smoke] Loading smoke-test.env"
    while IFS= read -r line; do
        [[ -z "${line}" || "${line}" =~ ^# ]] && continue
        if [[ "${line}" =~ ^SMOKE_ ]]; then
            # SMOKE_ 前綴：腳本層設定（health path / port / timeout）
            export "${line?}"
        else
            # 其他：容器環境變數
            EXTRA_ENV_ARGS="${EXTRA_ENV_ARGS} -e ${line}"
        fi
    done < "${SMOKE_ENV_FILE}"
fi

HEALTH_PATH="${SMOKE_HEALTH_PATH:-/healthz}"
APP_PORT="${SMOKE_APP_PORT:-8080}"
MAX_WAIT="${SMOKE_MAX_WAIT:-60}"

# ── 啟動臨時容器 ──────────────────────────────────────────────────────────────
echo "[go-smoke] Starting container: ${CONTAINER_NAME}"
# shellcheck disable=SC2086
docker run -d --name "${CONTAINER_NAME}" ${EXTRA_ENV_ARGS} "${HARBOR_IMAGE}"

# ── 容器退出時自動清理（trap EXIT）；失敗先輸出容器 log 再清理 ────────────────
SMOKE_FAILED=0
cleanup() {
    if [[ "${SMOKE_FAILED}" == "1" ]]; then
        echo "" >&2
        echo "=== CONTAINER LOG (${CONTAINER_NAME}) ===" >&2
        docker logs "${CONTAINER_NAME}" 2>&1 || true
        echo "=== END OF CONTAINER LOG ===" >&2
        echo "" >&2
    fi
    echo "[go-smoke] Cleaning up container: ${CONTAINER_NAME}"
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
}
trap cleanup EXIT

# ── 輪詢等待應用程式啟動（Dockerfile-go 已含 curl）───────────────────────────
echo "[go-smoke] Waiting for HTTP 200 on ${HEALTH_PATH} (max ${MAX_WAIT}s)..."
elapsed=0
until docker exec "${CONTAINER_NAME}" \
    curl -sf "http://localhost:${APP_PORT}${HEALTH_PATH}" > /dev/null 2>&1; do
    sleep 5
    elapsed=$((elapsed + 5))
    echo "[go-smoke] Waiting... ${elapsed}s"
    if [[ ${elapsed} -ge ${MAX_WAIT} ]]; then
        SMOKE_FAILED=1
        report_error "SMOKE" "001" "Startup timeout after ${MAX_WAIT}s. Container log printed above."
        exit 1
    fi
done

echo "[go-smoke] Health check passed: ${HEALTH_PATH} → 200"
echo "[go-smoke] Smoke test passed."
