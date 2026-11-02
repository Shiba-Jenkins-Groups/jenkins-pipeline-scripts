# Controlled Apple Vision native release artifacts

The fixed manual job is `shiba-release-automation/apple-vision-native-release`.
It produces macOS arm64 OCR, service-control owner CLI and an OCR-only Manifest
tool from exact, independently pinned source commits. It never changes a running
service, opens a product database, signs a Manifest, pushes a branch/image or
starts a downstream deployment. Native source identity is separate from the
unchanged Recognition image's successful parent CI identity.

`vars/ocrNativeReleasePipeline.groovy` must be loaded by an immutable reviewed
trusted Library commit. The job has no parameters/triggers, accepts exactly one
approved `UserIdCause`, rejects Replay/mixed causes, and disallows concurrent
builds. Its 30-minute timeout includes waiting for the single existing
`shiba-prod-owner-macos` executor. Agent lifecycle uses the separately authorized
installed session manager; this Pipeline neither starts/stops agents nor uses
the older persistent `install-prod-owner-agent.py` configuration.

## Pinned job interface

Create a nonsecret identity JSON with these fields:

```json
{
  "schema_version": 1,
  "job_name": "shiba-release-automation/apple-vision-native-release",
  "build_number": "1",
  "build_url": "http://127.0.0.1:9090/job/shiba-release-automation/job/apple-vision-native-release/1/",
  "source_commit": "REPLACE_NATIVE_40_HEX",
  "source_baseline_commit": "REPLACE_DEPLOYED_RECOGNITION_SOURCE_40_HEX",
  "owner_source_commit": "REPLACE_OWNER_40_HEX",
  "owner_baseline_commit": "REPLACE_DEPLOYED_OWNER_SOURCE_40_HEX",
  "library_revision": "REPLACE_LIBRARY_40_HEX",
  "requested_by": "claude-code-use",
  "owner_package": "./cmd/service-control-owner",
  "owner_test_run": "^TestOCRReplacement",
  "go_version": "go1.27.2",
  "scanner_tools": {
    "gitleaks": {"module": "github.com/zricethezav/gitleaks/v8", "version": "v8.30.1"},
    "govulncheck": {"path": "/Users/surpend/go/bin/govulncheck", "sha256": "REPLACE_REVIEWED_BINARY_SHA256"}
  },
  "parent_ci": {
    "schema": "recognition-parent-ci-v1",
    "status": "SUCCESS",
    "source_commit": "REPLACE_PARENT_IMAGE_40_HEX",
    "image": "REPLACE_REPOSITORY@sha256:REPLACE_IMAGE_64_HEX",
    "job_name": "shiba-go-ditch-recognition-project/prod",
    "build_number": 46,
    "build_url": "http://127.0.0.1:9090/job/shiba-go-ditch-recognition-project/job/prod/46/",
    "artifact_hash_inventory": {
      ".pipeline/release-manifest.env": "REPLACE_ARCHIVED_BYTES_SHA256",
      "reports/runtime-image.json": "REPLACE_ARCHIVED_BYTES_SHA256",
      "image-ref.txt": "REPLACE_ARCHIVED_BYTES_SHA256"
    }
  }
}
```

Build number/URL in this renderer input only validate the complete identity
shape; the running Pipeline substitutes its actual Jenkins build identity. All
source/parent/scanner pins and the approved manual user remain fixed in job XML.
Never use a moving branch or a locally built binary as release input.

Render a disabled job for review, without contacting Jenkins:

```bash
python3 resources/scripts/common/ocr-native-release.py render-job --identity /absolute/identity.json > /absolute/reviewed-disabled-job.xml
```

Only separately authorized registration/activation uses `--enable` after
reviewing the exact rendered configuration and Library/source pins. The example
placeholders intentionally fail validation. No JCasC or node change is required.

## Build and qualification

The job checks clean source HEADs before and after work. It runs the focused
preparation-receipt Python test; the owner's `^TestOCRReplacement` hostcontrol
tests; owner and Manifest-tool vet; Manifest-tool package tests; gitleaks on the
complete source deltas from the reviewed Recognition source and deployed-owner
baseline (each must be an ancestor); and govulncheck on each compiled Go command. Gitleaks
uses built-in rules through stdin from an empty control directory, with no
repository allowlist, and reports are redacted. Its existing CI version 8.30.1
may be built into a task-local GOBIN through the existing Nexus Go proxy and
sumdb; there is no fallback proxy/global install. Govulncheck must already exist
at its reviewed exact binary SHA. All scanner failures, findings, malformed
reports, missing tools, Nexus failures and toolchain drift stop the build.

The existing native builder compiles OCR and writes its source/binary/toolchain/
parent identity. CI then performs exactly one `-check-compatibility`, with a
120-second native budget and 135-second outer bound, in that new binary's PROD
cache namespace. It uses private logs and verifies ready, runtime/binary identity,
receipt-smoke policy and prepared-cache namespace. No user image or repeated
model/corpus evaluation runs. This startup smoke is not OCR quality certification.

The archive includes:

- `native/artifact.json`, `native/parent-ci.json`, `native/cache-info.json` and
  `native/shiba_ocr_prod_<binary-sha-prefix>`.
- `owner/artifact.json` and `owner/service-control-owner`, with source-bound Go
  build information.
- `manifest-tool/artifact.json` and `manifest-tool/ocr-release-manifest`; CI has
  no private signing key.
- `reports/startup-smoke.json`, both redacted gitleaks reports, both raw
  govulncheck streams and `reports/scan-summary.json`.

The fixed Jenkins artifact whitelist is `artifact/ocr-native-artifact.tar`,
`artifact/ci-receipt.json`, `artifact/SHA256SUMS`, `artifact/contracts.log`.
The raw CI receipt records `result=PASS` and `pipeline_final_success=false`:
local file integrity cannot attest a completed Pipeline.

## Authenticated readback and owner handoff

After the exact build completes, use the same reviewed central helper's
`collect` command with existing Jenkins credentials held only in the environment
for that process (`JENKINS_API_USER`, `JENKINS_API_TOKEN`). Never echo or persist
them. The collector makes GET requests only to fixed local Jenkins paths and
rejects redirects/proxies. Required explicit inputs are `--build`,
`--source-commit`, `--owner-source-commit`, `--library-revision`, `--requested-by`
and a new `--output` directory. It verifies final SUCCESS, all four workflow
stages, one approved manual cause, exact native/owner/Library repository checkout
proof, a single trusted pinned LibrariesAction and the artifact whitelist. It
also authenticates the fixed parent build's final SUCCESS, source, three archived
hashes and immutable runtime-image receipt. It checks all archive members,
component binaries and startup/scan reports without extracting archive paths.

Successful output is `trusted-build-evidence.json`, schema `ocr-native-ci-v1`,
`status=SUCCESS`, `pipeline_final_success=true`, flattened job/build/native-source/
owner-source/Library identity, the parent CI identity, all raw API/workflow hashes,
`artifact_hash_inventory` keyed by archive entry path, and separate raw Jenkins
artifact hashes. The collector does not deploy. The reviewed owner must bind the
raw evidence, CI receipt and archive SHA values, exact new component receipts,
old runtime/owner identities and the signed OCR-only Manifest before its own
exclusive/drain transaction. A successful artifact job never counts as deployed.

The separate fixed job `shiba-release-automation/apple-vision-native-deploy`
uses `ocrNativeDeploymentPipeline` and the exact Recognition source's
`ocr_release_session.py apply`. Render its disabled configuration with
`ocr-native-release.py render-deploy-job --config /absolute/deploy-config.json`.
The configuration has only `libraryRevision`, `sourceCommit`, `approvers`,
`plan`, `planSHA256`, `ciReceipt`, `ciReceiptSHA256`, `artifactDirectory`. Pins are
full reviewed SHAs; paths are the exact private prepared plan, authenticated CI
receipt and preparation's `ci-artifacts` directory. `--enable` is a separate
explicit rendering choice; rendering never registers or triggers a job.

## Focused implementation verification

`python3 -B resources/scripts/common/ocr-native-release.test.py` exercises offline
source/parent independence, component/archive tampering, unsafe member rejection,
startup identity mismatch, exact Jenkins/Library/manual-cause proof, renderer
defaults and incomplete-build refusal. `ocr-native-release-flow.test.groovy`
checks fixed job/node/source/manual flow and compiles the actual Pipeline with
the installed Jenkins CPS jars. These checks do not contact Jenkins, run OCR,
open databases or replace native production acceptance.
