#!/usr/bin/env bash
# Trusted coordinator checks of an unchanged, finalized App source tree.
# This produces test artifacts only; no image build, push, tag or publication.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export WORKSPACE="$(pwd)"
export GIT_BRANCH=prod TEST_LEVEL=coverage
export PYTHONDONTWRITEBYTECODE=1
case "${1:-}" in
  test)
    bash "${script_dir}/secret-scan.sh"
    bash "${script_dir}/../go/go-build.sh"
    bash "${script_dir}/../go/go-test.sh"
    ;;
  contract)
    # Exact Fast Contract commands from the finalized 1.1.16 Jenkinsfile.
    . ./go-pipeline.env
    bash tests/ci-go-budget.sh
    bash tests/recognition-boundary.sh
    bash tests/model-runtime-dependency.sh
    bash tests/notification-dependency.sh
    bash tests/runtime-architecture.sh --artifact .gobuild/app
    bash tests/service-control-artifact.sh --ci
    python3 tests/lifecycle-lock.py
    python3 tests/controlled-deploy.py
    bash scripts/tests/paired-backup.sh
    python3 -m unittest discover -s scripts/tests -p '*_test.py'
    ;;
  *) echo 'Expected test or contract' >&2; exit 2 ;;
esac
