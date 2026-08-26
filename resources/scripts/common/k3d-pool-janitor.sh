#!/usr/bin/env bash
# Deletes only expired namespaces reserved by the shared CI verification pool.
set -euo pipefail

dry_run="${K3D_POOL_JANITOR_DRY_RUN:-false}"
default_ttl_seconds="${K3D_POOL_NAMESPACE_TTL_SECONDS:-7200}"
now_epoch="$(date -u +%s)"

is_pool_namespace() {
    [[ "$1" =~ ^ci-(dev|prod)-[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]
}

expiry_epoch() {
    local namespace="$1" expires created created_epoch
    expires="$(kubectl get namespace "${namespace}" -o jsonpath="{.metadata.annotations['shiba\.dev/expires-at']}" 2>/dev/null || true)"
    if [[ -n "${expires}" ]]; then
        date -u -d "${expires}" +%s 2>/dev/null && return 0
    fi
    created="$(kubectl get namespace "${namespace}" -o jsonpath='{.metadata.creationTimestamp}')"
    created_epoch="$(date -u -d "${created}" +%s)"
    printf '%s\n' "$((created_epoch + default_ttl_seconds))"
}

main() {
    local deleted=0 kept=0 names namespace expires_epoch
    names="$(kubectl get namespaces -o jsonpath='{.items[*].metadata.name}')"
    for namespace in ${names}; do
        is_pool_namespace "${namespace}" || continue
        expires_epoch="$(expiry_epoch "${namespace}")" || {
            echo "[k3d-pool-janitor] WARN: cannot parse expiry for ${namespace}; keeping it"
            kept=$((kept + 1))
            continue
        }
        if (( now_epoch < expires_epoch )); then
            echo "[k3d-pool-janitor] keep ${namespace} until $(date -u -d "@${expires_epoch}" +%Y-%m-%dT%H:%M:%SZ)"
            kept=$((kept + 1))
            continue
        fi
        if [[ "${dry_run}" == "true" ]]; then
            echo "[k3d-pool-janitor] dry-run: would delete expired namespace ${namespace}"
        else
            echo "[k3d-pool-janitor] deleting expired namespace ${namespace}"
            kubectl delete namespace "${namespace}" --wait=false
        fi
        deleted=$((deleted + 1))
    done
    echo "[k3d-pool-janitor] complete: deleted=${deleted}, kept=${kept}, dry_run=${dry_run}"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main
fi
