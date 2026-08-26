#!/usr/bin/env bash
# Opt-in live test: KUBECONFIG must point at the shared local K3D cluster.
set -euo pipefail

: "${KUBECONFIG:?KUBECONFIG must point to a disposable/shared DEV K3D cluster}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test_root="$(mktemp -d)"
namespace="ci-dev-pool-smoke-$$-abcdef0"

# shellcheck disable=SC2329 # invoked through EXIT trap
cleanup() {
    CI_VERIFY_NAMESPACE="${namespace}" DEPLOY_ENV=dev DEPLOY_NAMESPACE=dev \
        WORKSPACE="${test_root}" APP_NAME=pool-smoke BRANCH=develop APP_VERSION=0 BUILD_NUMBER=0 \
        bash "${SCRIPT_DIR}/k3d-verify.sh" cleanup >/dev/null 2>&1 || true
    rm -rf "${test_root}"
}
trap cleanup EXIT
mkdir -p "${test_root}/k8s" "${test_root}/.pipeline"

cat > "${test_root}/k8s/deployment.yaml" <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${APP_NAME}
  namespace: ${NAMESPACE}
  labels: {app: ${APP_NAME}}
spec:
  replicas: 1
  selector:
    matchLabels: {app: ${APP_NAME}}
  template:
    metadata:
      labels: {app: ${APP_NAME}}
    spec:
      containers:
        - name: pause
          image: rancher/mirrored-pause:3.6
          imagePullPolicy: IfNotPresent
YAML

export WORKSPACE="${test_root}"
export CI_VERIFY_NAMESPACE="${namespace}"
export DEPLOY_ENV=dev DEPLOY_NAMESPACE=dev
export APP_NAME=pool-smoke BRANCH=develop APP_VERSION=0 BUILD_NUMBER=0 GIT_COMMIT=abcdef0
export JOB_NAME=k3d-pool-integration HARBOR_USER=dummy HARBOR_PASS=dummy

bash "${SCRIPT_DIR}/k3d-verify.sh" deploy
kubectl get namespace "${namespace}" >/dev/null
bash "${SCRIPT_DIR}/k3d-verify.sh" cleanup

for _ in $(seq 1 30); do
    kubectl get namespace "${namespace}" >/dev/null 2>&1 || {
        echo "PASS: live K3D pool slot acquired, verified and released"
        exit 0
    }
    sleep 1
done
echo "FAIL: namespace ${namespace} was not reclaimed" >&2
exit 1
