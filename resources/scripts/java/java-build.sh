#!/usr/bin/env bash
# java/java-build.sh — Java Build（Maven / Gradle）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
BUILD_TOOL="${BUILD_TOOL:-maven}"

# 依 pom.xml / build.gradle 宣告的版本切換 JAVA_HOME，確保使用正確 JDK
source "${SCRIPT_DIR}/java/java-env.sh"

echo "[java-build] Build tool: ${BUILD_TOOL}"

case "${BUILD_TOOL}" in
    maven)
        cd "${WORKSPACE}"
        ./mvnw clean package -DskipTests -B
        ;;
    gradle)
        cd "${WORKSPACE}"
        ./gradlew clean build -x test
        ;;
    *)
        echo "[ERROR] Unsupported build tool: ${BUILD_TOOL}" >&2
        exit 1
        ;;
esac

echo "[java-build] Build completed."
