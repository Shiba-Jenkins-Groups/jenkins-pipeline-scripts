"""Read-only closeout of the native App binding already completed by build #1."""

import os
from pathlib import Path


JOB = "shiba-release-automation/shiba-go-ditch-api-project-prod-binding-recover-115"
APPLY_LIBRARY = "7206654a606b48ebb29ac47add47f131e289aaa1"
APPLY_RECEIPT = "c5becf2e86e5a19530aea5cdb5712924245c863128cf65cedacad96ea9c44949"
PREFLIGHT_RECEIPT = "a25faf8081d3749346dab89a2a9cdd9cb5b1a4ceb8ec6e2dceba6ccd71dfd0b4"
COMPLETED_STATE = "93996d551eee2ecc2f95159abde63aa6828f6c2e9b97137d8e4d97ab4eda591f"
RESUMED_STATE = "b391abb257d9d073dd8d3f89ede3e360b404ef9e783f88b3d296295aaf9d913b"


def signed_apply_receipt(evidence, deploy, hashes, key):
    prefix = evidence.prefix(JOB, 1)

    def read(relative):
        return evidence.adapter.jenkins_get(evidence.JENKINS_URL, prefix + relative)

    build = evidence.decode(read("api/json?tree=number,queueId,result,building,actions[causes[_class,userId]]"))
    causes = [cause for action in build["actions"] for cause in action.get("causes", [])]
    evidence.require(build["number"] == 1 and build["queueId"] == 196 and build["result"] == "FAILURE"
                     and build["building"] is False and len(causes) == 1
                     and causes[0]["_class"] == "hudson.model.Cause$UserIdCause"
                     and causes[0]["userId"] == "claude-code-use"
                     and read("artifact/reviewed-library.txt").strip().decode("ascii") == APPLY_LIBRARY,
                     "owner115_apply_build_changed")
    verified = deploy.control.verify(
        evidence.envelope(read("artifact/verify-receipt.json"), PREFLIGHT_RECEIPT), key)
    applied = deploy.control.verify(
        evidence.envelope(read("artifact/recovery-receipt.json"), APPLY_RECEIPT), key)
    fields = ("job", "build", "recovery_library_revision", "transaction_id", "source_commit",
              "version", "digest", "original_evidence_sha256", "observed", "migrations", "retained_notification")
    evidence.require(verified["status"] == "VERIFIED" and verified["mode"] == "verify"
                     and verified["apply_claimed"] is False
                     and all(verified[name] == applied[name] for name in fields)
                     and applied["recovery_library_revision"] == APPLY_LIBRARY
                     and applied["original_evidence_sha256"] == hashes
                     and applied["mode"] == "apply" and applied["status"] == "FAILED"
                     and applied["code"] == "unexpected_final_binding"
                     and applied["apply_claimed"] is True and applied["native_binding_finished"] is True
                     and applied["app_binding"]["status"] == "completed",
                     "owner115_apply_receipt_not_completed")
    return applied


def observe_completed(binding, attachment, evidence, runtime_module, deploy, source, runtime, request,
                      failure, hashes, key, verify_closed):
    applied = signed_apply_receipt(evidence, deploy, hashes, key)
    tx = binding.AppBinding(request, runtime, -1, binding.NativeIO())
    try:
        tx.id = attachment.TRANSACTION
        tx.backup = binding.directory(tx.owner_directory / ("app-binding-" + tx.id))
        attachment.lock_existing(binding, tx, runtime / "data/locks/prod.lock")
        evidence.verify_persistent_failure(Path("/Users/surpend/Developer/jenkins/builds/release-state/deployment"),
                                           key, failure)
        claim = deploy.control.verify(binding.document(tx.owner_directory /
                                      "app-binding-incident-115-recovery.claim.json"), key)
        evidence.require(claim["transaction_id"] == tx.id and claim["job"] == JOB and claim["build"] == 1
                         and claim["container_id"] == attachment.CONTAINER
                         and claim["original_evidence_sha256"] == hashes,
                         "owner115_apply_claim_changed")
        completed_path = tx.backup / "completed.json"
        resumed_path = tx.backup / "owner-resumed.json"
        evidence.require(binding.sha(binding.read(completed_path)) == COMPLETED_STATE
                         and binding.sha(binding.read(resumed_path)) == RESUMED_STATE
                         and binding.document(completed_path) == applied["app_binding"]
                         and binding.sha(binding.read(tx.backup / "owner-upgrade.pending.committed.json")) == attachment.PENDING_SHA,
                         "owner115_original_completion_evidence_changed")
        tx.config = binding.document(tx.owner_directory / "owner.json")
        original = binding.document(tx.backup / "owner.json")
        old_target, = original["services"]["app"]["docker"]["targets"]
        commit = binding.document(tx.backup / "commit-authorized.json")
        live_files = {tx.owner_directory / name for name in ("owner.json", "registry.json", "installation.json")}
        live_files.add(tx.root / "consumer-admission.json")
        file_hashes = {str(path): binding.sha(binding.read(path)) for path in live_files}
        evidence.require(commit["transaction_id"] == tx.id and file_hashes == commit["after_sha256"]
                         and commit["observed"] == applied["observed"], "owner115_committed_binding_changed")
        tx.job_path = tx.resources / "consumer-jobs" / (tx.id + ".json")
        tx.owned_pending = [Path(path).with_name(Path(path).name.replace(".state.json", ".pending"))
                            for path in binding.document(tx.backup / "authorized.json")["state_pins"]]
        new_key = applied["app_binding"]["new_app_key"]
        new_prefix = tx.prefix(new_key)
        tx.owned_pending.append(new_prefix.with_suffix(".pending"))
        current_intent = binding.document(new_prefix.with_suffix(".state.json"))
        evidence.require(current_intent["resource_key"] == new_key and current_intent["desired_state"] == "running",
                         "owner115_app_intent_changed")
        live_owner = verify_closed(binding, tx, applied["observed"])
        evidence.require(live_owner == binding.document(resumed_path)["owner_after"],
                         "owner115_resumed_identity_changed")
        snapshot = binding.read(tx.backup / "operations-after-pause.jsonl")
        evidence.require(binding.read(tx.journal).startswith(snapshot), "owner115_historical_journal_changed")
        env = binding.read(runtime / ".env.prod")
        observed_image = [line.strip() for line in env.splitlines() if runtime_module.IMAGE_ASSIGNMENT.match(line)]
        journal = runtime / "data/logs/prod-deploy-journal.log"
        activation = applied["activation_persistence"]
        evidence.require(observed_image == [b"APP_IMAGE=" + request["image"].encode("ascii")]
                         and activation["image"] == request["image"] and activation["app_deploy_journal_completed"] is True
                         and activation["deploy_journal"] == str(journal)
                         and activation["deploy_journal_sha256"] == binding.sha(binding.read(journal, mode=0o644)),
                         "owner115_activation_record_changed")
        observed = runtime_module.observe_app(deploy, request, runtime)
        migrations = runtime_module.verify_migration_evidence(runtime, failure)
        redis = runtime_module.verify_retained_redis(source, runtime, request, observed, old_target)
        evidence.require(observed == applied["observed"] and migrations == applied["migrations"]
                         and redis == applied["retained_notification"]
                         and not os.path.lexists(tx.pending), "owner115_current_runtime_changed")
        return {"native_binding_finished": True, "binding_committed_by_build": 1, "closure_only": True,
                "apply_receipt_sha256": APPLY_RECEIPT, "observed": observed, "migrations": migrations,
                "retained_notification": redis, "activation_persistence": activation,
                "app_binding": applied["app_binding"], "resident_owner_pid": live_owner["pid"]}
    finally:
        tx.close()
