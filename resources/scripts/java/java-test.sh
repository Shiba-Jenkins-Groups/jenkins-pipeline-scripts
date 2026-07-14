#!/usr/bin/env bash
# java/java-test.sh — Java Test（依 branch 決定範圍）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
BUILD_TOOL="${BUILD_TOOL:-maven}"
BRANCH="${GIT_BRANCH:-unknown}"

# 依 pom.xml / build.gradle 宣告的版本切換 JAVA_HOME，與 java-build.sh 保持一致
source "${SCRIPT_DIR}/java/java-env.sh"
BRANCH="${BRANCH#origin/}"

echo "[java-test] Branch: ${BRANCH}"

# ── 測試檔位（TEST_LEVEL）─────────────────────────────────────────────────────
# 檔位由 branch-policy.sh 單一真相表決定（unit / coverage），本檔不自帶 branch case
# Pipeline 情境：Detect stage 已注入 env；standalone 情境：自行推導
if [[ -z "${TEST_LEVEL:-}" ]]; then
    # shellcheck source=../common/branch-policy.sh
    source "${SCRIPT_DIR}/common/branch-policy.sh"
    derive_branch_policy "${BRANCH}"
fi
echo "[java-test] Test level: ${TEST_LEVEL}"

run_unit_test() {
    # develop / 其他 branch 使用，只跑 test phase，速度最快
    echo "[java-test] Running unit tests..."
    case "${BUILD_TOOL}" in
        maven)  cd "${WORKSPACE}" && ./mvnw test -B ;;
        gradle) cd "${WORKSPACE}" && ./gradlew test ;;
    esac
}

run_coverage() {
    # main / prod branch 使用：mvnw verify 包含 compile → test → JaCoCo report
    # JaCoCo 覆蓋率門檻設定於 pom.xml <configuration><rules>，門檻不足時 Maven 自動 fail
    # HTML 報告輸出：target/site/jacoco/index.html
    echo "[java-test] Running coverage analysis (JaCoCo)..."
    case "${BUILD_TOOL}" in
        maven)  cd "${WORKSPACE}" && ./mvnw verify -B ;;
        gradle) cd "${WORKSPACE}" && ./gradlew test jacocoTestReport jacocoTestCoverageVerification ;;
    esac
}

run_integration_test() {
    echo "[java-test] TODO: Integration tests not yet implemented."
}

# 語言中立報告契約（#2）：把 Maven/Gradle 產物搬到統一路徑，groovy 只認 reports/
#   JUnit XML → reports/junit/    Coverage HTML → reports/coverage/
collect_reports() {
    cd "${WORKSPACE}"
    mkdir -p reports/junit reports/coverage
    # surefire（Maven）／test-results（Gradle）JUnit XML
    find . -path ./reports -prune -o \
        \( -path '*/surefire-reports/*.xml' -o -path '*/test-results/*/*.xml' \) -print 2>/dev/null \
        | while read -r xml; do cp "${xml}" reports/junit/ 2>/dev/null || true; done
    # JaCoCo HTML（coverage 檔位才有；index.html + 資源）
    if [[ -d target/site/jacoco ]]; then
        cp -r target/site/jacoco/. reports/coverage/ 2>/dev/null || true
    elif [[ -d build/reports/jacoco/test/html ]]; then
        cp -r build/reports/jacoco/test/html/. reports/coverage/ 2>/dev/null || true
    fi
    echo "[java-test] Reports collected → reports/junit ($(find reports/junit -name '*.xml' | wc -l | tr -d ' ') xml), reports/coverage"
}

# ── 執行 ──────────────────────────────────────────────────────────────────────
# coverage 檔位用 run_coverage()（內部已包含 unit test），避免 Maven 重複執行
# Security scan（OWASP / gitleaks）由 Phase 2（v1.7.x）以獨立政策旗標實作，不在測試檔位內
case "${TEST_LEVEL}" in
    coverage)
        run_coverage
        run_integration_test
        ;;
    *)
        # unit 檔位（develop / feature branch）：快速 unit test
        run_unit_test
        ;;
esac

collect_reports

echo "[java-test] Test completed."
