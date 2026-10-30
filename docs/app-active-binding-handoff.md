# Active App Docker binding handoff

`release-app-binding.py` is a native-only Shared Library component of the trusted
Jenkins deployment owner. It changes no App source, version, tag, image, native
owner binary, credentials, model, or other service definition. It is not a
standalone operator deployment command. The deployment request still has to pass
the normal signature, source, finalization, immutable-image and expiry gates.

The caller holds the existing App lifecycle EX descriptor. After its read-only
preflight, but before claiming/deploying the App, it calls:

```python
transaction = AppBinding.begin(validated_request, runtime_root, lifecycle_fd)
try:
    env = transaction.env_for_deploy(env) if transaction else env
    descriptors = transaction.pass_fds() if transaction else (lifecycle_fd,)
    # Existing exact-source deploy.sh invocation; no alternative deploy path.
    subprocess.run(command, env=env, pass_fds=descriptors, check=True)
    observed = final_identity(validated_request, image_id, runtime_root)
    binding_receipt = transaction.finish(observed) if transaction else None
    # Only now may the normal signed runtime-deployment receipt be SUCCESS.
finally:
    if transaction:
        transaction.close()
```

`begin` returns `None` only for a genuinely unactivated legacy installation. A
missing global admission registry with an activated installation fails closed.
Nonblocking resource locks avoid waiting for another job while the caller holds
the App lifecycle lock. A busy job, stopped/unknown intent, foreign pending,
unfinished operation, changed owner/artifact or drifted configuration blocks
before any App/container mutation.

Before creating pending or pausing the recovery owner, its hash-verified
official `client --action status` must report OCR and Recognition both desired
running, process running and contract ready at their exact registry revisions.
This reads metadata and adds only the owner's normal nonce audit records; it
does not warm models or OCR. Unknown operations, attempting/uncertain recovery,
unreleased drains and fences block even if the platform currently looks healthy.

For an active installation, the tool locks all existing service resources,
backs up the exact settings, original intents and historical journal, and writes
durable pending evidence. It pauses only the exact installed managed owner
LaunchAgent, leaving OCR, Recognition and the other processes running. A second
journal check after acquiring its lifetime EX lock rejects an operation racing
with pause. The child deployment inherits one normal, exact old-scope consumer
job and its SH descriptors; the App lifecycle descriptor remains EX. Nested
admission verifies that lease without admitting another job.

After the existing deployment verifier reports the exact healthy new App, the
tool reclaims resource EX and verifies the actual CID/image, fixed engine,
Compose labels, loopback port, version and unchanged database mount. Only the
App target/revision, its status identity evidence, corresponding global keys and
installation digests change. A new running intent requires both the original
running intent and the new healthy observation; an existing new-key intent is
never overwritten. The old intent and original history remain intact. The
owner's effective App permissions stay status-only; all other services, grants,
recovery policy and credentials remain unchanged.
Consumer resource lists retain their original order; entries without the old
App key remain identical.

Pending protects the sequential atomic file renames. Only after all pointers
are consistent does the same native owner restart from the same managed plist.
Its exact binary, new PID, listener and journal lifetime lock establish that it
loaded the committed configuration. Only then are the tool's own consumer job
and resource pending markers removed. The public handoff receipt is included in
the normal signed deployment receipt.

Any interrupted or failed transaction preserves its private
`prod/app-binding-<id>` backup, pending markers and accepted-work job. There is no
automatic rollback, retry, unknown-state reset, marker deletion or TTL release.
`BindingError.receipt` contains only public phase/transaction/backup metadata;
the owner archives it even when begin fails before the normal deployment claim.
An uncertain owner pause is distinguished from a confirmed pause. A failed
resume retains resource admission fences and restores the startup pending gate.

The separately authorized, pre-deployment pause incident restoration contract
is described in [App binding pause incident](app-binding-pause-incident.md).
It restores the original owner only; it is not an alternative App deployment
route or an automatic cleanup of failed transactions.

LOCAL validation:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 resources/scripts/common/release-app-binding.test.py
```

The fixtures use only private temporary files, synthetic Docker/LaunchAgent
observations and real `flock` descriptors. They exercise non-ASCII canonical
identity compatibility, lease handoff, pause races, intentional stops, unknown
state, foreign/busy work, partial commit, credential drift and resume failure.
They do not access a host service, product database, image, OCR, model or secret.

## Repair a ready installation after an already successful deployment

The fixed coordinator also accepts the mutually exclusive
`APP_BINDING_REPAIR_COORDINATOR_BUILD`, `APP_BINDING_REPAIR_OWNER_BUILD`, and
`APP_BINDING_REPAIR_PINS` parameters. This is explicitly authorized native
binding maintenance, not an App release or deployment retry. It performs no
source checkout, build, scan, finalization, image publication, App replacement,
database access, or model operation.

The pins are a JSON object containing exactly the raw SHA256 of the installed
`prod/owner.json`, `prod/registry.json`, `prod/installation.json`, and root
`consumer-admission.json`, using their basenames as keys. An authorized unique
user cause and exact original successful coordinator/owner builds are required.
The coordinator reads those original artifacts and verifies the original
deployment request and successful runtime receipt with its HMAC key. Historical
deployment expiry is checked at the original attempt; it is never renewed.
A new signed `app-binding-repair` request expires within fifteen minutes and
binds all four pins, the original receipts, a canonical UUID4, the operator,
the new coordinator build, and the reviewed Library revision.

The same fixed owner job receives this request in `SIGNED_RELEASE_REQUEST`.
Only its verified maintenance identity can select the repair branch; the
coordinator build and Library revision must match its trusted configuration.
The owner requires its persistent original successful deployment record to
remain byte-for-byte canonically equal to the signed prior receipt. A separate
claim derived from the receipt and binding pins prevents a new authorization
UUID from repeating the same repair; it never overwrites the original App
deployment record.

`AppBinding.repair` reuses the existing private backup, all-resource EX locks,
pending barriers, journal verification, managed owner pause, four-file commit,
same-binary resume and release checks. It does not create a deployment child
job or downgrade resource descriptors to SH. The old binding must match the
prior receipt's previous CID/image. Missing old containers require a complete,
successful full-ID Docker inventory; an inspect/daemon error is not absence.
An existing retired container must match its original identity and be inactive.
The current trusted runner additionally requires a unique Compose App target;
retaining a second stopped App under the same Compose identity blocks that
runner. This is not permission to delete it to make maintenance pass.
The current App must match the signed new observation, immutable image, engine,
labels, loopback port, version and database mount. An existing new-key intent,
stopped/unknown old intent, active job, foreign pending, changed pin, unfinished
journal or ambiguous owner blocks repair.

The resulting signed receipt has kind `app-binding-maintenance` and explicitly
records `app_deployed=false`. It proves only the asserted native binding
maintenance. Any uncertain result retains its one-attempt claim and forbids
automatic retry or rollback. If the final completion-artifact write fails after
all pointers were committed and admission released, the public status is
`outcome-unconfirmed` with both facts stated; it does not falsely claim that
barriers are still present.

Both central jobs must use the same reviewed Library SHA. A read-only routing
check validates the actual owner job's annotation and configured revision;
partial activation fails before any normal release or maintenance mutation.
See [the narrow activation proposal](app-binding-owner-routing.md). Activation,
any live maintenance invocation, and a required managed owner session remain
separately authorized operations.

Additional LOCAL fixtures:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 resources/scripts/common/release-app-binding-repair.test.py
PYTHONDONTWRITEBYTECODE=1 python3 resources/scripts/common/release-binding-repair.test.py
```
