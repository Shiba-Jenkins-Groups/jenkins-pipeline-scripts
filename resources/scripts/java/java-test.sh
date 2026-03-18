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
# develop: Unit Test only
# main:    Unit Test + Coverage (TODO) + Integration (TODO)
# prod:    Unit Test + Coverage (TODO) + Integration (TODO) + Security (TODO)
# 其他:    Unit Test only

run_unit_test() {
    echo "[java-test] Running unit tests..."
    case "${BUILD_TOOL}" in
        maven)  cd "${WORKSPACE}" && ./mvnw test -B ;;
        gradle) cd "${WORKSPACE}" && ./gradlew test ;;
    esac
}

run_coverage() {
    echo "[java-test] TODO: Coverage (JaCoCo) not yet implemented."
}

run_integration_test() {
    echo "[java-test] TODO: Integration tests not yet implemented."
}

run_security_scan() {
    echo "[java-test] TODO: Security scan (OWASP) not yet implemented."
}

# ── 執行 ──────────────────────────────────────────────────────────────────────
run_unit_test

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
esac

echo "[java-test] Test completed."
