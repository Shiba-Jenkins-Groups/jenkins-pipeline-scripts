#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT
mkdir -p "${test_root}/k8s/prod" "${test_root}/.pipeline"

cat > "${test_root}/k8s/service.yaml" <<'YAML'
apiVersion: v1
kind: Service
metadata:
  name: ${APP_NAME}
  namespace: ${NAMESPACE}
spec:
  type: NodePort
  ports:
    - port: 8090
      targetPort: 8090
      nodePort: ${NODE_PORT}
YAML

cat > "${test_root}/k8s/deployment.yaml" <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${APP_NAME}
  namespace: ${NAMESPACE}
spec:
  template:
    spec:
      containers:
        - name: app
          env:
            - name: APP_ENV
              value: ${DEPLOY_ENV}
            - name: ONE
              valueFrom:
                secretKeyRef:
                  name: minio-creds
                  key: endpoint
            - name: TWO
              valueFrom:
                secretKeyRef: { name: recognition-creds, key: token }
YAML

export WORKSPACE="${test_root}"
export DEPLOY_ENV=dev
export DEPLOY_NAMESPACE=dev
export CI_VERIFY_NAMESPACE=ci-dev-example-42-abcdef0
export APP_NAME=example
export NAMESPACE="${CI_VERIFY_NAMESPACE}"
export NODE_PORT=30092
source "${SCRIPT_DIR}/k3d-verify.sh"

validate_namespace
CI_VERIFY_NAMESPACE=dev
# shellcheck disable=SC2034 # consumed by validate_namespace() sourced above
namespace=dev
if validate_namespace; then
    echo "FAIL: permanent dev namespace must never be accepted by pool cleanup" >&2
    exit 1
fi
CI_VERIFY_NAMESPACE=ci-dev-example-42-abcdef0

rendered="${test_root}/.pipeline/rendered"
render_manifests "${rendered}"
grep -q 'type: ClusterIP' "${rendered}/service.yaml"
if grep -q 'nodePort:' "${rendered}/service.yaml"; then
    echo "FAIL: rendered verification Service still contains nodePort" >&2
    exit 1
fi
grep -q 'namespace: ci-dev-example-42-abcdef0' "${rendered}/deployment.yaml"
grep -q 'value: dev' "${rendered}/deployment.yaml"

actual_secrets="$(referenced_secret_names "${rendered}")"
expected_secrets=$'minio-creds\nrecognition-creds'
[[ "${actual_secrets}" == "${expected_secrets}" ]] || {
    echo "FAIL: secret references differ" >&2
    printf 'expected:\n%s\nactual:\n%s\n' "${expected_secrets}" "${actual_secrets}" >&2
    exit 1
}

echo "PASS: shared K3D verification pool rendering and namespace guard"
