#!/usr/bin/env bash
# go/go-build.sh — Go Build（go build）
# 專案可於根目錄放置 go-pipeline.env 宣告：
#   GO_MAIN_PKG=./cmd/app/server   ← main package 路徑（預設 .，多 main 專案必填）
#   GO_TEST_PKGS=./internal/...    ← 測試範圍（預設 ./...，go-test.sh 使用）
#   GO_LDFLAGS=-s -w               ← 額外 ldflags（選用）
# 另：GO_BUILD_TAGS 由 ciPipeline.groovy 依部署 namespace 注入（非本檔宣告，見 devBuildTags 參數）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"
source "${SCRIPT_DIR}/go/go-env.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
cd "${WORKSPACE}"

echo "[go-build] Go version: $(go version)"
echo "[go-build] Main package: ${GO_MAIN_PKG}"

# build tags（選用）：由 ciPipeline 依部署 namespace 注入（如 develop→devseed）。
# 未設時陣列為空、不帶 -tags，行為與過往完全一致。
TAGS_ARG=()
if [ -n "${GO_BUILD_TAGS:-}" ]; then
    TAGS_ARG=(-tags "${GO_BUILD_TAGS}")
    echo "[go-build] Build tags: ${GO_BUILD_TAGS}"
fi

# 先全量編譯驗證（含未進 artifact 的套件，等同 L0 的 go build ./...）
go build "${TAGS_ARG[@]}" ./...

# 產出物：CGO_ENABLED=0 靜態 binary（alpine runtime image 可直接執行）
# 輸出至 .gobuild/（不污染專案 bin/ 慣例，隨 cleanWs 清理）
mkdir -p "${WORKSPACE}/.gobuild"
CGO_ENABLED=0 go build "${TAGS_ARG[@]}" -ldflags "${GO_LDFLAGS:--s -w}" \
    -o "${WORKSPACE}/.gobuild/app" "${GO_MAIN_PKG}"

echo "[go-build] Binary: ${WORKSPACE}/.gobuild/app ($(du -h "${WORKSPACE}/.gobuild/app" | cut -f1))"
echo "[go-build] Build completed."
