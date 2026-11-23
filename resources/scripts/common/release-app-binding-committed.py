"""Signed one-shot continuation for the committed App binding from owner #105.

The fixed coordinator and owner must supply verified Jenkins run identities,
the existing signing key, and an independently registered native adapter.
There is no command-line entry, registration or fallback native operation.
The adapter must retain real locks, prove the reviewed files and runtime, and
own any managed bootstrap and release of this transaction's fences.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location("committed_binding_control", Path(__file__).with_name("release-promotion.py"))
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
gate, require = control.gate, control.require

COORDINATOR = "shiba-release-automation/shiba-go-ditch-api-project-auto-release"
OWNER = "shiba-release-automation/shiba-go-ditch-api-project-prod-deploy"
BASE_LIBRARY = "b354d9cc9e8d404f66950bae48f66de6a5b0bfd9"
TRANSACTION = "7f6fd5083c06bb654138c6c9a4346630"
FAILED_COMMIT = "13f9aab5cd6ed19faa08c6c6ac7ce204dc01b015"
ORIGINAL_COMMIT = "7ed4adc49884beacd225cab4946952924caf0cc3"
FAILED_DIGEST = "sha256:010fcc096f5bf750ac430c14cd037cd6b05ca4ee277cd93b304c0fdec50f56eb"
APP_DIGEST = "sha256:1628fe1821c2be174a3e4418cfa7a90edfbae671ce606ea81e13a26078d5178d"
APP_ID = "183a253fe295dd9f496479b280af15b57891d4970dc557d30a8703d76067b4b7"
APP_IMAGE_ID = "sha256:c32bc6a6a2f24686e0981ca1551900385da14fc9f2f0ad9973eb9d61ec53c203"
FAILED_SHA = "6223d276899cdd7949e7f0c470c6f1f366c446583926066eb706c25df8e582c4"
ORIGINAL_SHA = "0ac999ef2256706f4b397b5aaf8796b2886fcdf549ceeeb3f7a53a2f94083d37"
PENDING_SHA = "3f751a463fc9aafe25dfff67ec01d5897fbb5f8b96d069de6c7db422af648d0e"
OWNER_BINARY = "8f04aff46ce6f39fd60993d495355e47be107c9bd708d7861e896782280db583"
OWNER_PATH = ("/Users/surpend/Library/Application Support/ShibaServiceControl/releases/"
              "1.1.24-434043d1b1f002e8ca01223a7e4df5597124b9e5/owner-darwin-arm64")
FILES = {
    "recovery-105-attempt.json": "204df3cba34a55639c54559b47390975ee6a6be72bdb6e987d96772b8020b355",
    "recovery-105-failed.json": "41dc17f541f999626bd816e7138dac543336a543dc0c452d757c386650217eca",
    "owner-upgrade.pending.committed.json": PENDING_SHA,
    "commit-authorized.json": "18947cbdf6fa8f7f2d76ea6a2b6ca670adb4de214b851398f11faf737551a38d",
}
CONFIGS = {
    "consumer-admission.json": "7bb1b4b63fab38cc786f56334176c2c0d5fa7c6e362d91ba3003b775e03ce9b4",
    "installation.json": "cb5e508cdb81052f2b7b278405e0b08c911ba2a3acac8b250f0e598e2ac0ef24",
    "owner.json": "d81a6d7d040794f2021191ee359a584d903debe43a0e9b7290c4cae5ea4577ea",
    "registry.json": "5f66281051afab98a1a3fd48c11d40a33c405cbf6df39975568f8be3f45d31fd",
}
LOCKS = ("prod", "owner-upgrade", "consumer-helper-upgrade", "resources")
KIND = "app-binding-105-committed-continuation/v1"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def digest_json(value):
    return digest(gate.canonical(value))


def hex_digest(value):
    return isinstance(value, str) and gate.DIGEST.fullmatch("sha256:" + value) is not None


@dataclass(frozen=True)
class Admission:
    library_revision: str
    driver_sha256: str


@dataclass(frozen=True)
class OwnerRun:
    job: str
    build: int
    upstream_job: str
    upstream_build: int


@dataclass(frozen=True)
class Lease:
    identity: str
    locks: tuple


@dataclass(frozen=True)
class OwnerProof:
    pid: int
    started: str
    uid: int
    executable: str
    binary_sha256: str
    listener_pid: int
    owns_pinned_journal: bool


def signed_payload(envelope, key):
    require(isinstance(envelope, dict) and set(envelope) == {"payload", "signature"}
            and isinstance(envelope["payload"], dict) and hex_digest(envelope["signature"]),
            "invalid committed binding signature envelope")
    return control.verify(envelope, key)


def historical(failed_raw, original_raw, key):
    require(isinstance(failed_raw, bytes) and isinstance(original_raw, bytes)
            and digest(failed_raw) == FAILED_SHA and digest(original_raw) == ORIGINAL_SHA,
            "exact archived owner #105 and #103 receipts required")
    failed = signed_payload(json.loads(failed_raw), key)
    original = signed_payload(json.loads(original_raw), key)
    for receipt, status, commit, version, image in (
        (failed, "FAILED", FAILED_COMMIT, "1.1.29", FAILED_DIGEST),
        (original, "SUCCESS", ORIGINAL_COMMIT, "1.1.27", APP_DIGEST),
    ):
        require(receipt.get("kind") == "runtime-deployment" and receipt.get("product") == gate.PRODUCT
                and (receipt.get("status"), receipt.get("commit"), receipt.get("version"), receipt.get("digest"))
                == (status, commit, version, image), "archived owner identity differs")
    require(failed.get("mutation_may_have_started") is True and failed.get("automatic_rollback") is False,
            "owner #105 is not the reviewed failed mutation")


def check_admission(value):
    require(isinstance(value, Admission) and isinstance(value.library_revision, str)
            and gate.SHA.fullmatch(value.library_revision) and value.library_revision != BASE_LIBRARY
            and hex_digest(value.driver_sha256), "no independent fixed owner continuation approval")


def check_scope(scope):
    require(isinstance(scope, dict) and set(scope) == {
        "transaction", "phase", "engine", "runtime_sha256", "app_container", "app_digest", "app_image_id",
        "app_healthy", "files", "configs", "fences",
    }, "invalid committed binding snapshot")
    require(scope["transaction"] == TRANSACTION and scope["phase"] == "committed-installation.json"
            and isinstance(scope["engine"], str) and bool(scope["engine"])
            and hex_digest(scope["runtime_sha256"]) and scope["app_container"] == APP_ID
            and scope["app_digest"] == APP_DIGEST and scope["app_image_id"] == APP_IMAGE_ID
            and scope["app_healthy"] is True and scope["files"] == FILES and scope["configs"] == CONFIGS,
            "reviewed #105 transaction or restored App differs")
    fences = scope["fences"]
    require(isinstance(fences, dict) and 0 < len(fences) <= 32
            and all(hex_digest(name) and hex_digest(sha) for name, sha in fences.items()),
            "exact owned resource fences required")


def coordinator_request(*, job, build, author, approvers, admitted, scope, failed_raw, original_raw, key, now):
    """Called by the fixed coordinator after exact operator and artifact review."""
    check_admission(admitted)
    require(job == COORDINATOR and type(build) is int and build > 238
            and isinstance(author, str) and author.strip() == author and author and author in approvers,
            "fixed coordinator build and authorized incident operator required")
    historical(failed_raw, original_raw, key)
    check_scope(scope)
    return control.sign({
        "kind": KIND, "owner_job": OWNER, "coordinator_job": job, "coordinator_build": build,
        "failed_owner_build": 105, "original_owner_build": 103, "failed_sha256": FAILED_SHA,
        "original_sha256": ORIGINAL_SHA, "library_revision": admitted.library_revision,
        "driver_sha256": admitted.driver_sha256, "author": author, "scope_sha256": digest_json(scope),
        "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }, key)


def check_ready(proof):
    require(isinstance(proof, OwnerProof) and type(proof.pid) is int and proof.pid > 0 and proof.pid != 864
            and isinstance(proof.started, str) and bool(proof.started)
            and proof.uid == 501 and proof.executable == OWNER_PATH and proof.binary_sha256 == OWNER_BINARY
            and proof.listener_pid == proof.pid and proof.owns_pinned_journal is True,
            "exact admitted native owner generation, listener or journal is unconfirmed")


def owner_finish(request, *, run, admitted, approvers, native, state, failed_raw, original_raw, key, clock):
    """Consume the fixed incident and dispatch only to the admitted native port.

    The owner already holds prod.lock. The port acquires the remaining locks
    after the existing release-state lock, independently observes the reviewed
    files, running App/Redis/Gateway, and exact fences, and proves its durable
    pending archive. It alone may perform the already-committed managed resume
    and release this transaction's resource fences.
    """
    check_admission(admitted)
    require(native is not None and native.registration() == admitted,
            "fixed native owner continuation is not installed")
    grant = signed_payload(request, key)
    require(isinstance(run, OwnerRun) and run.job == OWNER and type(run.build) is int and run.build > 105
            and run.upstream_job == COORDINATOR and type(run.upstream_build) is int and run.upstream_build > 238
            and isinstance(grant.get("author"), str) and grant["author"] in approvers
            and hex_digest(grant.get("scope_sha256")), "fixed owner and trusted coordinator provenance required")
    issued, expires = gate.timestamp(grant["created_at"]), gate.timestamp(grant["expires_at"])
    require(grant == {
        "kind": KIND, "owner_job": OWNER, "coordinator_job": COORDINATOR, "coordinator_build": run.upstream_build,
        "failed_owner_build": 105, "original_owner_build": 103, "failed_sha256": FAILED_SHA,
        "original_sha256": ORIGINAL_SHA, "library_revision": admitted.library_revision,
        "driver_sha256": admitted.driver_sha256, "author": grant["author"], "scope_sha256": grant["scope_sha256"],
        "created_at": grant["created_at"], "expires_at": grant["expires_at"],
    } and issued < expires <= issued + timedelta(minutes=5), "wrong fixed continuation grant")
    historical(failed_raw, original_raw, key)
    lifecycle = native.lifecycle()
    require(isinstance(lifecycle, str) and bool(lifecycle), "existing prod.lock must be retained")

    def check_lease():
        require(issued <= clock() < expires and native.lifecycle() == lifecycle and native.lease() == lease
                and native.registration() == admitted,
                "continuation grant, admitted driver or retained owner locks changed")

    def check_context():
        check_lease()
        scope = native.scope()
        check_scope(scope)
        require(digest_json(scope) == grant["scope_sha256"],
                "live committed App binding drifted from reviewed scope")
        check_lease()
        return scope

    claim = digest_json([KIND, TRANSACTION, FAILED_SHA])[:40]
    with state.lock(create_missing=False), native.hold_remaining():
        lease = native.lease()
        require(isinstance(lease, Lease) and bool(lease.identity) and lease.locks == LOCKS,
                "existing lifecycle and owner locks must be retained")
        scope = check_context()
        require(not os.path.lexists(state.path(claim)), "committed binding continuation already consumed")
        require(native.pending() == PENDING_SHA and native.active_fences() == scope["fences"],
                "original committed pending or owned fence inventory differs")
        check_lease()
        record = {"kind": KIND + "/receipt", "transaction": TRANSACTION, "failed_owner_build": 105,
                  "owner_build": run.build, "coordinator_build": run.upstream_build,
                  "request_sha256": digest_json(request), "scope_sha256": grant["scope_sha256"],
                  "app_container": APP_ID, "app_version": "1.1.27", "app_digest": APP_DIGEST}
        state.write(claim, control.sign(dict(record, status="CLAIMED"), key))
        try:
            check_context()
            require(native.pending() == PENDING_SHA and native.active_fences() == scope["fences"],
                    "pending or fences changed before managed resume")
            check_lease()
            native.resume_committed(TRANSACTION, PENDING_SHA, expires)
            check_context()
            proof = native.ready_owner()
            check_ready(proof)
            require(native.pending() is None and native.active_fences() == scope["fences"]
                    and native.ready_owner() == proof and native.lease() == lease,
                    "managed startup retirement or exact owned fences are unconfirmed")
            check_lease()
            native.release_owned_fences(TRANSACTION, scope["fences"], proof, expires)
            check_context()
            require(native.pending() is None and native.active_fences() == {} and native.ready_owner() == proof,
                    "committed binding release outcome unconfirmed")
            check_lease()
            result = control.sign(dict(record, status="SUCCESS", owner_pid=proof.pid), key)
            state.write(claim, result)
            return result
        except BaseException:
            state.write(claim, control.sign(dict(record, status="FAILED", outcome="review_required"), key))
            raise
