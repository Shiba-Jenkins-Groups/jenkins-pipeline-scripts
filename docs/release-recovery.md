# App release recovery

The normal path remains develop branch CI, trusted coordinator verification,
prod merge and CI, then the signed PROD owner deployment job.

The macOS deployment agent must be managed by launchd, not by a terminal owned
by a conversation. `resources/scripts/common/install-prod-owner-agent.py` renders
the configuration by default; `--install` loads it for the existing logged-in
user. Supply the existing `--java` executable and `--agent-root`. It preserves
the existing private JNLP, refuses differing installed configuration, and does
not change Jenkins node identity or credentials. This is a user LaunchAgent:
logout stops it; a deployment requires that this user be logged in.

For an authorized retry after an infrastructure issue, submit SOURCE_BUILD and
EXPECTED_COMMIT to the fixed auto-release job. Both fields are required together.
Only a configured approver with a UserIdCause may use this path. The coordinator
loads the exact completed develop build and rejects a differing commit before
promotion. It still rechecks evidence freshness, every stage, scanners, approvals,
release state and signed deployment receipts. It never promotes an arbitrary
commit supplied by the caller, and does not turn failed CI into success.

The fixed job must expose these two String parameters before its first recovery
run; the Pipeline maintains them thereafter. Update the trusted library pin in
the managed job configuration when deploying this change. Normal upstream
triggers leave both parameters empty.

When both recovery parameters are supplied, the coordinator uses the `recover`
promotion command. If no claim exists, it performs the normal promotion. If a
claim exists, it may reuse only a correctly signed `MERGED` receipt whose source
commit, version, merge parents, and current remote develop/prod heads still
match. The eligible recovery build must be the original or a newer complete
build of that exact commit, so an expired candidate can be revalidated without
rewriting its original signed claim. `PREPARING`/`PUSHING` claims, older builds,
moved branches, existing release tags, or invalid signatures remain fail-closed.

Verification: run release-pipelines.test.groovy with the existing Groovy runtime
and CPS annotation jar; run each release-*.test.py directly (their dotted names
are not discovered by unittest discovery). Confirm launchd and Jenkins agree
that the single deployment node is running and online across separate sessions.

## Finalized artifact, failed owner deployment

This separate lane requires all four exact parameters: SOURCE_BUILD,
EXPECTED_COMMIT, RECOVER_COORDINATOR_BUILD and RECOVER_OWNER_BUILD. A unique
UserIdCause belonging to a configured approver is mandatory; Replay and mixed
causes are rejected. It supports only a first failed owner attempt with no new
backup/migration files and the original healthy runtime still present.

The coordinator authenticates the original native build chain and signed
promotion/finalization/request/FAILED receipt. It evaluates both complete
original evidence bundles at the actual current time under unchanged security
policy. Existing release heads, merge parents, peeled tag, published binary hash
and immutable image identity are rechecked. No merge, build, publication or
tagging occurs. Only clean PASS gates are supported; exceptions and stale
evidence fail closed. The new request expires at the earliest original evidence
or scan deadline, or fifteen minutes, whichever comes first.

The existing owner job receives the new signed request through a native
coordinator handoff. While holding both lifecycle and state locks, it requires
the exact signed FAILED state, healthy original container/image, unchanged
branches, engine and owner DB domain. It creates and fsyncs an immutable private
attempt archive before recording the new claim. It never deletes the prior
receipt or pretends the previous failure was a success. Replays, ambiguous state,
changed runtime, existing attempt archives and recursive recovery are blocked.
Failure preserves the new attempt too; rollback remains separately authorized.

If the one-hour original evidence window expires, stop. This lane does not
refresh timestamps or permit rebuilding an already finalized version.

## Published PROD disaster rebuild (2026-09-16)

An explicitly authorized operator may submit `REBUILD_PROD_COMMIT` and
`PUBLISHED_COORDINATOR_BUILD` to the pinned coordinator. This is separate from
failed-attempt recovery and rejects mixed parameters, Replay and non-approvers.
The original successful coordinator and signed promotion/finalization/request
prove publication; the current prod head and immutable version tag must still
match. No develop run, promotion, tag overwrite or old evidence timestamp refresh
occurs. The prod branch executes its full CI once, then all native evidence,
package graphs and scanners are revalidated under the existing gate policy.

New binary bytes use the new build-specific Nexus path; the old published binary
and tag remain intact. A schema 3 signed deployment handoff binds fresh evidence,
new image digest, source, operator and the configured restored Docker engine.
Only the existing owner job may consume it. The owner requires the original
signed successful deployment record, missing PROD app containers, the existing
nonempty PROD DB file, and no competing database owner. It calls the original
source's `scripts/deploy.sh prod deploy` under both existing locks. Normal schema
2 deployment and failed-attempt recovery guards remain unchanged.

The disaster attempt gets a deterministic separate state key derived from prod
commit and restored engine ID. The original successful receipt is not replaced.
A claimed/failed/successful disaster attempt blocks another attempt on the same
engine; reconciliation needs separately scoped repair. Automatic rollback and
empty-database bootstrap are forbidden. There are 3 focused offline data/owner
contract tests and 7 mocked normal/rebuild control-flow checks, with no product
DB, model execution or live deployment in those checks.
