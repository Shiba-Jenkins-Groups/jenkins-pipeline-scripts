#!/usr/bin/env bash
# common/secret-scan.sh — 秘密掃描（gitleaks；Security Phase 2 v1.7.0）
#
# 掃描 git 歷史，偵測洩漏的秘密（API key / token / 密碼 / 私鑰 / 高熵字串）。
# 政策：DO_SECRET_SCAN / SECRET_SCAN_EXIT_CODE 由 branch-policy.sh 單一真相表決定
#       （全 branch fail——秘密洩漏處處 critical，feature branch 也擋）。
# --redact：報告與 log 遮罩秘密實際值，避免「找到秘密」的報告本身二次曝露。
# 專案可放 .gitleaks.toml（自訂規則/allowlist）或 .gitleaksignore（豁免已知誤判），gitleaks 自動採用。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
cd "${WORKSPACE}"

EXIT_CODE="${SECRET_SCAN_EXIT_CODE:-1}"
REPORT="${WORKSPACE}/gitleaks-report.json"

echo "[secret-scan] gitleaks $(gitleaks version) — 掃描 git 歷史（redacted）..."

# gitleaks 8.x：git 歷史掃描用 `gitleaks git [path]`（舊版 `detect` 已移除）
# 找不到秘密 → exit 0；找到 → exit ${EXIT_CODE}（政策＝1，觸發 ERR trap → stage FAILURE）
# --redact：遮罩秘密值；報告一律產出（供 archive），供人工查 檔案:行:規則（不含秘密明文）
gitleaks git "${WORKSPACE}" \
    --redact \
    --report-format json \
    --report-path "${REPORT}" \
    --exit-code "${EXIT_CODE}" \
    --no-banner

echo "[secret-scan] 未偵測到秘密洩漏。✅"
