#!/usr/bin/env bash
# go/go-archive.sh — Go binary 版本管理
# 版本來源優先序：VERSION 檔 > CHANGELOG.md 首個 ## [x.y.z] > git describe > 0.0.0
# （Go 無 pom.xml，採 Keep a Changelog / SemVer 慣例讀取版本）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"
source "${SCRIPT_DIR}/common/git-tag.sh"
source "${SCRIPT_DIR}/common/version.sh"
source "${SCRIPT_DIR}/common/nexus-upload.sh"
source "${SCRIPT_DIR}/go/go-env.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
BRANCH="${GIT_BRANCH:-unknown}"
BRANCH="${BRANCH#origin/}"
BUILD_NUMBER="${BUILD_NUMBER:?BUILD_NUMBER is required}"
cd "${WORKSPACE}"

# ── appName：go.mod module path 最後一段 ─────────────────────────────────────
APP_NAME="$(basename "$(go list -m)")"

# ── appVersion：統一契約 VERSION > CHANGELOG.md > git describe > 0.0.0 ──────
# Go 無原生 version 欄位（native 傳空），優先序由 common/version.sh 統一維護
APP_VERSION="$(resolve_app_version go "")"

# RUNTIME_VERSION：runtime base 是 debian slim（對應 Dockerfile-go ARG 預設；
# Go 1.26 linux/arm64 ELF 帶 glibc loader，musl/alpine 不可用）
RUNTIME_VERSION="bookworm-slim"

export APP_NAME APP_VERSION RUNTIME_VERSION

BASE_VERSION="${APP_VERSION%-SNAPSHOT}"
BASE_VERSION="${BASE_VERSION%-RC}"

echo "[go-archive] appName: ${APP_NAME}"
echo "[go-archive] appVersion: ${APP_VERSION} (base: ${BASE_VERSION})"
echo "[go-archive] branch: ${BRANCH}"
echo "[go-archive] buildNumber: ${BUILD_NUMBER}"

# ── 產出物命名（同 Java 慣例，Go binary 無副檔名）────────────────────────────
# develop: {appName}-dev-{baseVersion}-SNAPSHOT-{buildNumber}
# main:    {appName}-main-{baseVersion}-RC-{buildNumber}
# prod:    {appName}-prod-{baseVersion}
resolve_artifact_name() {
    local safe_branch
    safe_branch="$(echo "${BRANCH}" | tr '/' '-' | tr '_' '-')"

    case "${BRANCH}" in
        develop) echo "${APP_NAME}-dev-${BASE_VERSION}-SNAPSHOT-${BUILD_NUMBER}" ;;
        main)    echo "${APP_NAME}-main-${BASE_VERSION}-RC-${BUILD_NUMBER}" ;;
        prod)    echo "${APP_NAME}-prod-${BASE_VERSION}" ;;
        *)       echo "${APP_NAME}-${safe_branch}-${BASE_VERSION}-${BUILD_NUMBER}" ;;
    esac
}

ARTIFACT_NAME="$(resolve_artifact_name)"
echo "[go-archive] artifact: ${ARTIFACT_NAME}"

# ── 取得 go-build.sh 產出的 binary ───────────────────────────────────────────
SOURCE_BIN="${WORKSPACE}/.gobuild/app"
if [[ ! -f "${SOURCE_BIN}" ]]; then
    echo "[ERROR] No binary found at ${SOURCE_BIN}. Did go-build.sh succeed?" >&2
    exit 1
fi

ARTIFACT_PATH="/tmp/${ARTIFACT_NAME}"
cp "${SOURCE_BIN}" "${ARTIFACT_PATH}"

# ── 上傳 Nexus raw-artifacts（#4b 起單一真相：版本化路徑＝防覆蓋防競態）──────
NEXUS_ARTIFACT_URL="$(nexus_upload_artifact "${APP_NAME}" "${BRANCH}" "${BASE_VERSION}" "${BUILD_NUMBER}" "${ARTIFACT_PATH}")"
# staging 檔保留供 Docker Build 精確取用（拋棄式 agent，/tmp 隨容器回收，不需 rm）

# ── 寫入 build.env（供後續 Docker Build stage 讀取）──────────────────────────
mkdir -p "${WORKSPACE}/.pipeline"
cat > "${WORKSPACE}/.pipeline/build.env" <<EOF
APP_NAME=${APP_NAME}
APP_VERSION=${APP_VERSION}
BASE_VERSION=${BASE_VERSION}
RUNTIME_VERSION=${RUNTIME_VERSION}
BRANCH=${BRANCH}
BUILD_NUMBER=${BUILD_NUMBER}
ARTIFACT_NAME=${ARTIFACT_NAME}
ARTIFACT_LOCAL=${ARTIFACT_PATH}
NEXUS_ARTIFACT_URL=${NEXUS_ARTIFACT_URL}
EOF
echo "[go-archive] build.env written."

# ── Git Tag ───────────────────────────────────────────────────────────────────
GIT_TAG_NAME="$(resolve_git_tag "${BRANCH}" "${BUILD_NUMBER}")"
export GIT_TAG_NAME
echo "[go-archive] git tag: ${GIT_TAG_NAME}"
push_git_tag "${GIT_TAG_NAME}"

echo "[go-archive] Archive completed."
