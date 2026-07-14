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

# ── 測試檔位（TEST_LEVEL）─────────────────────────────────────────────────────
# 檔位由 branch-policy.sh 單一真相表決定（unit / coverage），本檔不自帶 branch case
# 註：-race 需要 CGO + gcc，agent 未裝 C toolchain，race 檢測由開發機本地執行
if [[ -z "${TEST_LEVEL:-}" ]]; then
    # shellcheck source=../common/branch-policy.sh
    source "${SCRIPT_DIR}/common/branch-policy.sh"
    derive_branch_policy "${BRANCH}"
fi
echo "[go-test] Test level: ${TEST_LEVEL}"

# 語言中立報告契約（#2）：gotestsum 產 JUnit XML → reports/junit/（測試失敗仍寫檔）
run_unit_test() {
    echo "[go-test] Running go vet..."
    # shellcheck disable=SC2086
    go vet ${GO_TEST_PKGS}
    echo "[go-test] Running unit tests (gotestsum → JUnit)..."
    mkdir -p "${WORKSPACE}/reports/junit"
    # shellcheck disable=SC2086
    gotestsum --format testname --junitfile "${WORKSPACE}/reports/junit/go-tests.xml" -- ${GO_TEST_PKGS}
}

run_coverage() {
    echo "[go-test] Running go vet..."
    # shellcheck disable=SC2086
    go vet ${GO_TEST_PKGS}
    echo "[go-test] Running unit tests with coverage (gotestsum → JUnit)..."
    mkdir -p "${WORKSPACE}/reports/junit" "${WORKSPACE}/reports/coverage"
    # shellcheck disable=SC2086
    gotestsum --format testname --junitfile "${WORKSPACE}/reports/junit/go-tests.xml" \
        -- -coverprofile="${WORKSPACE}/coverage.out" ${GO_TEST_PKGS}
    # 覆蓋率總覽（僅輸出，不設門檻；門檻策略待專案成熟後補）
    go tool cover -func="${WORKSPACE}/coverage.out" | tail -1
    # Coverage HTML → 契約路徑 reports/coverage/index.html
    go tool cover -html="${WORKSPACE}/coverage.out" -o "${WORKSPACE}/reports/coverage/index.html"
}

case "${TEST_LEVEL}" in
    coverage)
        run_coverage
        ;;
    *)
        run_unit_test
        ;;
esac

echo "[go-test] Test completed."
