#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${TMP}/ws/.pipeline" "${TMP}/bin"
touch "${TMP}/ws/Dockerfile-worker"
cat >"${TMP}/ws/.pipeline/build.env" <<'EOF'
APP_NAME=demo-app
APP_VERSION=1.2.3
BUILD_NUMBER=42
BRANCH=develop
EOF

cat >"${TMP}/bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >>"${FAKE_LOG}"
if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then exit 1; fi
exit 0
EOF
cat >"${TMP}/bin/trivy" <<'EOF'
#!/usr/bin/env bash
echo "trivy $*" >>"${FAKE_LOG}"
while (($#)); do
  if [[ "$1" == --output ]]; then shift; : >"$1"; break; fi
  shift
done
EOF
chmod +x "${TMP}/bin/docker" "${TMP}/bin/trivy"

export PATH="${TMP}/bin:${PATH}"
export WORKSPACE="${TMP}/ws"
export ADDITIONAL_IMAGES="worker=Dockerfile-worker"
export FAKE_LOG="${TMP}/calls.log"
export HARBOR_USER=test HARBOR_PASS=test HARBOR_REGISTRY=localhost:9290

bash "${SCRIPT_DIR}/additional-images.sh" build
bash "${SCRIPT_DIR}/additional-images.sh" scan
bash "${SCRIPT_DIR}/additional-images.sh" push

grep -q 'docker build .*Dockerfile-worker.*demo-app-worker:1.2.3-42' "${FAKE_LOG}"
grep -q 'trivy image .*demo-app-worker:1.2.3-42' "${FAKE_LOG}"
grep -q 'docker push localhost:9290/demo-app/worker/develop/1.2.3:42' "${FAKE_LOG}"
grep -q '^IMAGE_REF=localhost:9290/demo-app/worker/develop/1.2.3:42$' \
  "${WORKSPACE}/image-ref-worker.txt"

echo '✅ additional-images.sh build／scan／push 契約通過'
