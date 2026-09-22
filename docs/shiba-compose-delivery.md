# Shiba delivery simplification, 2026-09-22

Scope: App and Recognition only. Other projects retain the legacy pipeline.

| Lane | Required work |
|---|---|
| App develop | checkout/secret/build/vet/test/package, no image or release |
| App prod | test/contract/build image/push/isolated runtime verification |
| App coordinator | native evidence + one govulncheck, Trivy, Harbor scan each; finalize; signed owner handoff |
| App owner | existing locked/backup-aware deploy + readiness and digest receipt |
| Recognition DEV | local build/deploy; no Jenkins branch CI |
| Recognition PROD | runtime build/tests/boundary + scan/push/isolated verify/finalize; existing runtime-deploy |

The Compose gate replaces both the generic smoke and Kubernetes deployment gate.
It checks the exact immutable image, nonroot startup, App empty DB migration,
health, graceful shutdown and cleanup. It has no host bind mounts or published
ports. Recognition `/readyz` and current Manifest identity are checked by the
actual deployment entry point, not by a CI container without model dependencies.

`controlled-compose-v2` moves App scanners from the branch pipeline to the trusted
coordinator. All three scanner envelopes, raw findings, source/digest binding,
freshness and explicit exceptions are still mandatory. No failed scanner is
relabelled SUCCESS. Old v1 candidates and signed recovery artifacts retain their
old contract. The library combines current main's lean/capacity implementation
with the already-reviewed recovery fixes from 9635613, then pins all participants
to one revision.

Trivy uses the persistent trusted agent cache and still performs normal DB update
checks. Heavyweight Manifest handoff is reserved for an actual Manifest change.
No Jenkins, Harbor, Nexus or k3d shared service is removed by this change.

Validation: Python evidence/gate/recovery tests; Groovy flow mocks; real Jenkins
contract rehearsal; isolated checks against both existing published images;
then new PROD releases with exact queue/build/digest and runtime verification.
Mock tests alone are not reported as live deployment success.

References: [Jenkins Pipeline best practices](https://www.jenkins.io/doc/book/pipeline/pipeline-best-practices/)
and [Trivy database configuration](https://trivy.dev/docs/dev/configuration/db/).
