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

LOCAL validation:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 resources/scripts/common/release-app-binding.test.py
```

The fixtures use only private temporary files, synthetic Docker/LaunchAgent
observations and real `flock` descriptors. They exercise non-ASCII canonical
identity compatibility, lease handoff, pause races, intentional stops, unknown
state, foreign/busy work, partial commit, credential drift and resume failure.
They do not access a host service, product database, image, OCR, model or secret.
