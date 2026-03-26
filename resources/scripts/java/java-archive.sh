#!/usr/bin/env bash
# java/java-archive.sh — JAR 版本管理

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"
source "${SCRIPT_DIR}/common/archive-base.sh"
source "${SCRIPT_DIR}/common/git-tag.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
BUILD_TOOL="${BUILD_TOOL:-maven}"
BRANCH="${GIT_BRANCH:-unknown}"
BRANCH="${BRANCH#origin/}"
BUILD_NUMBER="${BUILD_NUMBER:?BUILD_NUMBER is required}"

# ── 讀取 pom.xml / build.gradle 版本資訊 ────────────────────────────────────
read_maven_info() {
    APP_NAME="$(./mvnw help:evaluate -Dexpression=project.artifactId -q -DforceStdout -B)"
    APP_VERSION="$(./mvnw help:evaluate -Dexpression=project.version -q -DforceStdout -B)"
    # Maven help:evaluate 可能回傳 JVM 系統屬性（如 21.0.10）而非 pom.xml 定義的主版本號
    # eclipse-temurin image tag 只支援主版本號（如 21），截取第一段確保相容
    local raw_version
    raw_version="$(./mvnw help:evaluate -Dexpression=java.version -q -DforceStdout -B 2>/dev/null || echo '17')"
    RUNTIME_VERSION="${raw_version%%.*}"
}

read_gradle_info() {
    APP_NAME="$(./gradlew properties -q | grep '^name:' | awk '{print $2}')"
    APP_VERSION="$(./gradlew properties -q | grep '^version:' | awk '{print $2}')"
    RUNTIME_VERSION="17"
}

cd "${WORKSPACE}"
case "${BUILD_TOOL}" in
    maven)  read_maven_info ;;
    gradle) read_gradle_info ;;
esac

export APP_NAME APP_VERSION RUNTIME_VERSION

# pom.xml version 可能已含 -SNAPSHOT，統一去除後各 branch 自行加後綴
BASE_VERSION="${APP_VERSION%-SNAPSHOT}"
BASE_VERSION="${BASE_VERSION%-RC}"

echo "[java-archive] appName: ${APP_NAME}"
echo "[java-archive] appVersion: ${APP_VERSION} (base: ${BASE_VERSION})"
echo "[java-archive] branch: ${BRANCH}"
echo "[java-archive] buildNumber: ${BUILD_NUMBER}"

# ── 產出物命名 ────────────────────────────────────────────────────────────────
# develop: {appName}-dev-{baseVersion}-SNAPSHOT-{buildNumber}.jar
# main:    {appName}-main-{baseVersion}-RC-{buildNumber}.jar
# prod:    {appName}-prod-{baseVersion}.jar
# 其他:    {appName}-{branch}-{baseVersion}-{buildNumber}.jar
resolve_artifact_name() {
    local safe_branch
    safe_branch="$(echo "${BRANCH}" | tr '/' '-' | tr '_' '-')"

    case "${BRANCH}" in
        develop)
            echo "${APP_NAME}-dev-${BASE_VERSION}-SNAPSHOT-${BUILD_NUMBER}.jar"
            ;;
        main)
            echo "${APP_NAME}-main-${BASE_VERSION}-RC-${BUILD_NUMBER}.jar"
            ;;
        prod)
            echo "${APP_NAME}-prod-${BASE_VERSION}.jar"
            ;;
        *)
            echo "${APP_NAME}-${safe_branch}-${BASE_VERSION}-${BUILD_NUMBER}.jar"
            ;;
    esac
}

ARTIFACT_NAME="$(resolve_artifact_name)"
echo "[java-archive] artifact: ${ARTIFACT_NAME}"

# ── 找到 JAR 並重新命名 ───────────────────────────────────────────────────────
if [[ "${BUILD_TOOL}" == "maven" ]]; then
    SOURCE_JAR="$(find "${WORKSPACE}/target" -maxdepth 1 -name "*.jar" ! -name "*sources*" ! -name "*javadoc*" | head -1)"
else
    SOURCE_JAR="$(find "${WORKSPACE}/build/libs" -maxdepth 1 -name "*.jar" ! -name "*sources*" | head -1)"
fi

if [[ -z "${SOURCE_JAR}" ]]; then
    echo "[ERROR] No JAR found after build." >&2
    exit 1
fi

ARTIFACT_PATH="/tmp/${ARTIFACT_NAME}"
cp "${SOURCE_JAR}" "${ARTIFACT_PATH}"

# ── 存入 release/backup ───────────────────────────────────────────────────────
archive_artifact "${APP_NAME}" "${ARTIFACT_PATH}"
rm -f "${ARTIFACT_PATH}"

# ── 寫入 build.env（供後續 Docker Build stage 讀取）────────────────────────
mkdir -p "${WORKSPACE}/.pipeline"
cat > "${WORKSPACE}/.pipeline/build.env" <<EOF
APP_NAME=${APP_NAME}
APP_VERSION=${APP_VERSION}
BASE_VERSION=${BASE_VERSION}
RUNTIME_VERSION=${RUNTIME_VERSION}
BRANCH=${BRANCH}
BUILD_NUMBER=${BUILD_NUMBER}
ARTIFACT_NAME=${ARTIFACT_NAME}
EOF
echo "[java-archive] build.env written."

# ── Git Tag ───────────────────────────────────────────────────────────────────
GIT_TAG_NAME="$(resolve_git_tag "${BRANCH}" "${BUILD_NUMBER}")"
export GIT_TAG_NAME
echo "[java-archive] git tag: ${GIT_TAG_NAME}"
push_git_tag "${GIT_TAG_NAME}"

echo "[java-archive] Archive completed."
