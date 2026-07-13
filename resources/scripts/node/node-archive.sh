#!/usr/bin/env bash
# node/node-archive.sh — Node.js 版本管理（zip）
# 流程：讀取 package.json 取得 appName/appVersion/nodeVersion
#       → 打包 zip（排除 node_modules/.git/logs 等）
#       → archive_artifact → 寫 build.env → 打 Git Tag

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"
source "${SCRIPT_DIR}/common/archive-base.sh"
source "${SCRIPT_DIR}/common/git-tag.sh"
source "${SCRIPT_DIR}/common/version.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
BUILD_TOOL="${BUILD_TOOL:-npm}"
BRANCH="${GIT_BRANCH:-unknown}"
BRANCH="${BRANCH#origin/}"
BUILD_NUMBER="${BUILD_NUMBER:?BUILD_NUMBER is required}"

cd "${WORKSPACE}"

# ── 從 package.json 讀取應用資訊 ─────────────────────────────────────────────
# 使用 python3 確保在 nvm 切換前也能可靠解析
# 分三次呼叫避免 IFS read 只讀單行的問題（`read` 每次只讀到第一個換行符）
APP_NAME="$(python3 -c "import json; print(json.load(open('package.json')).get('name','unknown-app'))")"
# 統一版本契約：VERSION 檔（若存在）覆蓋 package.json version；否則沿用 package.json（行為不變）
NATIVE_VERSION="$(python3 -c "import json; print(json.load(open('package.json')).get('version','0.0.1'))")"
APP_VERSION="$(resolve_app_version node "${NATIVE_VERSION}")"
NODE_VERSION="$(python3 -c "
import json, re
pkg = json.load(open('package.json'))
raw = pkg.get('engines', {}).get('node', '')
m = re.search(r'\d+', raw)
print(m.group() if m else '20')
")"

# 去除版本號中可能含有的 -SNAPSHOT / -RC 後綴（保持與 Java 命名規則一致）
BASE_VERSION="${APP_VERSION%-SNAPSHOT}"
BASE_VERSION="${BASE_VERSION%-RC}"

export APP_NAME APP_VERSION NODE_VERSION

echo "[node-archive] appName:     ${APP_NAME}"
echo "[node-archive] appVersion:  ${APP_VERSION} (base: ${BASE_VERSION})"
echo "[node-archive] nodeVersion: ${NODE_VERSION}"
echo "[node-archive] branch:      ${BRANCH}"
echo "[node-archive] buildNumber: ${BUILD_NUMBER}"

# ── 產出物命名規則 ─────────────────────────────────────────────────────────────
# develop: {appName}-dev-{baseVersion}-SNAPSHOT-{buildNumber}.zip
# main:    {appName}-main-{baseVersion}-RC-{buildNumber}.zip
# prod:    {appName}-prod-{baseVersion}.zip
# 其他:    {appName}-{branch}-{baseVersion}-{buildNumber}.zip
resolve_artifact_name() {
    local safe_branch
    safe_branch="$(echo "${BRANCH}" | tr '/' '-' | tr '_' '-')"
    case "${BRANCH}" in
        develop) echo "${APP_NAME}-dev-${BASE_VERSION}-SNAPSHOT-${BUILD_NUMBER}.zip" ;;
        main)    echo "${APP_NAME}-main-${BASE_VERSION}-RC-${BUILD_NUMBER}.zip" ;;
        prod)    echo "${APP_NAME}-prod-${BASE_VERSION}.zip" ;;
        *)       echo "${APP_NAME}-${safe_branch}-${BASE_VERSION}-${BUILD_NUMBER}.zip" ;;
    esac
}

ARTIFACT_NAME="$(resolve_artifact_name)"
ARTIFACT_PATH="/tmp/${ARTIFACT_NAME}"
echo "[node-archive] artifact: ${ARTIFACT_NAME}"

# ── 打包 zip ──────────────────────────────────────────────────────────────────
# 排除清單：
#   node_modules/  → Docker build 時重新執行 npm ci，不需打包進去
#   .git/          → 版控資料，不需打包
#   .pipeline/     → CI 暫存資料
#   logs/          → 執行期 log，不屬於部署產出物
#   *.DS_Store     → macOS 系統檔
echo "[node-archive] Creating zip..."
# zip exclude 需同時帶前綴與不帶前綴兩種 pattern：
#   帶前綴（*/.../*）：匹配子目錄層的路徑
#   不帶前綴（.../*）：匹配 archive root 層的路徑（zip 路徑不含前導 ./）
zip -r "${ARTIFACT_PATH}" . \
    --exclude "node_modules/*"   --exclude "*/node_modules/*" \
    --exclude ".git/*"           --exclude "*/.git/*" \
    --exclude ".pipeline/*"      --exclude "*/.pipeline/*" \
    --exclude "logs/*"           --exclude "*/logs/*" \
    --exclude "*.DS_Store"

echo "[node-archive] zip created: ${ARTIFACT_PATH}"

# ── 存入 release/backup ───────────────────────────────────────────────────────
archive_artifact "${APP_NAME}" "${ARTIFACT_PATH}"
rm -f "${ARTIFACT_PATH}"

# ── 寫入 build.env（供後續 Docker Build stage 讀取）─────────────────────────
mkdir -p "${WORKSPACE}/.pipeline"
cat > "${WORKSPACE}/.pipeline/build.env" <<EOF
APP_NAME=${APP_NAME}
APP_VERSION=${APP_VERSION}
BASE_VERSION=${BASE_VERSION}
NODE_VERSION=${NODE_VERSION}
BRANCH=${BRANCH}
BUILD_NUMBER=${BUILD_NUMBER}
ARTIFACT_NAME=${ARTIFACT_NAME}
EOF
echo "[node-archive] build.env written."

# ── Git Tag ───────────────────────────────────────────────────────────────────
GIT_TAG_NAME="$(resolve_git_tag "${BRANCH}" "${BUILD_NUMBER}")"
export GIT_TAG_NAME
echo "[node-archive] git tag: ${GIT_TAG_NAME}"
push_git_tag "${GIT_TAG_NAME}"

echo "[node-archive] Archive completed."
