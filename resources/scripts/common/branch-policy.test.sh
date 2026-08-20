#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/branch-policy.sh"
fail=0

assert_flag() {
    local branch="$1" key="$2" want="$3"
    derive_branch_policy "${branch}"
    local got="${!key}"
    if [[ "${got}" == "${want}" ]]; then
        printf 'PASS: %s → %s=%s\n' "${branch}" "${key}" "${got}"
    else
        printf 'FAIL: %s → %s=%s, want %s\n' "${branch}" "${key}" "${got}" "${want}"
        fail=$((fail + 1))
    fi
}

unset CHANGE_ID CHANGE_BRANCH CHANGE_TARGET
export PROJECT_MAIN_ENABLED=false
for key in DO_ARTIFACT_PUBLISH DO_GIT_TAG DO_IMAGE_BUILD DO_IMAGE_PUSH DO_K3S_VERIFY DO_RUNTIME_DEPLOY DO_PROD_DEPLOY; do
    assert_flag feature/x "${key}" false
    assert_flag hotfix/x "${key}" false
    assert_flag main "${key}" false
done
assert_flag hotfix/x TEST_LEVEL coverage
assert_flag hotfix/x DEP_SCAN_CVSS 7
assert_flag main DO_IMAGE_PUSH false

export PROJECT_MAIN_ENABLED=true
assert_flag main DO_IMAGE_PUSH true
assert_flag main DO_RUNTIME_DEPLOY false
export PROJECT_MAIN_ENABLED=false

assert_flag develop DO_PACKAGE true
assert_flag develop DO_GIT_TAG true
assert_flag develop DO_IMAGE_PUSH true
assert_flag develop DO_K3S_VERIFY true
assert_flag develop DO_RUNTIME_DEPLOY false
assert_flag develop DO_PROD_DEPLOY false
assert_flag develop PIPELINE_TRUST trusted

assert_flag prod TEST_LEVEL coverage
assert_flag prod SCAN_EXIT_CODE 1
assert_flag prod GO_VULN_EXIT_CODE 1
assert_flag prod DO_GIT_TAG true
assert_flag prod DO_RUNTIME_DEPLOY false
assert_flag prod DO_PROD_DEPLOY true
assert_flag prod DEPLOY_NAMESPACE prod

for target in develop prod unknown; do
    export CHANGE_ID=17 CHANGE_BRANCH=feature/pr CHANGE_TARGET="${target}"
    derive_branch_policy PR-17
    for key in DO_ARTIFACT_PUBLISH DO_GIT_TAG DO_IMAGE_BUILD DO_IMAGE_PUSH DO_K3S_VERIFY DO_RUNTIME_DEPLOY DO_PROD_DEPLOY; do
        if [[ "${!key}" != false ]]; then
            printf 'FAIL: PR target=%s leaked %s=%s\n' "${target}" "${key}" "${!key}"
            fail=$((fail + 1))
        fi
    done
done
export CHANGE_ID=18 CHANGE_BRANCH=hotfix/fix CHANGE_TARGET=prod
assert_flag PR-18 TEST_LEVEL coverage
assert_flag PR-18 DEP_SCAN_CVSS 7
unset CHANGE_ID CHANGE_BRANCH CHANGE_TARGET

for br in develop prod feature/x hotfix/x main; do
    assert_flag "${br}" DO_SECRET_SCAN true
    assert_flag "${br}" SECRET_SCAN_EXIT_CODE 1
done

derive_branch_policy prod
printed_keys="$(print_branch_policy | cut -d= -f1 | sort)"
expected_keys="$(printf '%s\n' \
    IS_PR POLICY_NAME PIPELINE_TRUST PIPELINE_EVENT \
    DO_SECRET_SCAN SECRET_SCAN_EXIT_CODE DO_DEP_SCAN DEP_SCAN_CVSS \
    DO_PACKAGE DO_ARTIFACT_PUBLISH DO_GIT_TAG DO_IMAGE_BUILD DO_IMAGE_SCAN DO_IMAGE_PUSH \
    DO_K3S_VERIFY DO_RUNTIME_DEPLOY DO_PROD_DEPLOY \
    DO_DOCKER_BUILD DO_SCAN SCAN_EXIT_CODE GO_VULN_EXIT_CODE DO_PUSH DO_DEPLOY \
    DEPLOY_NAMESPACE NODE_PORT DEPLOY_INPUT_GATE TEST_LEVEL | sort)"
if [[ "${printed_keys}" != "${expected_keys}" ]]; then
    echo 'FAIL: print key contract mismatch'
    fail=$((fail + 1))
fi

if [[ ${fail} -eq 0 ]]; then
    echo '✅ branch-policy 全數通過'
else
    printf '❌ branch-policy 有 %d 項失敗\n' "${fail}"
fi
exit "${fail}"
