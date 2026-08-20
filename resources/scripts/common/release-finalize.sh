#!/usr/bin/env bash
# strict verification 完成後才執行的 PROD release finalization。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/error-handler.sh"
source "${SCRIPT_DIR}/nexus-upload.sh"
source "${SCRIPT_DIR}/git-tag.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
BUILD_ENV="${WORKSPACE}/.pipeline/build.env"
[[ -f "${BUILD_ENV}" ]] || { report_error "RELEASE" "001" "Missing ${BUILD_ENV}."; exit 1; }
cd "${WORKSPACE}"
# shellcheck source=/dev/null
source "${BUILD_ENV}"

BRANCH="${BRANCH#origin/}"
[[ "${BRANCH}" == "prod" ]] || { report_error "RELEASE" "002" "Release finalization is prod-only (got ${BRANCH})."; exit 1; }
[[ "${DO_PROD_DEPLOY:-false}" == "true" ]] || { report_error "RELEASE" "003" "DO_PROD_DEPLOY is not true."; exit 1; }
[[ -n "${BUILT_IMAGE_REF:-}" && -n "${BUILT_IMAGE_DIGEST:-}" ]] \
    || { report_error "RELEASE" "004" "Verified image ref/digest is missing."; exit 1; }
[[ "${BUILT_IMAGE_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || { report_error "RELEASE" "005" "Invalid image digest: ${BUILT_IMAGE_DIGEST}."; exit 1; }

RELEASE_VERSION="${BASE_VERSION:-${APP_VERSION}}"
export RELEASE_VERSION
GIT_TAG_NAME="$(resolve_git_tag prod "${BUILD_NUMBER}")"

# 在任何不可逆發布前先檢查 tag 是否與其他 commit 衝突。
existing_commit="$(remote_tag_commit "${GIT_TAG_NAME}")"
head_commit="$(git -C "${WORKSPACE}" rev-parse HEAD)"
if [[ -n "${existing_commit}" && "${existing_commit}" != "${head_commit}" ]]; then
    report_error "RELEASE" "006" "Immutable tag ${GIT_TAG_NAME} already belongs to ${existing_commit}."
    exit 1
fi

NEXUS_ARTIFACT_URL=""
if [[ "${DO_ARTIFACT_PUBLISH:-false}" == "true" ]]; then
    NEXUS_ARTIFACT_URL="$(nexus_upload_artifact "${APP_NAME}" "${BRANCH}" "${BASE_VERSION}" \
        "${BUILD_NUMBER}" "${ARTIFACT_LOCAL}")"
fi

if [[ "${DO_GIT_TAG:-false}" == "true" ]]; then
    push_git_tag "${GIT_TAG_NAME}"
fi

umask 077
cat > "${WORKSPACE}/.pipeline/release-manifest.env" <<EOF
RELEASE_TAG=${GIT_TAG_NAME}
GIT_COMMIT=${head_commit}
IMAGE_REF=${BUILT_IMAGE_REF}
IMAGE_DIGEST=${BUILT_IMAGE_DIGEST}
NEXUS_ARTIFACT_URL=${NEXUS_ARTIFACT_URL}
EOF
echo "[release] Finalized ${GIT_TAG_NAME}: ${BUILT_IMAGE_REF}@${BUILT_IMAGE_DIGEST}"
