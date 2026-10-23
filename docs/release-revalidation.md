# Unchanged finalized image: fresh verification

This explicit fixed-coordinator lane retains the original develop source,
promotion, version, immutable image, Nexus bytes, Git tag and signed failure.
It does not use the disaster-rebuild lane and does not extend old evidence.
It accepts only an original, non-recursive failed owner attempt with no new
backup/migration files and no automatic rollback. The owner's existing runtime,
database-domain, lifecycle/state locks and immutable attempt archive remain
mandatory.

`release-revalidate.py collect` authenticates the complete original native
upstream chain and promotion/finalization/request/FAILED signatures. Original
evidence is gated at the signed failed attempt's historical completion, never
given a new completion time. Security policy must equal the original policy
apart from the trusted library revision. Original source CI stays bound to its
original build; a newer develop run is not substituted into that signature.

The trusted coordinator then checks out the unchanged promotion commit and
runs complete fixed secret/build/vet/test and contract commands. Each `check`
records the actual coordinator job/build, source, command, start/end, exit status
and raw execution bytes. The test report must contain passing non-skipped JUnit
cases. JUnit and runtime reports must not exist before their check starts. Only
known generated CI output paths may be excluded from the clean-source check;
tracked source is never changed. Scans may use another clean checkout of the
same exact promotion commit.

Fresh package graphs, govulncheck, Trivy and Harbor scans use the normal native
collector and gate rules, including the exact conditional GO-2026-5932 rule.
The original image is checked with `runtime-image-verify.py --revalidation`
using a separate fresh workspace. Its six-field `revalidation_run` records the
real coordinator and the original image build separately. Empty-container
bootstrap, health, graceful shutdown and cleanup must all pass. No production
image, database, model, cache or service is used in these LOCAL fixtures.

The coordinator invokes the helpers with its real `JOB_NAME`/`BUILD_NUMBER`:

```sh
python3 control/release-revalidate.py collect --root revalidation \
  --policy control/policy.json --receipt-key-file "$RECEIPT_KEY_FILE" \
  --jenkins-url "$RELEASE_JENKINS_URL" --coordinator-build 193 \
  --owner-build 90 --source-build 357 --expected-commit '<original-develop-40sha>'

python3 control/release-revalidate.py check --root revalidation \
  --source /absolute/exact-source --kind test \
  --report-path /absolute/exact-source/reports/junit/go-tests.xml -- \
  bash /absolute/control/release-revalidation-checks.sh test

python3 control/release-revalidate.py check --root revalidation \
  --source /absolute/exact-source --kind contract -- \
  bash /absolute/control/release-revalidation-checks.sh contract

python3 control/release-revalidate.py check --root revalidation \
  --source /absolute/exact-source --kind runtime \
  --report-path /absolute/revalidation/runtime-workspace/reports/runtime-image.json -- \
  env WORKSPACE=/absolute/revalidation/runtime-workspace \
  python3 /absolute/control/runtime-image-verify.py --revalidation /absolute/runtime-context.json

python3 control/release-revalidate.py scan --root revalidation \
  --source /absolute/clean-exact-source --policy control/policy.json --harbor-url "$HARBOR_API_URL"

python3 control/release-revalidate.py handoff --root revalidation \
  --source /absolute/clean-exact-source --policy control/policy.json \
  --receipt-key-file "$RECEIPT_KEY_FILE" --approver "$RECOVERY_APPROVER" \
  --output deployment-request.json
```

For the runtime command, its `WORKSPACE` must point to the separate runtime
workspace containing the original image metadata; the outer coordinator
environment stays unchanged. All paths above are supplied by the fixed trusted
Pipeline, not a product-controlled recovery parameter.

The fresh envelope is `finalized-image-revalidation-v1`, not a new PROD CI build.
Only clean PASS results can produce a schema 4 deployment. It retains the
original promotion/finalization and historical `evidence_sha256`; the independent
fresh digest lives in `revalidation.evidence_sha256`. `revalidation` also embeds
the exact original signed request. The existing six-field `recovery` contract
and `kind=failed-deployment-retry` are preserved, with an attempt UUID fixed at
collection. `validate_identity(request,key,now)` is the owner's schema 4 verifier.
The owner archives the exact original FAILED state before claiming the retry;
an existing attempt or recursive failure cannot be reused.

The new handoff lasts at most 15 minutes and never beyond the earliest fresh
check/scan deadline. Revalidating cannot relabel original failures, rebuild an
image, republish Nexus, overwrite a tag, bypass an exception or retry a failed
owner recursively. Failure preserves the collected evidence and new check
artifacts. Run `python3 -B resources/scripts/common/release-revalidate.test.py`
for hermetic contract tests; these are not live deployment proof.
