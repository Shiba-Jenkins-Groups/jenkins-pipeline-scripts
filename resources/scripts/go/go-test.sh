#!/usr/bin/env bash
# go/go-test.sh — Go Test（依 branch 決定範圍）
# 測試範圍由專案 go-pipeline.env 的 GO_TEST_PKGS 宣告（預設 ./...）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"
source "${SCRIPT_DIR}/go/go-env.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
BRANCH="${GIT_BRANCH:-unknown}"
BRANCH="${BRANCH#origin/}"
cd "${WORKSPACE}"

echo "[go-test] Branch: ${BRANCH}"
echo "[go-test] Test packages: ${GO_TEST_PKGS}"

# ── 各 branch 測試範圍 ────────────────────────────────────────────────────────
# develop / 其他：go vet + unit test（快速反饋）
# main / prod：go vet + unit test + coverage 總覽
# 註：-race 需要 CGO + gcc，agent 未裝 C toolchain，race 檢測由開發機本地執行

run_unit_test() {
    echo "[go-test] Running go vet..."
    # shellcheck disable=SC2086
    go vet ${GO_TEST_PKGS}
    echo "[go-test] Running unit tests..."
    # shellcheck disable=SC2086
    go test ${GO_TEST_PKGS}
}

run_coverage() {
    echo "[go-test] Running go vet..."
    # shellcheck disable=SC2086
    go vet ${GO_TEST_PKGS}
    echo "[go-test] Running unit tests with coverage..."
    # shellcheck disable=SC2086
    go test -coverprofile="${WORKSPACE}/coverage.out" ${GO_TEST_PKGS}
    # 覆蓋率總覽（僅輸出，不設門檻；門檻策略待專案成熟後補）
    go tool cover -func="${WORKSPACE}/coverage.out" | tail -1
}

case "${BRANCH}" in
    main|prod)
        run_coverage
        ;;
    *)
        run_unit_test
        ;;
esac

echo "[go-test] Test completed."
