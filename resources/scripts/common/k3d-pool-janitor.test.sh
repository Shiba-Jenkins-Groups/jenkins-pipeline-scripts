#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/k3d-pool-janitor.sh"

for name in ci-dev-api-12-abcdef0 ci-prod-worker-7-1234567; do
    is_pool_namespace "${name}" || { echo "FAIL: expected pool namespace ${name}" >&2; exit 1; }
done
for name in dev prod default ci-stage-api ci-dev-API ci-dev-x_1; do
    if is_pool_namespace "${name}"; then
        echo "FAIL: janitor accepted non-pool namespace ${name}" >&2
        exit 1
    fi
done

echo "PASS: K3D pool janitor namespace allowlist"
