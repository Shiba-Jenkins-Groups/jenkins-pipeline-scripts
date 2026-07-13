#!/usr/bin/env bash
# ci.sh — CI 入口（自動偵測語言、buildTool、appName）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"

# ── 語言偵測（單一真相：detect.sh，本檔不再自帶偵測邏輯）─────────────────────
# detect.sh 輸出 KEY=VALUE；賦值失敗（偵測不到語言）時 set -e 直接終止
DETECT_OUTPUT="$(bash "${SCRIPT_DIR}/detect.sh")"
eval "${DETECT_OUTPUT}"
export LANGUAGE BUILD_TOOL

echo "[ci] Detected language: ${LANGUAGE}"
echo "[ci] Detected buildTool: ${BUILD_TOOL}"

# ── 慣例執行：build → test → archive ─────────────────────────────────────────
# 慣例路徑 {lang}/{lang}-{step}.sh；新增語言毋須修改本檔（OCP）
# 各語言的 env 前置（java-env / go-env）由各 step 腳本自行 source
for step in build test archive; do
    bash "${SCRIPT_DIR}/${LANGUAGE}/${LANGUAGE}-${step}.sh"
done

echo "[ci] CI completed successfully."
