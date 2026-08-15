#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
mkdir -p "${TMP}/cmd/app" "${TMP}/cmd/worker"
cat >"${TMP}/go.mod" <<'EOF'
module example.test/multi

go 1.22
EOF
cat >"${TMP}/cmd/app/main.go" <<'EOF'
package main
func main() {}
EOF
cp "${TMP}/cmd/app/main.go" "${TMP}/cmd/worker/main.go"
cat >"${TMP}/go-pipeline.env" <<'EOF'
GO_MAIN_PKG=./cmd/app
GO_ADDITIONAL_BINARIES=worker=./cmd/worker
EOF

WORKSPACE="${TMP}" bash "${SCRIPT_DIR}/go-build.sh"
[[ -x "${TMP}/.gobuild/app" ]]
[[ -x "${TMP}/.gobuild/worker" ]]
echo '✅ go-build app＋additional binary 契約通過'
