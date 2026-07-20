#!/usr/bin/env bash
# common/pipefail-guard.test.sh — 「命令替換內的 grep 在 set -euo pipefail 下靜默殺腳本」迴歸測試。
#
# 背景（2026-07-20，claude-project/develop build #86）：
#   cd.sh 的 deploy 在 kubectl rollout 成功後，用
#       health_path="$(grep -E '^SMOKE_HEALTH_PATH=' smoke-test.env | tail -1 | cut … | tr …)"
#   取選用設定。claude-project 的 smoke-test.env **存在但沒有這個 key** ⇒ grep 回 1 ⇒
#   pipefail 讓整條 pipeline 回 1 ⇒ 賦值回 1 ⇒ set -e **靜默殺掉整個 deploy**：
#   log 停在「successfully rolled out」，沒有任何錯誤訊息，build 卻是 FAILURE。
#
#   最惡毒的地方在於：那段 code 本來就寫了「沒宣告就只用 rollout status」的 else 分支——
#   作者明白表示缺 key 是合法情形，pipefail 卻讓那條路**不可達**。這與同期發現的
#   prod_app.sh 回滾死碼是同一類缺陷：**寫好的求救／降級路徑永遠執行不到**。
#
# 本測試不驗特定腳本，而是把「這個 bash 陷阱本身」釘住：先證明未加保護的寫法確實會靜默死，
# 再證明現行各處的寫法已加上保護。新增類似取值邏輯時，照這裡的形狀寫。
#
# 用法：bash resources/scripts/common/pipefail-guard.test.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

pass() { echo "PASS: $*"; }
bad()  { echo "FAIL: $*"; fail=$((fail + 1)); }

# ── 1. 證明陷阱為真（沒有這條，下面的斷言只是裝飾）──────────────────────────
printf 'OTHER_KEY=1\n' > "${TMP}/nokey.env"

cat > "${TMP}/unguarded.sh" <<'EOF'
set -euo pipefail
v="$(grep -E '^WANTED=' "$1" | tail -1 | cut -d= -f2-)"
echo "REACHED:${v}"
EOF
if bash "${TMP}/unguarded.sh" "${TMP}/nokey.env" 2>/dev/null | grep -q REACHED; then
    bad "未加保護的寫法竟然存活——本測試的前提不成立，請重新確認 bash 行為"
else
    pass "已證實陷阱為真：未加保護時 key 不存在即靜默終止，後續程式碼不可達"
fi

# ── 2. 加了 `|| true` 之後必須存活並回空字串 ────────────────────────────────
cat > "${TMP}/guarded.sh" <<'EOF'
set -euo pipefail
v="$(grep -E '^WANTED=' "$1" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
echo "REACHED:[${v}]"
EOF
out="$(bash "${TMP}/guarded.sh" "${TMP}/nokey.env" 2>&1)"
if [[ "${out}" == "REACHED:[]" ]]; then
    pass "加上 || true 後：存活、回空字串，呼叫端得以走自己的降級分支"
else
    bad "加保護後行為不如預期：${out}"
fi

# 有 key 時仍要正確取值（保護不得改變正常路徑）
printf 'WANTED=hello\n' > "${TMP}/haskey.env"
out="$(bash "${TMP}/guarded.sh" "${TMP}/haskey.env" 2>&1)"
if [[ "${out}" == "REACHED:[hello]" ]]; then
    pass "有 key 時取值正確（保護未影響正常路徑）"
else
    bad "有 key 時取值錯誤：${out}"
fi

# ── 3. 現行 library 中所有同型寫法都必須已加保護 ────────────────────────────
# 掃「賦值 = 命令替換且內含 grep」的行；`|| true`／`|| echo`／`2>/dev/null …||` 皆算已保護。
echo "── 掃描 library 中的同型寫法 ──────────────────────────────────────"
while IFS= read -r hit; do
    [[ -z "${hit}" ]] && continue
    file="${hit%%:*}"
    rest="${hit#*:}"
    lineno="${rest%%:*}"
    code="${rest#*:}"
    # 測試檔本身不設 -e，不受此陷阱影響
    case "${file}" in *".test.sh") continue ;; esac
    if [[ "${code}" == *"|| true"* || "${code}" == *"|| echo"* ]]; then
        pass "已保護 ${file##*/}:${lineno}"
    else
        # 多行寫法：續行可能帶著保護，往下看兩行
        ctx="$(sed -n "${lineno},$((lineno + 2))p" "${file}")"
        if [[ "${ctx}" == *"|| true"* || "${ctx}" == *"|| echo"* ]]; then
            pass "已保護 ${file##*/}:${lineno}（保護在續行）"
        else
            bad "未保護的同型寫法 ${file}:${lineno}
        ${code}
        → 在 set -euo pipefail 下，grep 找不到就會靜默終止腳本。請於命令替換結尾加 || true。"
        fi
    fi
done < <(grep -rnE '=\$?\"?\$\(grep' "${LIB_ROOT}" --include="*.sh" 2>/dev/null || true)

echo "────────────────────────────────────────────────────────────"
if [[ ${fail} -eq 0 ]]; then
    echo "✅ pipefail 陷阱防護全數通過"
else
    echo "❌ 有 ${fail} 項失敗"
fi
exit ${fail}
