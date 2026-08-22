#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK_SCRIPT="${SCRIPT_DIR}/dependency-check.sh"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "${TEST_DIR}"' EXIT
fail=0

cat > "${TEST_DIR}/govulncheck-ok" <<'EOF'
#!/usr/bin/env bash
printf 'fake govulncheck args=%s\n' "$*"
exit 0
EOF
cat > "${TEST_DIR}/govulncheck-vulnerable" <<'EOF'
#!/usr/bin/env bash
echo 'fake reachable vulnerability'
exit 3
EOF
chmod +x "${TEST_DIR}/govulncheck-ok" "${TEST_DIR}/govulncheck-vulnerable"

assert_case() {
    local name="$1" expected="$2" scanner="$3" policy="$4" pattern="$5"
    local output status
    output="$(WORKSPACE="${TEST_DIR}" LANGUAGE=go BUILD_TOOL=go \
        GOVULNCHECK_BIN="${scanner}" GO_VULN_EXIT_CODE="${policy}" \
        bash "${CHECK_SCRIPT}" 2>&1)"
    status=$?
    if [[ "${status}" == "${expected}" && "${output}" == *"${pattern}"* ]]; then
        printf 'PASS: %s\n' "${name}"
    else
        printf 'FAIL: %s status=%s expected=%s output=%s\n' "${name}" "${status}" "${expected}" "${output}"
        fail=$((fail + 1))
    fi
}

assert_case go-clean 0 "${TEST_DIR}/govulncheck-ok" 0 '未發現可達弱點'
assert_case go-warn 0 "${TEST_DIR}/govulncheck-vulnerable" 0 '警告模式'
assert_case go-strict 1 "${TEST_DIR}/govulncheck-vulnerable" 1 '禁止繼續交付'
assert_case go-missing-warn 0 "${TEST_DIR}/missing-govulncheck" 0 '未執行'
assert_case go-missing-strict 1 "${TEST_DIR}/missing-govulncheck" 1 'govulncheck 不存在'

if [[ ${fail} -eq 0 ]]; then
    echo '✅ dependency-check Go 分派全數通過'
else
    printf '❌ dependency-check Go 分派有 %d 項失敗\n' "${fail}"
fi
exit "${fail}"
