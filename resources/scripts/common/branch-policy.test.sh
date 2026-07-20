#!/usr/bin/env bash
# common/branch-policy.test.sh — 政策表的迴歸測試
#
# ── 為何這支測試值得存在 ────────────────────────────────────────────────────────
# 政策表裡有一整類旗標「**只有 prod 分支會執行到**」（SCAN_EXIT_CODE=1／GO_VULN_EXIT_CODE=1／
# DEP_SCAN_CVSS=7／DEPLOY_INPUT_GATE／git-tag.sh 的 exact-match tag 要求）。develop 跑一百次
# 也碰不到它們——它們第一次被執行的時刻，就是你要發版的那一刻。歷史上正是這個結構咬過人：
# `--no-tags` 使 prod build 結構上不可能成功，而 develop 全綠了無數次都驗不到。
#
# 本檔把「prod 會拿到什麼旗標」變成**離線可驗**，不必真的發一次版。
# derive_branch_policy 是純函數（吃 branch 名、吐旗標），這使離線斷言成為可能。
#
# 用法：bash resources/scripts/common/branch-policy.test.sh
# 慣例同 k8s-ops/ttl-janitor/scan.test.sh（source 受測腳本 → assert → 回傳失敗數）。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=branch-policy.sh
source "${SCRIPT_DIR}/branch-policy.sh"   # 執行模式受 BASH_SOURCE guard 保護，source 不會觸發輸出

fail=0

# 斷言某 branch 推導後，某個旗標等於預期值
assert_flag() {
    local branch="$1" key="$2" want="$3"
    derive_branch_policy "${branch}"
    local got="${!key}"
    if [[ "${got}" == "${want}" ]]; then
        echo "PASS: ${branch} → ${key}=${got}"
    else
        echo "FAIL: ${branch} → ${key}=${got}，應為 ${want}"
        fail=$((fail + 1))
    fi
}

echo "── prod：只有發版當下才會執行到的旗標（本測試的主要價值）──────────────"
assert_flag prod SCAN_EXIT_CODE    1        # Trivy HIGH/CRITICAL 即擋
assert_flag prod GO_VULN_EXIT_CODE 1        # govulncheck 可達弱點即擋
assert_flag prod DEP_SCAN_CVSS     7        # 依賴 CVSS≥7 即擋
assert_flag prod DO_DEPLOY         true
assert_flag prod DEPLOY_NAMESPACE  prod
assert_flag prod TEST_LEVEL        coverage

echo "── develop／main：日常路徑（警告不阻斷，痛點不該落在發版日）────────────"
assert_flag develop SCAN_EXIT_CODE    0
assert_flag develop GO_VULN_EXIT_CODE 0
# 2026-07-20 起 develop 也跑依賴掃描：弱點訊號要在日常就出現，不是留到發版日才由 prod 硬閘
# 攔下來（那會把發版日變成修依賴日）。CVSS=11＝warn only，與本分支其他掃描一致不阻斷。
# 鎖進測試是因為它「開著也不會讓 build 變紅」⇒ 被誰順手關掉不會有任何人察覺。
assert_flag develop DO_DEP_SCAN       true
assert_flag develop DEP_SCAN_CVSS     11
assert_flag develop DO_DEPLOY         true
assert_flag develop DEPLOY_NAMESPACE  dev
assert_flag develop TEST_LEVEL        unit
assert_flag main    DO_DEPLOY         false
assert_flag main    DEPLOY_NAMESPACE  ""
assert_flag main    TEST_LEVEL        coverage

echo "── feature／PR：不得進入任何交付步驟（預設安全）────────────────────────"
assert_flag feature/anything DO_DOCKER_BUILD false
assert_flag feature/anything DO_PUSH         false
assert_flag feature/anything DO_DEPLOY       false

echo "── 資安不變量：秘密掃描全 branch 開啟且一律阻斷 ────────────────────────"
# 秘密洩漏處處 critical。若哪天有人為了讓某分支「快一點」而放寬，這裡要立刻紅。
for br in develop main prod feature/x; do
    assert_flag "${br}" DO_SECRET_SCAN        true
    assert_flag "${br}" SECRET_SCAN_EXIT_CODE 1
done

echo "── origin/ 前綴需被剝除（Jenkins 的 GIT_BRANCH 常帶前綴）──────────────"
assert_flag origin/prod    DO_DEPLOY        true
assert_flag origin/prod    DEPLOY_NAMESPACE prod
assert_flag origin/develop DEPLOY_NAMESPACE dev

echo "── 表內不變量（原本註解宣稱「測試涵蓋」，此處使其成真）────────────────"
# 註：NODE_PORT 已不由本表決定（改由各專案 Jenkinsfile 的 devNodePort/prodNodePort 提供），
# 故原不變量「DO_DEPLOY=true ⇒ NODE_PORT 非空」不再適用於這一層，不在此斷言。
for br in develop main prod feature/x; do
    derive_branch_policy "${br}"
    # push 的必須是本次 build 的 image
    if [[ "${DO_PUSH}" == "true" && "${DO_DOCKER_BUILD}" != "true" ]]; then
        echo "FAIL: ${br} 違反不變量 DO_PUSH=true ⇒ DO_DOCKER_BUILD=true"; fail=$((fail + 1))
    else
        echo "PASS: ${br} 不變量 DO_PUSH ⇒ DO_DOCKER_BUILD"
    fi
    # k3s 拉的是 pushed image
    if [[ "${DO_DEPLOY}" == "true" && "${DO_PUSH}" != "true" ]]; then
        echo "FAIL: ${br} 違反不變量 DO_DEPLOY=true ⇒ DO_PUSH=true"; fail=$((fail + 1))
    else
        echo "PASS: ${br} 不變量 DO_DEPLOY ⇒ DO_PUSH"
    fi
    # 要部署就必須知道部署去哪
    if [[ "${DO_DEPLOY}" == "true" && -z "${DEPLOY_NAMESPACE}" ]]; then
        echo "FAIL: ${br} 違反不變量 DO_DEPLOY=true ⇒ DEPLOY_NAMESPACE 非空"; fail=$((fail + 1))
    else
        echo "PASS: ${br} 不變量 DO_DEPLOY ⇒ DEPLOY_NAMESPACE 非空"
    fi
done

echo "── stdout 契約：derive 出的每個旗標都必須被 print 出來 ─────────────────"
# ciPipeline.groovy 靠解析 print_branch_policy 的 KEY=VALUE 注入 env。若有人在 derive 新增
# 旗標卻忘了在 print 補一行，groovy 端會拿到空值——**沒有任何錯誤訊息**，下游只會用空字串
# 走進錯誤分支（典型靜默失效）。故此處斷言兩者的 key 集合一致。
derive_branch_policy prod
printed_keys="$(print_branch_policy | cut -d= -f1 | sort)"
expected_keys="$(printf '%s\n' \
    DO_SECRET_SCAN SECRET_SCAN_EXIT_CODE DO_DEP_SCAN DEP_SCAN_CVSS \
    DO_DOCKER_BUILD DO_SCAN SCAN_EXIT_CODE GO_VULN_EXIT_CODE \
    DO_PUSH DO_DEPLOY DEPLOY_NAMESPACE NODE_PORT DEPLOY_INPUT_GATE TEST_LEVEL | sort)"
if [[ "${printed_keys}" == "${expected_keys}" ]]; then
    echo "PASS: print_branch_policy 的 key 集合與預期一致"
else
    echo "FAIL: print_branch_policy 的 key 集合不符——groovy 端會拿到空值且不報錯"
    echo "  只在 print：$(comm -23 <(echo "${printed_keys}") <(echo "${expected_keys}") | tr '\n' ' ')"
    echo "  只在預期：$(comm -13 <(echo "${printed_keys}") <(echo "${expected_keys}") | tr '\n' ' ')"
    fail=$((fail + 1))
fi

echo "────────────────────────────────────────────────────────────"
if [[ ${fail} -eq 0 ]]; then
    echo "✅ branch-policy 全數通過"
else
    echo "❌ branch-policy 有 ${fail} 項失敗"
fi
exit ${fail}
