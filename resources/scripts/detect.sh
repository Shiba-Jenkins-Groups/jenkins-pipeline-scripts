#!/usr/bin/env bash
# detect.sh — 語言與建置工具自動偵測
# 輸出格式：KEY=VALUE（供 ciPipeline.groovy 讀取並設定 env 變數）

set -euo pipefail

WORKSPACE="${WORKSPACE:-$(pwd)}"
cd "${WORKSPACE}"

if [[ -f "pom.xml" ]]; then
    LANGUAGE=java
    BUILD_TOOL=maven
elif [[ -f "build.gradle" ]]; then
    LANGUAGE=java
    BUILD_TOOL=gradle
elif [[ -f "package.json" ]]; then
    LANGUAGE=node
    if [[ -f "yarn.lock" ]]; then
        BUILD_TOOL=yarn
    else
        BUILD_TOOL=npm
    fi
elif [[ -f "requirements.txt" ]] || [[ -f "pyproject.toml" ]]; then
    LANGUAGE=python
    BUILD_TOOL=pip
else
    echo "[ERROR] Cannot detect project language. No pom.xml / build.gradle / package.json / requirements.txt found." >&2
    exit 1
fi

echo "LANGUAGE=${LANGUAGE}"
echo "BUILD_TOOL=${BUILD_TOOL}"
