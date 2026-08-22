#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "${TEST_ROOT}"' EXIT

REMOTE="${TEST_ROOT}/remote.git"
WORKSPACE="${TEST_ROOT}/workspace"
BIN="${TEST_ROOT}/bin"
mkdir -p "${BIN}"
git init --bare -q "${REMOTE}"
git init -q -b prod "${WORKSPACE}"
git -C "${WORKSPACE}" config user.name test
git -C "${WORKSPACE}" config user.email test@example.invalid
printf 'release\n' > "${WORKSPACE}/app.txt"
git -C "${WORKSPACE}" add app.txt
git -C "${WORKSPACE}" commit -q -m release
git -C "${WORKSPACE}" remote add origin "${REMOTE}"
git -C "${WORKSPACE}" push -q -u origin prod
# 模擬拋棄式 Jenkins agent：checkout 可存在，但沒有任何 committer identity。
git -C "${WORKSPACE}" config --unset user.name
git -C "${WORKSPACE}" config --unset user.email
export HOME="${TEST_ROOT}/empty-home"
mkdir -p "${HOME}"

mkdir -p "${WORKSPACE}/.pipeline"
ARTIFACT="${TEST_ROOT}/app-prod-1.2.3"
printf 'binary\n' > "${ARTIFACT}"
cat > "${WORKSPACE}/.pipeline/build.env" <<EOF
APP_NAME=app
APP_VERSION=1.2.3
BASE_VERSION=1.2.3
BRANCH=prod
BUILD_NUMBER=7
ARTIFACT_LOCAL=${ARTIFACT}
EOF

# Nexus upload 只驗證呼叫契約，不連外。
cat > "${BIN}/curl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${BIN}/curl"

export PATH="${BIN}:${PATH}"
export WORKSPACE GIT_COMMIT
export DO_PROD_DEPLOY=true DO_ARTIFACT_PUBLISH=true DO_GIT_TAG=true
export BUILT_IMAGE_REF='harbor.invalid/shiba/app/prod/1.2.3:7'
BUILT_IMAGE_DIGEST="sha256:$(printf 'a%.0s' {1..64})"
export BUILT_IMAGE_DIGEST
export NEXUS_CRED_USR=test NEXUS_CRED_PSW=test
export GITHUB_CREDENTIALS_USR=test GITHUB_CREDENTIALS_PSW=test
GIT_COMMIT="$(git -C "${WORKSPACE}" rev-parse HEAD)"

bash "${SCRIPT_DIR}/release-finalize.sh"
test "$(git --git-dir="${REMOTE}" rev-list -n 1 v1.2.3)" = "${GIT_COMMIT}"
test "$(git --git-dir="${REMOTE}" for-each-ref --format='%(taggername)' refs/tags/v1.2.3)" = 'Jenkins Release'
grep -q '^RELEASE_TAG=v1.2.3$' "${WORKSPACE}/.pipeline/release-manifest.env"
grep -q "^IMAGE_DIGEST=${BUILT_IMAGE_DIGEST}$" "${WORKSPACE}/.pipeline/release-manifest.env"

# 同版本若已屬於其他 commit，必須 fail closed 且不可移動 tag。
printf 'next\n' >> "${WORKSPACE}/app.txt"
git -C "${WORKSPACE}" -c user.name=test -c user.email=test@example.invalid commit -q -am next
if bash "${SCRIPT_DIR}/release-finalize.sh" >"${TEST_ROOT}/conflict.log" 2>&1; then
    echo 'FAIL: conflicting immutable tag was accepted' >&2
    exit 1
fi
test "$(git --git-dir="${REMOTE}" rev-list -n 1 v1.2.3)" = "${GIT_COMMIT}"
grep -q 'already belongs to' "${TEST_ROOT}/conflict.log"

echo 'release-finalize contract: PASS'
