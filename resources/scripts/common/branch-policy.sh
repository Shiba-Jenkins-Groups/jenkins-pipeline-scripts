#!/usr/bin/env bash
# common/branch-policy.sh — branch／PR 政策單一真相表
#
# 下游 Groovy 與 shell 只能讀旗標，不得自行重做 branch case。Package、Artifact Publish、
# Git Tag、Image、k3s Verification 與 Mac Runtime Deployment 必須各自有旗標；PR/feature
# 的 Package 只整理 workspace binary，絕不能因舊稱 Archive 而暗中上傳 Nexus 或 push tag。
#
# derive_branch_policy <branch> [CHANGE_ID] [CHANGE_TARGET]
# 任何 PR 都在最後套用無副作用硬閘；target=prod 採 strict checks，但一樣不得發布或部署。

derive_branch_policy() {
    local branch="${1:?derive_branch_policy: branch is required}"
    local change_id="${2:-${CHANGE_ID:-}}"
    local change_target="${3:-${CHANGE_TARGET:-}}"
    if [[ -n "${change_id}" && -n "${CHANGE_BRANCH:-}" ]]; then
        branch="${CHANGE_BRANCH}"
    fi
    branch="${branch#origin/}"

    IS_PR=false
    POLICY_NAME=fast
    PIPELINE_EVENT=branch
    PIPELINE_TRUST=untrusted

    # 全部 lane 先給安全預設；各分支只能明確打開所需能力。
    DO_SECRET_SCAN=true; SECRET_SCAN_EXIT_CODE=1
    DO_DEP_SCAN=false; DEP_SCAN_CVSS=11
    DO_PACKAGE=true; DO_ARTIFACT_PUBLISH=false; DO_GIT_TAG=false
    DO_IMAGE_BUILD=false; DO_IMAGE_SCAN=false; SCAN_EXIT_CODE=0; GO_VULN_EXIT_CODE=0
    DO_IMAGE_PUSH=false; DO_K3S_VERIFY=false; DEPLOY_NAMESPACE=""; NODE_PORT=""
    DO_RUNTIME_DEPLOY=false; DO_PROD_DEPLOY=false; DEPLOY_INPUT_GATE=false
    TEST_LEVEL=unit

    case "${branch}" in
        develop)
            POLICY_NAME=develop
            PIPELINE_TRUST=trusted
            # 日常整合掃描為 warn-only；弱點訊號提早出現，不把修依賴留到發版日。
            DO_DEP_SCAN=true; DEP_SCAN_CVSS=11
            DO_ARTIFACT_PUBLISH=true; DO_GIT_TAG=true
            DO_IMAGE_BUILD=true; DO_IMAGE_SCAN=true; SCAN_EXIT_CODE=0; GO_VULN_EXIT_CODE=0
            DO_IMAGE_PUSH=true; DO_K3S_VERIFY=true; DEPLOY_NAMESPACE=dev
            TEST_LEVEL=unit
            ;;
        main)
            # Shared Library 為多專案共用；main 的既有交付行為保留。shiba 由 Multibranch
            # discovery 排除 main，不在這一層破壞另外兩個專案。
            POLICY_NAME=main
            PIPELINE_TRUST=trusted
            DO_DEP_SCAN=true; DEP_SCAN_CVSS=11
            DO_ARTIFACT_PUBLISH=true; DO_GIT_TAG=true
            DO_IMAGE_BUILD=true; DO_IMAGE_SCAN=true; SCAN_EXIT_CODE=0; GO_VULN_EXIT_CODE=0
            DO_IMAGE_PUSH=true
            TEST_LEVEL=coverage
            ;;
        prod)
            POLICY_NAME=prod
            PIPELINE_TRUST=trusted
            DO_DEP_SCAN=true; DEP_SCAN_CVSS=7
            DO_ARTIFACT_PUBLISH=true; DO_GIT_TAG=true
            DO_IMAGE_BUILD=true; DO_IMAGE_SCAN=true; SCAN_EXIT_CODE=1; GO_VULN_EXIT_CODE=1
            DO_IMAGE_PUSH=true; DO_K3S_VERIFY=true; DEPLOY_NAMESPACE=prod
            DO_PROD_DEPLOY=true; DEPLOY_INPUT_GATE=true
            TEST_LEVEL=coverage
            ;;
        hotfix/*)
            # shiba Multibranch 目前不 discovery feature/hotfix/PR；此 policy 僅供其他專案
            # 或未來中央可信 Jenkinsfile 使用，不能解讀為 shiba hotfix 會執行 Jenkins。
            POLICY_NAME=hotfix
            DO_DEP_SCAN=true; DEP_SCAN_CVSS=7
            GO_VULN_EXIT_CODE=1
            TEST_LEVEL=coverage
            ;;
        feature/*|*)
            # shiba 目前不 discovery 此 lane；安全預設保留給其他使用 Shared Library 的專案。
            ;;
    esac

    if [[ -n "${change_id}" ]]; then
        IS_PR=true
        PIPELINE_EVENT='pr'
        PIPELINE_TRUST=untrusted
        if [[ "${change_target}" == "prod" ]]; then
            POLICY_NAME=pr-prod-strict
            DO_DEP_SCAN=true; DEP_SCAN_CVSS=7
            GO_VULN_EXIT_CODE=1
            TEST_LEVEL=coverage
        else
            POLICY_NAME=pr-fast
            DO_DEP_SCAN=false; DEP_SCAN_CVSS=11
            GO_VULN_EXIT_CODE=0
            TEST_LEVEL=unit
        fi

        # PR 硬性不變量：source/target 名稱即使碰巧是 develop/prod，也不得交付。
        DO_ARTIFACT_PUBLISH=false; DO_GIT_TAG=false
        DO_IMAGE_BUILD=false; DO_IMAGE_SCAN=false; DO_IMAGE_PUSH=false
        DO_K3S_VERIFY=false; DEPLOY_NAMESPACE=""; NODE_PORT=""
        DO_RUNTIME_DEPLOY=false; DO_PROD_DEPLOY=false; DEPLOY_INPUT_GATE=false
    fi

    # Shared Library 仍服務其他專案；是否使用 main 由專案明確聲明。shiba 設 false，
    # 其他專案未設定時保留舊行為。
    if [[ "${branch}" == "main" && "${PROJECT_MAIN_ENABLED:-true}" != "true" ]]; then
        POLICY_NAME=main-disabled
        DO_ARTIFACT_PUBLISH=false; DO_GIT_TAG=false
        DO_IMAGE_BUILD=false; DO_IMAGE_SCAN=false; DO_IMAGE_PUSH=false
        DO_K3S_VERIFY=false; DEPLOY_NAMESPACE=""; NODE_PORT=""
        DO_RUNTIME_DEPLOY=false; DO_PROD_DEPLOY=false; DEPLOY_INPUT_GATE=false
    fi

    # 舊旗標保留為相容 alias，讓未同步升級的下游腳本與其他專案原功能不變。
    DO_DOCKER_BUILD="${DO_IMAGE_BUILD}"
    DO_SCAN="${DO_IMAGE_SCAN}"
    DO_PUSH="${DO_IMAGE_PUSH}"
    DO_DEPLOY="${DO_K3S_VERIFY}"

    export IS_PR POLICY_NAME PIPELINE_EVENT PIPELINE_TRUST
    export DO_SECRET_SCAN SECRET_SCAN_EXIT_CODE
    export DO_DEP_SCAN DEP_SCAN_CVSS
    export DO_PACKAGE DO_ARTIFACT_PUBLISH DO_GIT_TAG
    export DO_IMAGE_BUILD DO_IMAGE_SCAN DO_IMAGE_PUSH DO_K3S_VERIFY
    export DO_RUNTIME_DEPLOY DO_PROD_DEPLOY
    export DO_DOCKER_BUILD DO_SCAN SCAN_EXIT_CODE GO_VULN_EXIT_CODE DO_PUSH DO_DEPLOY
    export DEPLOY_NAMESPACE NODE_PORT DEPLOY_INPUT_GATE TEST_LEVEL
}

print_branch_policy() {
    # 只印 KEY=VALUE（stdout 契約，供 Groovy 解析）；診斷訊息一律走 stderr。
    echo "IS_PR=${IS_PR}"
    echo "POLICY_NAME=${POLICY_NAME}"
    echo "PIPELINE_EVENT=${PIPELINE_EVENT}"
    echo "PIPELINE_TRUST=${PIPELINE_TRUST}"
    echo "DO_SECRET_SCAN=${DO_SECRET_SCAN}"
    echo "SECRET_SCAN_EXIT_CODE=${SECRET_SCAN_EXIT_CODE}"
    echo "DO_DEP_SCAN=${DO_DEP_SCAN}"
    echo "DEP_SCAN_CVSS=${DEP_SCAN_CVSS}"
    echo "DO_PACKAGE=${DO_PACKAGE}"
    echo "DO_ARTIFACT_PUBLISH=${DO_ARTIFACT_PUBLISH}"
    echo "DO_GIT_TAG=${DO_GIT_TAG}"
    echo "DO_IMAGE_BUILD=${DO_IMAGE_BUILD}"
    echo "DO_IMAGE_SCAN=${DO_IMAGE_SCAN}"
    echo "DO_IMAGE_PUSH=${DO_IMAGE_PUSH}"
    echo "DO_K3S_VERIFY=${DO_K3S_VERIFY}"
    echo "DO_RUNTIME_DEPLOY=${DO_RUNTIME_DEPLOY}"
    echo "DO_PROD_DEPLOY=${DO_PROD_DEPLOY}"
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

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    set -euo pipefail
    # PR 以 CHANGE_BRANCH 為 source；一般 branch 依序使用 GIT_BRANCH／BRANCH_NAME／BRANCH。
    RESOLVED_BRANCH="${CHANGE_BRANCH:-${GIT_BRANCH:-${BRANCH_NAME:-${BRANCH:-}}}}"
    if [[ -z "${RESOLVED_BRANCH}" ]]; then
        echo "[branch-policy] [ERROR] Cannot resolve branch from CHANGE_BRANCH/GIT_BRANCH/BRANCH_NAME/BRANCH." >&2
        exit 1
    fi
    derive_branch_policy "${RESOLVED_BRANCH}" "${CHANGE_ID:-}" "${CHANGE_TARGET:-}"
    echo "[branch-policy] branch=${RESOLVED_BRANCH#origin/} target=${CHANGE_TARGET:-none} policy=${POLICY_NAME}." >&2
    print_branch_policy
fi
