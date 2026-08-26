#!/usr/bin/env bash
# Shared k3d verification pool.
# Each trusted pipeline build gets one isolated ci-dev-* or ci-prod-* namespace.
# The namespace is created on demand and deleted in the Pipeline finally block.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/error-handler.sh"
source "${SCRIPT_DIR}/docker.sh"

command="${1:-deploy}"
environment="${DEPLOY_ENV:-${DEPLOY_NAMESPACE:-}}"
namespace="${CI_VERIFY_NAMESPACE:-}"

validate_namespace() {
    [[ "${environment}" == "dev" || "${environment}" == "prod" ]] || return 1
    [[ "${namespace}" =~ ^ci-${environment}-[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]
}

require_pool_identity() {
    if ! validate_namespace; then
        report_error "K3D_POOL" "001" \
            "invalid verification namespace/environment: namespace=${namespace:-empty}, environment=${environment:-empty}"
        return 1
    fi
}

pool_expiry_timestamp() {
    if date -u -d '+2 hours' +%Y-%m-%dT%H:%M:%SZ >/dev/null 2>&1; then
        date -u -d '+2 hours' +%Y-%m-%dT%H:%M:%SZ
    else
        date -u -v+2H +%Y-%m-%dT%H:%M:%SZ
    fi
}

render_manifests() {
    local rendered="$1" f target tmp
    mkdir -p "${rendered}"
    find "${rendered}" -mindepth 1 -maxdepth 1 -type f -delete

    for f in "${WORKSPACE}/k8s/"*.yaml; do
        [[ -e "${f}" ]] || continue
        target="${rendered}/$(basename "${f}")"
        envsubst < "${f}" > "${target}"
    done
    # Overlay is selected by logical environment, not by the per-build namespace.
    if [[ -d "${WORKSPACE}/k8s/${environment}" ]]; then
        for f in "${WORKSPACE}/k8s/${environment}/"*.yaml; do
            [[ -e "${f}" ]] || continue
            target="${rendered}/$(basename "${f}")"
            envsubst < "${f}" > "${target}"
        done
    fi

    # CI verification never publishes a host port. This keeps old project manifests
    # compatible while eliminating cluster-global NodePort allocation and collisions.
    for f in "${rendered}/"*.yaml; do
        [[ -e "${f}" ]] || continue
        tmp="${f}.tmp"
        sed -E \
            's/^([[:space:]]*)type:[[:space:]]*NodePort[[:space:]]*$/\1type: ClusterIP/; /^[[:space:]]*nodePort:/d' \
            "${f}" > "${tmp}"
        mv "${tmp}" "${f}"
    done
}

referenced_secret_names() {
    local rendered="$1"
    python3 - "${rendered}" <<'PY'
import pathlib
import re
import sys

names = set()
for path in pathlib.Path(sys.argv[1]).glob("*.yaml"):
    pending_indent = None
    for line in path.read_text(encoding="utf-8").splitlines():
        inline = re.search(r"secretKeyRef:\s*\{\s*name:\s*([A-Za-z0-9._-]+)", line)
        if inline:
            names.add(inline.group(1))
            pending_indent = None
            continue
        if "secretKeyRef:" in line:
            pending_indent = len(line) - len(line.lstrip())
            continue
        if pending_indent is not None:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            match = re.match(r"name:\s*([A-Za-z0-9._-]+)", stripped)
            if match:
                names.add(match.group(1))
                pending_indent = None
            elif stripped and indent <= pending_indent:
                pending_indent = None
for name in sorted(names):
    print(name)
PY
}

clone_environment_secrets() {
    local rendered="$1" secret_name
    while IFS= read -r secret_name; do
        [[ -n "${secret_name}" ]] || continue
        if ! kubectl get secret "${secret_name}" -n "${environment}" >/dev/null 2>&1; then
            echo "[k3d-pool] referenced secret ${environment}/${secret_name} is absent; workload validation will determine whether it is optional"
            continue
        fi
        kubectl get secret "${secret_name}" -n "${environment}" -o json \
            | python3 -c 'import json,sys; d=json.load(sys.stdin); d["metadata"]={"name":d["metadata"]["name"]}; print(json.dumps(d))' \
            | kubectl apply -n "${namespace}" -f - >/dev/null
        echo "[k3d-pool] copied required secret ${environment}/${secret_name} -> ${namespace}/${secret_name}"
    done < <(referenced_secret_names "${rendered}")
}

apply_slot_limits() {
    kubectl apply -n "${namespace}" -f - >/dev/null <<'YAML'
apiVersion: v1
kind: LimitRange
metadata:
  name: ci-slot-defaults
spec:
  limits:
    - type: Container
      defaultRequest: {cpu: 100m, memory: 128Mi}
      default: {cpu: "1", memory: 1Gi}
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: ci-slot-budget
spec:
  hard:
    requests.cpu: "2"
    requests.memory: 2Gi
    limits.cpu: "4"
    limits.memory: 4Gi
    pods: "10"
YAML
}

create_pool_namespace() {
    local rendered="$1" registry="$2"
    if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
        echo "[k3d-pool] removing stale namespace with the same build identity: ${namespace}"
        kubectl delete namespace "${namespace}" --wait=true --timeout=150s
    fi
    kubectl create namespace "${namespace}"
    kubectl label namespace "${namespace}" \
        shiba.dev/ci-pool=true "shiba.dev/environment=${environment}" --overwrite >/dev/null
    kubectl annotate namespace "${namespace}" \
        "shiba.dev/owner=${JOB_NAME:-unknown}#${BUILD_NUMBER:-unknown}" \
        "shiba.dev/expires-at=$(pool_expiry_timestamp)" --overwrite >/dev/null

    apply_slot_limits
    clone_environment_secrets "${rendered}"

    : "${HARBOR_USER:?HARBOR_USER is required for k3d image pull}"
    : "${HARBOR_PASS:?HARBOR_PASS is required for k3d image pull}"
    kubectl create secret docker-registry ci-harbor-pull -n "${namespace}" \
        --docker-server="${registry}" --docker-username="${HARBOR_USER}" --docker-password="${HARBOR_PASS}" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl patch serviceaccount default -n "${namespace}" --type=merge \
        -p '{"imagePullSecrets":[{"name":"ci-harbor-pull"}]}' >/dev/null
}

verify_service_health() {
    local health_path="$1"
    if [[ -z "${health_path}" ]]; then
        echo "[k3d-pool] project has no SMOKE_HEALTH_PATH; rollout/readiness is the verification gate"
        return 0
    fi

    local service_port log_file port_forward_pid local_port="" ok=0
    service_port="$(kubectl get service "${APP_NAME}" -n "${namespace}" -o jsonpath='{.spec.ports[0].port}')"
    log_file="${WORKSPACE}/.pipeline/port-forward-${namespace}.log"
    : > "${log_file}"
    kubectl port-forward --address 127.0.0.1 -n "${namespace}" service/"${APP_NAME}" ":${service_port}" >"${log_file}" 2>&1 &
    port_forward_pid=$!
    for _ in $(seq 1 20); do
        local_port="$(sed -nE 's/.*127\.0\.0\.1:([0-9]+).*/\1/p' "${log_file}" | head -1)"
        [[ -n "${local_port}" ]] && break
        kill -0 "${port_forward_pid}" 2>/dev/null || break
        sleep 1
    done
    if [[ -z "${local_port}" ]]; then
        kill "${port_forward_pid}" 2>/dev/null || true
        wait "${port_forward_pid}" 2>/dev/null || true
        report_error "K3D_POOL" "005" "port-forward did not expose ${namespace}/${APP_NAME}"
        cat "${log_file}" >&2
        return 1
    fi

    local probe_url="http://127.0.0.1:${local_port}${health_path}"
    echo "[k3d-pool] verifying Service -> Pod through ephemeral port-forward: ${probe_url}"
    for _ in $(seq 1 10); do
        if curl -sf -m 5 -o /dev/null "${probe_url}"; then ok=1; break; fi
        sleep 3
    done
    kill "${port_forward_pid}" 2>/dev/null || true
    wait "${port_forward_pid}" 2>/dev/null || true
    [[ "${ok}" == "1" ]] || {
        report_error "K3D_POOL" "006" "Service health check failed for ${namespace}/${APP_NAME}"
        return 1
    }
    echo "[k3d-pool] ✅ Service health check passed"
}

deploy() {
    require_pool_identity
    : "${APP_NAME:?APP_NAME must be exported from cd.sh build.env context}"
    : "${APP_VERSION:?APP_VERSION must be exported from cd.sh build.env context}"
    : "${BUILD_NUMBER:?BUILD_NUMBER must be exported from cd.sh build.env context}"
    : "${BRANCH:?BRANCH must be exported from cd.sh build.env context}"
    [[ -d "${WORKSPACE}/k8s" ]] || {
        report_error "K3D_POOL" "002" "k8s/ directory not found; project must provide verification manifests"
        return 1
    }

    local registry="${HARBOR_K3S_REGISTRY:-host.docker.internal:9290}"
    local rendered="${WORKSPACE}/.pipeline/k8s-rendered"
    export APP_NAME HARBOR_IMAGE NAMESPACE DEPLOY_ENV DEPLOY_TIMESTAMP GIT_COMMIT
    HARBOR_IMAGE="$(harbor_image_ref "${registry}" "${APP_NAME}" "${BRANCH}" "${APP_VERSION}" "${BUILD_NUMBER}")"
    NAMESPACE="${namespace}"
    DEPLOY_ENV="${environment}"
    DEPLOY_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    render_manifests "${rendered}"
    create_pool_namespace "${rendered}" "${registry}"
    echo "[k3d-pool] acquired slot ${namespace} (environment=${environment})"
    echo "[k3d-pool] image ${HARBOR_IMAGE}"

    kubectl apply -f "${rendered}/" -n "${namespace}" \
        || { report_error "K3D_POOL" "003" "kubectl apply failed for ${namespace}"; return 1; }
    kubectl rollout status deployment/"${APP_NAME}" -n "${namespace}" --timeout=180s \
        || {
            report_error "K3D_POOL" "004" "rollout timeout for ${namespace}/${APP_NAME}"
            kubectl get pods,service -n "${namespace}" -l "app=${APP_NAME}" -o wide >&2 || true
            kubectl logs -n "${namespace}" -l "app=${APP_NAME}" --all-containers --tail=100 >&2 || true
            return 1
        }

    local health_path=""
    if [[ -f "${WORKSPACE}/smoke-test.env" ]]; then
        health_path="$(grep -E '^SMOKE_HEALTH_PATH=' "${WORKSPACE}/smoke-test.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'' || true)"
    fi
    verify_service_health "${health_path}"
    echo "[k3d-pool] verification complete; Pipeline finally will release ${namespace}"
}

cleanup() {
    require_pool_identity
    echo "[k3d-pool] releasing slot ${namespace}"
    kubectl delete namespace "${namespace}" --ignore-not-found --wait=false
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    case "${command}" in
        deploy)  deploy ;;
        cleanup) cleanup ;;
        *) echo "usage: k3d-verify.sh deploy|cleanup" >&2; exit 2 ;;
    esac
fi
