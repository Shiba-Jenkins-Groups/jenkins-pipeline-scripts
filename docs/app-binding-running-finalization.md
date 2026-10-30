# Finalize an already running owner after an unconfirmed restoration

This is a LOCAL candidate. It has not been published, enabled, or used to
release a live resource fence. Its later activation and once-only finalization
require explicit incident authorization. The App 1.1.20 release and its managed
Jenkins session have not been started; their existing authorization is retained
and remains conditional on successful incident finalization.

## Observed incident and evidence limits

Library `5f631e5` passed DEV rehearsal #6 and was activated in the fixed central
jobs. The original transaction `58c9d1e320e5e1de80981692d0002703` then passed
fresh collection and planning. Its single bootstrap was submitted, but the
post-bootstrap observer rejected `foreign_owner_process`. The recovery receipt
therefore remains `outcome-unconfirmed`, with no fence released.

A later read-only observation found PID 21988, UID 501, the exact original
1.1.18 executable and SHA256, and the expected listener. That does not prove
which UID or path was observed by the earlier failed read. The original error
did not retain that information. A launcher/exec transition is a possible
explanation, not a retrospective finding or a reason to mark the old receipt
successful.

All eight resource fences remain closed. The original startup marker is
archived under the original recovery claim. Repeating the original apply,
removing its claim, or directly calling its internal release code is not an
eligible recovery route.

## Bootstrap observation

The owner observer must retain safe structured PID, UID, kernel-path, phase and
error metadata. It never saves arbitrary native stdout, credential contents or
model responses. A process that has not yet reached the expected executable is
not an authenticated or ready owner.

Only after the one verified managed bootstrap is acknowledged may the observer
perform bounded read-only convergence checks for explicitly supported temporary
states. The first observed process generation is pinned. Readiness still
requires the exact UID, expected executable and binary digest, the same new PID
and process start identity, unique listener and journal lifetime lock. A foreign
complete identity, reused PID, incorrect digest, unknown launchd result, or
expired deadline fails closed. No second bootstrap, automatic stop or automatic
retry is introduced. Existing-owner checks before bootstrap and pre-pause
identity checks remain strict.

This LOCAL regression contract models supported transitions and refusal cases.
It does not assert that a modeled transition caused the historical incident.

## Separate finalize-only transaction

The new entry has no bootstrap, bootout, service-driver, inference, OCR or model
API action. It preserves the original claim, failure receipt and archived marker
unchanged and creates a separate fixed once-only finalization claim. Changing
the plan cannot grant another attempt.

Collection and planning bind the old producer's immutable Git objects and raw
claim/plan/collection/failure evidence, plus the new finalizer's exact source.
They require the specific incomplete-resume phase, archived startup marker,
zero previously released or unknown fences, all eight exact original markers,
and no original completion or release-intent evidence. The original App
container generation, configuration, credentials, artifact and state pins must
still match. The approved running process is an exact PID/UID/start/path/hash
identity, not an arbitrary process with the same binary.

The existing lifecycle, owner-upgrade, helper-upgrade and all resource EX locks
protect finalization. The live journal's lifetime EX belongs to the owner and
must not be acquired by this tool. Instead the verifier requires the approved
process to have the same journal device/inode open, a positive busy-lock
observation, the original checked prefix, and a complete idle journal snapshot.
Only incomplete concurrent append observations may be read again within a
finite bound; a complete active or unknown operation remains a failure.

Apply rechecks a fresh, hash-bound, unexpired plan before claiming and before
the first marker removal. A durable release-intent records the exact owned
markers. Only those markers may be released, with rechecks and synchronization.
Partial release, interruption and output publication failure report the known
released/present/unknown sets and retain their claim. The tool does not recreate
the startup marker, rewrite intent or settings, overwrite the old receipt,
restart a process, or roll back an uncertain result.

Success proves only finalization of the original 1.1.18 owner. After admission
is released, its unchanged Ollama/oMLX/OCR recovery policy resumes normally;
startup and status metadata probes are not ruled out. This is not evidence of
an App 1.1.20 deployment, browser acceptance, or a new model bootstrap.

## Proposed activation and continuation

Publish the final reviewed Library source to its isolated branch and validate
it once in the existing nonprivileged DEV offline rehearsal. Only a successful
exact-source contract receipt permits the narrow central-job and JCasC pin
update. The [activation template](owner95-library-activation.template.patch)
contains four literal `5f631e5` to reviewed-source substitutions; its placeholder
must be rendered and the before hashes must match before any live update.

After separate once-only finalization authorization, collect and plan from the
reviewed original evidence and approved running-owner identity, then apply that
exact plan once. Any evidence drift or failure stops the sequence. Do not
reuse the old apply or fabricate a deployment failure receipt.

After a completed finalization receipt, the existing unused authorization for
App `c9cc7dd` / 1.1.20 and one needed managed Jenkins owner session can continue
through the normal develop, coordinator, PROD, finalization and owner flow.
All tests, scans, signatures, image and deployment gates remain in force.
No Harbor operation, Factory bootstrap, credential change or model-policy
change is part of this plan.
