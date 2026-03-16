#!/usr/bin/env bash
# node/node-test.sh — Node.js Test（依 branch 決定範圍）
# develop: Unit Test only
# main:    Unit Test + Coverage（TODO）+ Integration（TODO）
# prod:    以上全部 + Security（TODO）
# 其他:    Unit Test only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
BUILD_TOOL="${BUILD_TOOL:-npm}"
BRANCH="${GIT_BRANCH:-unknown}"
BRANCH="${BRANCH#origin/}"

echo "[node-test] Branch: ${BRANCH}"

# ── 重新初始化 nvm（各 script 以獨立 bash subprocess 執行，須自行 source）─────
# 同 node-build.sh 的版本讀取邏輯，確保測試環境與 build 環境一致
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

setup_nvm() {
    local version="${1}"
    local nvm_dir=""
    for candidate in "${NVM_DIR:-}" /opt/nvm /root/.nvm /home/jenkins/.nvm; do
        if [[ -s "${candidate}/nvm.sh" ]]; then
            nvm_dir="${candidate}"
            break
        fi
    done
    if [[ -z "${nvm_dir}" ]]; then
        echo "[node-test] [ERROR] nvm not found." >&2
        exit 1
    fi
    export NVM_DIR="${nvm_dir}"
    # shellcheck disable=SC1091
    source "${NVM_DIR}/nvm.sh"
    nvm use "${version}"
    echo "[node-test] Active: $(node --version)"
}

NODE_VERSION="$(get_node_version)"
setup_nvm "${NODE_VERSION}"

cd "${WORKSPACE}"

# ── 確認 package.json 內是否定義 test script ────────────────────────────────
HAS_TEST="$(python3 -c "import json; p=json.load(open('package.json')); print('yes' if p.get('scripts',{}).get('test') else 'no')")"

# ── 各測試階段定義 ────────────────────────────────────────────────────────────
run_unit_test() {
    if [[ "${HAS_TEST}" != "yes" ]]; then
        echo "[node-test] No test script in package.json, skipping unit test."
        return
    fi
    echo "[node-test] Running unit tests..."
    case "${BUILD_TOOL}" in
        npm)  npm test ;;
        yarn) yarn test ;;
    esac
}

run_coverage() {
    # 預留：nyc（Istanbul）或 c8 覆蓋率報告
    echo "[node-test] TODO: Coverage (nyc/c8) not yet implemented."
}

run_integration_test() {
    echo "[node-test] TODO: Integration tests not yet implemented."
}

run_security_scan() {
    # 預留：npm audit 或 Snyk
    echo "[node-test] TODO: Security scan (npm audit / Snyk) not yet implemented."
}

# ── 依 branch 決定執行範圍 ────────────────────────────────────────────────────
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

echo "[node-test] Test completed."
