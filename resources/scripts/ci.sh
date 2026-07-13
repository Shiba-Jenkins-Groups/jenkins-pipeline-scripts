#!/usr/bin/env bash
# ci.sh — CI 入口（自動偵測語言、buildTool、appName）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"

# ── 自動偵測語言與 buildTool ──────────────────────────────────────────────────
detect_language() {
    # go.mod 優先：Go 專案可能同時帶前端工具檔（package.json），有 go.mod 即視為 Go 專案
    if [[ -f "${WORKSPACE}/go.mod" ]]; then
        echo "go"
    elif [[ -f "${WORKSPACE}/pom.xml" ]]; then
        echo "java"
    elif [[ -f "${WORKSPACE}/build.gradle" ]]; then
        echo "java"
    elif [[ -f "${WORKSPACE}/package.json" ]]; then
        echo "node"
    elif [[ -f "${WORKSPACE}/requirements.txt" ]] || [[ -f "${WORKSPACE}/pyproject.toml" ]]; then
        echo "python"
    else
        echo "[ERROR] Cannot detect project language. No go.mod / pom.xml / build.gradle / package.json / requirements.txt found." >&2
        exit 1
    fi
}

detect_build_tool() {
    local language="${1}"
    case "${language}" in
        go)
            echo "go"
            ;;
        java)
            if [[ -f "${WORKSPACE}/pom.xml" ]]; then
                echo "maven"
            else
                echo "gradle"
            fi
            ;;
        node)
            if [[ -f "${WORKSPACE}/yarn.lock" ]]; then
                echo "yarn"
            else
                echo "npm"
            fi
            ;;
        python)
            echo "pip"
            ;;
    esac
}

export LANGUAGE="$(detect_language)"
export BUILD_TOOL="$(detect_build_tool "${LANGUAGE}")"

echo "[ci] Detected language: ${LANGUAGE}"
echo "[ci] Detected buildTool: ${BUILD_TOOL}"

# ── 執行對應語言的 CI 流程 ────────────────────────────────────────────────────
case "${LANGUAGE}" in
    go)
        # go-env.sh 由各 go-*.sh 自行 source（讀取專案 go-pipeline.env 宣告）
        bash "${SCRIPT_DIR}/go/go-build.sh"
        bash "${SCRIPT_DIR}/go/go-test.sh"
        bash "${SCRIPT_DIR}/go/go-archive.sh"
        ;;
    java)
        # java-env.sh 由 java-build.sh 自行 source，此處不重複呼叫
        bash "${SCRIPT_DIR}/java/java-build.sh"
        bash "${SCRIPT_DIR}/java/java-test.sh"
        bash "${SCRIPT_DIR}/java/java-archive.sh"
        ;;
    node)
        bash "${SCRIPT_DIR}/node/node-build.sh"
        bash "${SCRIPT_DIR}/node/node-test.sh"
        bash "${SCRIPT_DIR}/node/node-archive.sh"
        ;;
    python)
        bash "${SCRIPT_DIR}/python/python-build.sh"
        bash "${SCRIPT_DIR}/python/python-test.sh"
        bash "${SCRIPT_DIR}/python/python-archive.sh"
        ;;
    *)
        echo "[ERROR] Unsupported language: ${LANGUAGE}" >&2
        exit 1
        ;;
esac

echo "[ci] CI completed successfully."
