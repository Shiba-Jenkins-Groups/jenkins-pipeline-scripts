#!/usr/bin/env bash
# Raw evidence must never inherit the filtered gate's .trivyignore.
set -euo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace="$(mktemp -d)"
trap 'rm -rf "${workspace}"' EXIT
mkdir -p "${workspace}/.pipeline" "${workspace}/fakebin"

cat >"${workspace}/.pipeline/build.env" <<'EOF'
APP_NAME=trivy-contract-test
APP_VERSION=0.0.0
BUILD_NUMBER=1
LANGUAGE=go
BRANCH=develop
EOF
touch "${workspace}/.trivyignore"

cat >"${workspace}/fakebin/trivy" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${TRIVY_TEST_LOG}"
output=""
format=""
while (($#)); do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    --format) format="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "${output}" ]]
if [[ "${format}" == json ]]; then
  printf '{"Results":[]}\n' >"${output}"
else
  printf '<testsuites/>\n' >"${output}"
fi
EOF
chmod +x "${workspace}/fakebin/trivy"

export WORKSPACE="${workspace}"
export PATH="${workspace}/fakebin:${PATH}"
export TRIVY_TEST_LOG="${workspace}/trivy.log"
export TRIVY_RAW_REPORT_ENABLED=true
export DO_DOCKER_BUILD=true DO_SCAN=true SCAN_EXIT_CODE=0

bash "${SCRIPT_ROOT}/cd.sh" image-scan >/dev/null

[[ -s "${workspace}/trivy-results-raw.json" ]]
[[ -s "${workspace}/trivy-results.xml" ]]
[[ "$(wc -l <"${TRIVY_TEST_LOG}" | tr -d ' ')" -eq 2 ]]
raw_call="$(sed -n '1p' "${TRIVY_TEST_LOG}")"
filtered_call="$(sed -n '2p' "${TRIVY_TEST_LOG}")"
[[ "${raw_call}" == *'--format json'* ]]
[[ "${raw_call}" != *'--ignorefile'* ]]
[[ "${filtered_call}" == *'--ignorefile'* ]]

echo '✅ Trivy raw evidence／filtered gate 分流契約通過'
