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

# ── 各 branch 測試範圍 ────────────────────────────────────────────────────────
# develop / 其他：Unit Test only（快速反饋）
# main：Coverage（包含 Unit Test）+ Integration（TODO）
# prod：Coverage（包含 Unit Test）+ Integration（TODO）+ Security（TODO）

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

run_security_scan() {
    echo "[java-test] TODO: Security scan (OWASP) not yet implemented."
}

# ── 執行 ──────────────────────────────────────────────────────────────────────
# main / prod 用 run_coverage()（內部已包含 unit test），避免 Maven 重複執行
case "${BRANCH}" in
    main)
        run_coverage
        run_integration_test
        ;;
    prod)
        run_coverage
        run_integration_test
        run_security_scan
        ;;
    *)
        # develop 及 feature branch：快速 unit test
        run_unit_test
        ;;
esac

echo "[java-test] Test completed."
