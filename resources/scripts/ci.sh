#!/usr/bin/env bash
# ci.sh — CI 入口（自動偵測語言、buildTool、appName）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"

# ── 自動偵測語言與 buildTool ──────────────────────────────────────────────────
detect_language() {
    if [[ -f "${WORKSPACE}/pom.xml" ]]; then
        echo "java"
    elif [[ -f "${WORKSPACE}/build.gradle" ]]; then
        echo "java"
    elif [[ -f "${WORKSPACE}/package.json" ]]; then
        echo "node"
    elif [[ -f "${WORKSPACE}/requirements.txt" ]] || [[ -f "${WORKSPACE}/pyproject.toml" ]]; then
        echo "python"
    else
        echo "[ERROR] Cannot detect project language. No pom.xml / build.gradle / package.json / requirements.txt found." >&2
        exit 1
    fi
}

detect_build_tool() {
    local language="${1}"
    case "${language}" in
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
    java)
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
