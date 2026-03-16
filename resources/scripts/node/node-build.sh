#!/usr/bin/env bash
# node/node-build.sh — Node.js Build（npm / yarn）
# 流程：讀取 package.json engines.node → 切換 nvm 版本 → 安裝依賴 → 執行 build script（若存在）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
BUILD_TOOL="${BUILD_TOOL:-npm}"

# ── 從 package.json 讀取 Node 版本需求 ────────────────────────────────────────
# 用 python3 解析避免「先有 node 才能跑 node 解析」的雞蛋問題
# 支援格式："20"、">=18"、"18.x"、"~18.0.0"、"^20.0.0"
# 無 engines.node 時預設 LTS 20
get_node_version() {
    python3 - "${WORKSPACE}/package.json" <<'EOF'
import json, re, sys
try:
    with open(sys.argv[1]) as f:
        pkg = json.load(f)
    raw = pkg.get("engines", {}).get("node", "")
    m = re.search(r"\d+", raw)
    print(m.group() if m else "20")
except Exception:
    print("20")
EOF
}

# ── 初始化 nvm 並切換至指定版本 ───────────────────────────────────────────────
# NVM_DIR 優先讀環境變數，再依序 fallback 至常見安裝位置
setup_nvm() {
    local version="${1}"

    # 找到 nvm.sh 所在目錄
    local nvm_dir=""
    for candidate in "${NVM_DIR:-}" /opt/nvm /root/.nvm /home/jenkins/.nvm; do
        if [[ -s "${candidate}/nvm.sh" ]]; then
            nvm_dir="${candidate}"
            break
        fi
    done

    if [[ -z "${nvm_dir}" ]]; then
        echo "[node-build] [ERROR] nvm not found. Checked: \${NVM_DIR}, /opt/nvm, /root/.nvm, /home/jenkins/.nvm" >&2
        exit 1
    fi

    export NVM_DIR="${nvm_dir}"
    # shellcheck disable=SC1091
    source "${NVM_DIR}/nvm.sh"

    # 版本不存在時自動安裝（CI 環境 image 預裝常用版本，此為保底措施）
    if ! nvm ls "${version}" 2>/dev/null | grep -q "v${version}"; then
        echo "[node-build] Node ${version} not in image, installing..."
        nvm install "${version}"
    fi

    nvm use "${version}"
    echo "[node-build] Active: $(node --version) / npm: $(npm --version)"
}

# ── 主流程 ────────────────────────────────────────────────────────────────────
NODE_VERSION="$(get_node_version)"
echo "[node-build] Required Node version: ${NODE_VERSION}"
echo "[node-build] Build tool: ${BUILD_TOOL}"

setup_nvm "${NODE_VERSION}"

# 將版本號匯出供後續 node-test.sh / node-archive.sh 使用（同一 bash session 內有效）
export NODE_VERSION

cd "${WORKSPACE}"

# 安裝依賴
# npm ci：鎖定 package-lock.json 版本，比 npm install 更適合 CI 環境
echo "[node-build] Installing dependencies..."
case "${BUILD_TOOL}" in
    npm)
        if [[ -f "package-lock.json" ]]; then
            npm ci
        else
            echo "[node-build] WARNING: package-lock.json not found, falling back to npm install."
            npm install
        fi
        ;;
    yarn)
        yarn install --frozen-lockfile
        ;;
    *)
        echo "[node-build] [ERROR] Unsupported build tool: ${BUILD_TOOL}" >&2
        exit 1
        ;;
esac

# 執行 build script（Vue / React / Angular 等前端框架）
# 純後端 Node app 通常無此步驟，偵測到才執行
HAS_BUILD="$(python3 -c "import json; p=json.load(open('package.json')); print('yes' if p.get('scripts',{}).get('build') else 'no')")"
if [[ "${HAS_BUILD}" == "yes" ]]; then
    echo "[node-build] Running build script..."
    case "${BUILD_TOOL}" in
        npm)  npm run build ;;
        yarn) yarn build ;;
    esac
else
    echo "[node-build] No build script found, skipping."
fi

echo "[node-build] Build completed. Node: $(node --version)"
