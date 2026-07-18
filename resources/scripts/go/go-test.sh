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

# ── govulncheck：Go 專屬依賴／標準庫弱點掃描（可達性分析）────────────────────
# 為何在這裡而不在 Dependency Scan stage：那個 stage 跑的是 OWASP Dependency-Check
# （只實作 java/maven），且政策上只在 main/prod 開——對 Go 等於空轉，弱點要到發版當天
# 才由 Trivy 掃 binary 間接抓到（2026-07-19 實例：x/crypto／x/net 的 HIGH CVE 一路綠到
# prod 發版才被擋，把發版日變成修依賴日）。放這裡＝每個 branch 都跑，訊號最早出現。
#
# 失敗政策比照 Trivy 的 SCAN_EXIT_CODE 慣例（GO_VULN_EXIT_CODE：dev/main warn、prod fail）——
# 對共用 library 而言這是必要的保守：govulncheck 也會回報「標準庫」弱點，那綁 agent 的 Go
# 版本，新 CVE 一落地就會讓所有 Go 專案的 build 一起紅。要收緊成 develop 即 fail，
# 改 branch-policy.sh 的 develop 那行為 1 即可。
run_govulncheck() {
    if ! command -v govulncheck >/dev/null 2>&1; then
        echo "╔══════════════════════════════════════════════════════════════╗"
        echo "║ [go-test] ⚠ govulncheck 未安裝，弱點掃描已跳過               ║"
        echo "║ 這不是「沒有弱點」，是「沒有檢查」——agent image 需重建：      ║"
        echo "║   cd jenkins/docker-compose/agent && bash rebuild.sh          ║"
        echo "╚══════════════════════════════════════════════════════════════╝"
        return 0
    fi
    local exit_code="${GO_VULN_EXIT_CODE:-0}"
    echo "[go-test] govulncheck 掃描（可達性分析；exit-code 政策=${exit_code}）..."
    if govulncheck ./...; then
        echo "[go-test] ✅ 未發現可達弱點"
        return 0
    fi
    if [[ "${exit_code}" == "1" ]]; then
        report_error "GOVULN" "001" "發現可達的已知弱點（上方為 govulncheck 報告）。prod 分支不得帶病發版：升級對應依賴或 Go 版本後重試。"
        exit 1
    fi
    echo "[go-test] ⚠ 發現可達弱點（本分支政策為警告不阻斷；prod 分支會 fail）——請及早處理，別留到發版日。"
}

# 語言中立報告契約（#2）：gotestsum 產 JUnit XML → reports/junit/（測試失敗仍寫檔）
run_unit_test() {
    echo "[go-test] Running go vet..."
    # shellcheck disable=SC2086
    go vet ${GO_TEST_PKGS}
    run_govulncheck
    echo "[go-test] Running unit tests (gotestsum → JUnit)..."
    mkdir -p "${WORKSPACE}/reports/junit"
    # shellcheck disable=SC2086
    gotestsum --format testname --junitfile "${WORKSPACE}/reports/junit/go-tests.xml" -- ${GO_TEST_PKGS}
}

run_coverage() {
    echo "[go-test] Running go vet..."
    # shellcheck disable=SC2086
    go vet ${GO_TEST_PKGS}
    run_govulncheck
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
