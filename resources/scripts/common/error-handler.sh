#!/usr/bin/env bash
# common/error-handler.sh — 共用錯誤處理

set -euo pipefail

trap_error() {
    local exit_code=$?
    local line_number=${1:-unknown}
    echo "[ERROR] Script failed at line ${line_number} with exit code ${exit_code}" >&2
    exit "${exit_code}"
}

trap 'trap_error ${LINENO}' ERR
