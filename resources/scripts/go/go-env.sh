#!/usr/bin/env bash
# go/go-env.sh — Go 專案共用環境（go-build / go-test / go-archive 皆 source）
# 讀取專案根目錄 go-pipeline.env（選用），並輸出 GO_MAIN_PKG / GO_TEST_PKGS 預設值

WORKSPACE="${WORKSPACE:-$(pwd)}"

# 專案宣告檔（選用）：多 main package 專案在此指定 artifact 入口
GO_PIPELINE_ENV="${WORKSPACE}/go-pipeline.env"
if [[ -f "${GO_PIPELINE_ENV}" ]]; then
    echo "[go-env] Loading go-pipeline.env"
    # shellcheck source=/dev/null
    source "${GO_PIPELINE_ENV}"
fi

export GO_MAIN_PKG="${GO_MAIN_PKG:-.}"
export GO_TEST_PKGS="${GO_TEST_PKGS:-./...}"

# 依賴快取：Nexus 目前無 go proxy repo，先走官方 proxy（未來建立 go-group 後改指 Nexus）
export GOPROXY="${GOPROXY:-https://proxy.golang.org,direct}"
