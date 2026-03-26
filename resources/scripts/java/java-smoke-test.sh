#!/usr/bin/env bash
# java/java-smoke-test.sh — Java Smoke Test
# Harbor image 起臨時容器，驗證 Spring Boot Actuator health 回應 UP
# 清理：trap EXIT 確保容器無論成功或失敗都被移除

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

HARBOR_IMAGE="${1:?HARBOR_IMAGE is required}"
CONTAINER_NAME="${APP_NAME:-app}-smoke-${BUILD_NUMBER:-0}"
HEALTH_PATH="${SMOKE_HEALTH_PATH:-/actuator/health}"
APP_PORT="${SMOKE_APP_PORT:-8080}"
MAX_WAIT="${SMOKE_MAX_WAIT:-60}"

# ── 讀取專案自訂 smoke test 環境變數（選用）────────────────────────────────
# 專案根目錄放置 smoke-test.env，每行一個 KEY=VALUE
# 用途：注入應用程式啟動所需的最小設定（例如排除不必要的 AutoConfiguration）
SMOKE_ENV_FILE="${WORKSPACE:-$(pwd)}/smoke-test.env"
EXTRA_ENV_ARGS=""
if [[ -f "${SMOKE_ENV_FILE}" ]]; then
    echo "[java-smoke] Loading smoke-test.env"
    while IFS= read -r line; do
        # 跳過空行與注解
        [[ -z "${line}" || "${line}" =~ ^# ]] && continue
        EXTRA_ENV_ARGS="${EXTRA_ENV_ARGS} -e ${line}"
    done < "${SMOKE_ENV_FILE}"
fi

# ── 啟動臨時容器 ──────────────────────────────────────────────────────────────
echo "[java-smoke] Starting container: ${CONTAINER_NAME}"
# shellcheck disable=SC2086
docker run -d --name "${CONTAINER_NAME}" ${EXTRA_ENV_ARGS} "${HARBOR_IMAGE}"

# ── 容器退出時自動清理（trap EXIT）───────────────────────────────────────────
cleanup() {
    echo "[java-smoke] Cleaning up container: ${CONTAINER_NAME}"
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
}
trap cleanup EXIT

# ── 輪詢等待應用程式啟動 ──────────────────────────────────────────────────────
echo "[java-smoke] Waiting for startup (max ${MAX_WAIT}s)..."
elapsed=0
until docker exec "${CONTAINER_NAME}" \
    curl -sf "http://localhost:${APP_PORT}${HEALTH_PATH}" > /dev/null 2>&1; do
    sleep 5
    elapsed=$((elapsed + 5))
    echo "[java-smoke] Waiting... ${elapsed}s"
    if [[ ${elapsed} -ge ${MAX_WAIT} ]]; then
        echo "[ERROR] Smoke test timeout after ${MAX_WAIT}s" >&2
        exit 1
    fi
done

# ── 驗證 health status ────────────────────────────────────────────────────────
STATUS=$(docker exec "${CONTAINER_NAME}" \
    curl -s "http://localhost:${APP_PORT}${HEALTH_PATH}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))")

echo "[java-smoke] Health status: ${STATUS}"
[[ "${STATUS}" == "UP" ]] || {
    echo "[ERROR] Expected UP, got ${STATUS}" >&2
    exit 1
}

echo "[java-smoke] Smoke test passed."
