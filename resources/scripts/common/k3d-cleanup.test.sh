#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export DEPLOY_ENV=dev DEPLOY_NAMESPACE=dev CI_VERIFY_NAMESPACE=ci-dev-offline-1-abcdef0
export APP_NAME=offline-app BRANCH=develop APP_VERSION=1 BUILD_NUMBER=2
source "${SCRIPT_DIR}/k3d-verify.sh"

log="$(mktemp)"
trap 'rm -f "${log}"' EXIT
kubectl() {
    printf 'kubectl %s\n' "$*" >>"${log}"
    if [[ "$*" == 'get pods -A -o json' ]]; then
        printf '%s\n' '{"items":[]}'
    elif [[ "$*" == get\ nodes\ -o\ jsonpath=* ]]; then
        printf '%s\n' 'k3d-offline-server-0'
    fi
}
docker() {
    printf 'docker %s\n' "$*" >>"${log}"
    if [[ "$*" == exec\ k3d-offline-server-0\ crictl\ images* ]]; then
        printf '%s\n' 'sha256:offline'
    fi
}

cleanup

grep -Fq 'kubectl delete namespace ci-dev-offline-1-abcdef0 --ignore-not-found --wait=true --timeout=150s' "${log}"
grep -Fq 'docker exec k3d-offline-server-0 crictl rmi host.docker.internal:9290/offline-app/develop/1:2' "${log}"
if grep -Eq 'crictl (rmi --prune|image prune)|docker (system|builder) prune' "${log}"; then
    echo 'broad K3D/Docker prune unexpectedly used' >&2
    exit 1
fi
echo 'PASS: synchronous namespace release and exact-image-only K3D cache removal'
