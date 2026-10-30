# Pre-deployment App binding pause incident

This change is prepared and tested in LOCAL only. It has not been published or
enabled in Jenkins, and it has not cleared a live pending marker, restored an
owner, or retried a deployment. The target repository's CI/CD policy and the
operator's explicit incident authorization govern any later activation.

## Stop observation

The normal binding transaction still validates the installed owner before
pausing it. Durable diagnostics record fixed identity and error metadata,
without native stdout, stderr, credential bytes, or model responses. After the
last durable pre-pause record, the owner identity is checked again immediately
before the single bootout request.

Only an acknowledged bootout opens the existing fifteen-second observation
window. An incomplete observation of that same original PID can be observed
again in this window. A different PID, UID, binary, path, or unknown launchd
result remains a failure. Success requires an explicit launchd not-loaded
result before the deadline. Native query timeouts use the remaining budget,
and a late successful observation is rejected. Local filesystem I/O is not a
hard real-time guarantee. No second bootout, automatic rollback, or background
physical-action retry is introduced. Resume validation remains unchanged.

## Incident eligible for restoration

The new recovery entry restores the original owner after an `authorized`
binding transaction failed before `owner-paused`, `deploy-admitted`, or any
binding commit. It never substitutes for the deployment owner and never
creates a failed deployment receipt to make an ineligible retry pass.

Collection and planning check the exact transaction, private backup, pending,
failure evidence, four original configuration files, installed artifact and
credentials hashes, historical journal prefix, current idle journal, original
App container identity, Docker Engine, and resource state. The owner must be
explicitly unloaded and the original App must remain the unique healthy App
under its Compose identity. A foreign job, changed source, intentional stop,
unknown active state, replacement container, incomplete journal, or evidence of
a later transaction phase blocks restoration.

The existing lifecycle, owner upgrade, helper upgrade, all current resource,
and journal locks protect the checks. Content and file identity are checked;
holding an obsolete lock inode is not sufficient. Historical state files are
preserved and pinned, without turning them into active recovery targets.

## One bounded restoration

Apply requires the reviewed plan's exact hash and rechecks its inputs under the
same locks. A durable claim belongs to the original transaction; making a new
plan cannot grant another bootstrap attempt. The old installation pending is
archived into its existing private incident backup. Resource fences remain
closed while the original owner starts through its managed plist.

Only the original artifact, expected UID and executable, a new PID, the exact
listener and the owner's lifetime journal lock can qualify the restored owner.
Configuration, credentials, intent and historical journal data are not rewritten
by this tool. The checked journal prefix must remain intact. Only this
transaction's fences may be released after successful verification.

An uncertain result retains its one-attempt claim and reports the actual owner
and fence-release state. It does not claim that every marker remains when
release was partially completed, restore an overwritten startup marker, kill a
partially started owner, or automatically repeat the operation.

The tool makes no model, OCR, inference, or business API request. The original
owner can issue its normal metadata probes during startup. Resource pending
and EX locks prevent physical service control during identity verification;
after release, the owner's unchanged recovery policy resumes normally. The
receipt therefore does not claim zero metadata HTTP requests or browser/UI
acceptance.

## Entry point and evidence

`resources/scripts/common/release-app-binding-incident.py` exposes three explicit
actions. Command flags record the requested scope; they are not a substitute for
human authorization or the repository's CI/CD policy.

| Action | Required reviewed input | Managed effects |
| --- | --- | --- |
| `collect` | Inactive incident metadata raw SHA256, exact transaction, runtime root and reviewed Library commit | Read-only checks under existing locks; no lock-file creation |
| `plan` | Collection path and raw SHA256 | Recollects and requires the same snapshot; produces a plan valid for at most fifteen minutes |
| `apply` | Plan path, exact raw SHA256 and explicit once-only resume flag | Claims this original transaction once, archives its startup marker, bootstraps its original owner once, verifies identity and releases only its fences |

The CLI requires private output outside the managed installation and App runtime,
and refuses an existing output path. Inputs are regular, owned private files;
symlinks, duplicate JSON keys, changed inode/content and source drift fail closed.
Runtime use requires a checkout whose exact reviewed commit contains both helper
files unchanged. A copied untracked helper cannot supply its own provenance.

An apply claim remains even when bootstrap or receipt publication is uncertain.
Neither changing a plan nor resubmitting a command grants another attempt.
Inspect the preserved internal claim and completed/failure receipt instead of
repeating an operation to recreate missing output.

## Deployment continuation

Coordinator #205 / owner #95 failed before the normal deployment claim. The
existing deployment recovery mode requires a signed `FAILED`
`runtime-deployment` receipt, which this incident did not produce. The existing
binding repair mode requires an original successful deployment. Neither mode
can legally consume #95, and no receipt or claim should be fabricated.

App 1.1.19 has finalized artifacts but was not deployed. A proposed continuation
preserves those artifacts and the incident history, restores the original
1.1.18 owner once after separate approval, then releases App 1.1.20 through the
normal develop-to-PROD path. The App candidate changes only release metadata
from the already LOCAL-validated login/navigation fix. This is a proposal, not
authorization to publish, update central job pins, restore a runtime, or deploy.

The [activation template](owner95-library-activation.template.patch) records only
the four current `5f631e5` coordinator/owner Library pins in the JCasC source.
Its placeholder is intentionally not executable. A separately authorized
activation must render the final reviewed commit, compare the captured source
and live job hashes, and update both fixed jobs consistently. The existing
offline rehearsal job has its own two runtime pins and must validate that same
source first. No global Library default, credentials, triggers, or other jobs
are changed by this proposal.

The existing offline DEV rehearsal loads the new pause and incident fixture
suites. Its receipt still states `DEV_CONTRACT_ONLY`,
`runtime_deployed=false`, and `prod_enabled=false`. No live recovery action is
added to that rehearsal or to an automatic job trigger.

The separate [running-owner finalization contract](app-binding-running-finalization.md)
handles an already submitted bootstrap whose verification did not finish. It
does not rerun this restoration transaction or reinterpret its failed receipt.
