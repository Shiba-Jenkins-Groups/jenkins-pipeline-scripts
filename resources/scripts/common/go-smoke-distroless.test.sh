#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${TMP}/bin" "${TMP}/workspace"

cat >"${TMP}/bin/docker" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  run) echo fake-container ;;
  inspect) echo healthy ;;
  rm) exit 0 ;;
  exec) echo "distroless smoke must not exec curl" >&2; exit 99 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "${TMP}/bin/docker"

PATH="${TMP}/bin:${PATH}" WORKSPACE="${TMP}/workspace" APP_NAME=app BUILD_NUMBER=1 \
  bash "${SCRIPT_DIR}/go/go-smoke-test.sh" example.invalid/app:test

echo '✅ Go smoke 使用 Docker HEALTHCHECK，Distroless 不需 curl'
