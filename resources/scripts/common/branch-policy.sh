#!/usr/bin/env bash
# common/branch-policy.sh — branch 政策單一真相表
#
# 所有「哪個 branch 做什麼」的決策集中於此；下游（groovy when / cd.sh / {lang}-test.sh）
# 只讀旗標，不得自帶 branch case。改政策＝只改本檔。
#
# ── 政策矩陣 ──────────────────────────────────────────────────────────────────
# | 旗標              | develop | main     | prod     | 其他  |
# |-------------------|---------|----------|----------|-------|
# | DO_DEP_SCAN       | true    | true     | true     | false |
# | DEP_SCAN_CVSS     | 11(warn)| 11(warn) | 7(fail)  | 11    |
# | DO_DOCKER_BUILD   | true    | true     | true     | false |
# | DO_SCAN           | true    | true     | true     | false |
# | SCAN_EXIT_CODE    | 0(warn) | 0(warn)  | 1(fail)  | 0     |
# | GO_VULN_EXIT_CODE | 0(warn) | 0(warn)  | 1(fail)  | 0     |
# | DO_PUSH           | true    | true     | true     | false |
# | DO_DEPLOY         | true    | false    | true     | false |
# | DEPLOY_NAMESPACE  | dev     | —        | prod     | —     |
# | NODE_PORT         | ""(見③) | —        | ""(見③)  | —     |
# | DEPLOY_INPUT_GATE | false   | false    | true     | false |
# | TEST_LEVEL        | unit    | coverage | coverage | unit  |
#
# 表內不變量（測試涵蓋）：
#   DO_PUSH=true   ⇒ DO_DOCKER_BUILD=true（push 的是本次 build 的 image）
#   DO_DEPLOY=true ⇒ DO_PUSH=true（k3s 拉的是 pushed image）
#   DO_DEPLOY=true ⇒ DEPLOY_NAMESPACE / NODE_PORT 非空
#   ③ NODE_PORT 不再由本表決定：改由各專案 Jenkinsfile 的 devNodePort/prodNodePort
#     參數提供（避免所有專案撞用同一固定值），見 ciPipeline.groovy Detect stage
#
# 使用方式：
#   1. 執行模式（Detect stage）：印出 KEY=VALUE 供 ciPipeline.groovy 解析注入 env
#   2. source 模式（cd.sh all / {lang}-test.sh standalone fallback）：
#      source 本檔後呼叫 derive_branch_policy "<branch>"

derive_branch_policy() {
    local branch="${1:?derive_branch_policy: branch is required}"
    branch="${branch#origin/}"

    case "${branch}" in
        develop)
            DO_SECRET_SCAN=true; SECRET_SCAN_EXIT_CODE=1
            # 2026-07-20 起 develop 也跑依賴掃描（CVSS=11＝warn only，與本分支 Trivy／
            # govulncheck 一律 warn 的政策一致）。原本關閉的顧慮是速度，但 NVD DB 已快取於
            # m2（jenkins-agent-cache volume），實測僅 ~15 秒；且 dependency-check.sh 現在會把
            # 發現摘要印進 console，開著才有意義——弱點訊號要在 develop 就出現，不是留到發版日。
            DO_DEP_SCAN=true; DEP_SCAN_CVSS=11
            DO_DOCKER_BUILD=true;  DO_SCAN=true; SCAN_EXIT_CODE=0; GO_VULN_EXIT_CODE=0
            DO_PUSH=true;  DO_DEPLOY=true;  DEPLOY_NAMESPACE=dev;  NODE_PORT=""
            DEPLOY_INPUT_GATE=false
            TEST_LEVEL=unit
            ;;
        main)
            DO_SECRET_SCAN=true; SECRET_SCAN_EXIT_CODE=1
            DO_DEP_SCAN=true; DEP_SCAN_CVSS=11
            DO_DOCKER_BUILD=true;  DO_SCAN=true;  SCAN_EXIT_CODE=0; GO_VULN_EXIT_CODE=0
            DO_PUSH=true;  DO_DEPLOY=false; DEPLOY_NAMESPACE="";   NODE_PORT=""
            DEPLOY_INPUT_GATE=false
            TEST_LEVEL=coverage
            ;;
        prod)
            DO_SECRET_SCAN=true; SECRET_SCAN_EXIT_CODE=1
            DO_DEP_SCAN=true; DEP_SCAN_CVSS=7
            DO_DOCKER_BUILD=true;  DO_SCAN=true;  SCAN_EXIT_CODE=1; GO_VULN_EXIT_CODE=1
            DO_PUSH=true;  DO_DEPLOY=true;  DEPLOY_NAMESPACE=prod; NODE_PORT=""
            DEPLOY_INPUT_GATE=true
            TEST_LEVEL=coverage
            ;;
        *)
            # feature / PR branch：只做 CI（build + unit test + archive），不進任何交付步驟
            # 但秘密掃描仍全 branch 執行（秘密洩漏處處 critical，feature 也擋）
            DO_SECRET_SCAN=true; SECRET_SCAN_EXIT_CODE=1
            DO_DEP_SCAN=false; DEP_SCAN_CVSS=11
            DO_DOCKER_BUILD=false; DO_SCAN=false; SCAN_EXIT_CODE=0; GO_VULN_EXIT_CODE=0
            DO_PUSH=false; DO_DEPLOY=false; DEPLOY_NAMESPACE="";   NODE_PORT=""
            DEPLOY_INPUT_GATE=false
            TEST_LEVEL=unit
            ;;
    esac

    export DO_SECRET_SCAN SECRET_SCAN_EXIT_CODE
    export DO_DEP_SCAN DEP_SCAN_CVSS
    export DO_DOCKER_BUILD DO_SCAN SCAN_EXIT_CODE GO_VULN_EXIT_CODE DO_PUSH DO_DEPLOY
    export DEPLOY_NAMESPACE NODE_PORT DEPLOY_INPUT_GATE TEST_LEVEL
}

print_branch_policy() {
    # 只印 KEY=VALUE（stdout 契約，供 groovy 解析）；診斷訊息一律走 stderr
    echo "DO_SECRET_SCAN=${DO_SECRET_SCAN}"
    echo "SECRET_SCAN_EXIT_CODE=${SECRET_SCAN_EXIT_CODE}"
    echo "DO_DEP_SCAN=${DO_DEP_SCAN}"
    echo "DEP_SCAN_CVSS=${DEP_SCAN_CVSS}"
    echo "DO_DOCKER_BUILD=${DO_DOCKER_BUILD}"
    echo "DO_SCAN=${DO_SCAN}"
    echo "SCAN_EXIT_CODE=${SCAN_EXIT_CODE}"
    echo "GO_VULN_EXIT_CODE=${GO_VULN_EXIT_CODE}"
    echo "DO_PUSH=${DO_PUSH}"
    echo "DO_DEPLOY=${DO_DEPLOY}"
    echo "DEPLOY_NAMESPACE=${DEPLOY_NAMESPACE}"
    echo "NODE_PORT=${NODE_PORT}"
    echo "DEPLOY_INPUT_GATE=${DEPLOY_INPUT_GATE}"
    echo "TEST_LEVEL=${TEST_LEVEL}"
}

# ── 執行模式：從 Jenkins env 取 branch，推導後印出 KEY=VALUE ─────────────────
# branch 來源優先序：GIT_BRANCH（現行 Pipeline）> BRANCH_NAME（Multibranch 預留）> BRANCH
# 取不到 branch 時直接 fail——寧可早死，不以錯誤政策靜默續跑
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    set -euo pipefail
    RESOLVED_BRANCH="${GIT_BRANCH:-${BRANCH_NAME:-${BRANCH:-}}}"
    if [[ -z "${RESOLVED_BRANCH}" ]]; then
        echo "[branch-policy] [ERROR] Cannot resolve branch: GIT_BRANCH / BRANCH_NAME / BRANCH all empty." >&2
        exit 1
    fi
    derive_branch_policy "${RESOLVED_BRANCH}"
    echo "[branch-policy] branch=${RESOLVED_BRANCH#origin/} → policy derived." >&2
    print_branch_policy
fi
