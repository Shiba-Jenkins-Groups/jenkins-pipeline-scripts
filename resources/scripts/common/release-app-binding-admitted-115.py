"""Attach to the original private #115 deployment transaction without replaying it."""

import json
import os
from pathlib import Path
import stat


TRANSACTION = "ad99ceab78822c71b8fe99bebb52dd8a"
VERSION = "1.1.39"
COMMIT = "84c2b0a6eae1fb74944af0083859450d321dbcbd"
DIGEST = "sha256:8a83cd3ea683caf29e3c44952061970618e53c2fdb1da595e85f5393eb9fde89"
IMAGE = "localhost:9290/shiba-go-ditch-api-project/prod/" + VERSION + "@" + DIGEST
IMAGE_ID = "sha256:861ae9baf79fa6f8021da642143ed720dc9370dc23239fda99ad6f59395acbaf"
OLD_IMAGE_ID = "sha256:856bad5166318ae0b7f5326e0bf478eabea4c5ef65af73651bacd806a95f7596"
CONTAINER = "4aee6e553ba3a886e2772240f23d1c2067b9f51b7571150b82f1dba9967606b7"
OLD_CONTAINER = "1828f5def8c53dcf6797fa5f112cb6260e4db968e418f74434a04c14cf851ca8"
ENGINE = "9f2f05b6-4637-45c7-ab13-cf5a35e2a539"
LIBRARY = "6b799683623af09719aecf11d8f37929c04956ee"
REQUEST_SHA = "7c70fa604e37979ef9241abbd78c7f5e19420f034467b401ee2c471429681705"
REQUEST_PAYLOAD_SHA = "dbe45501af1cbea6e9ceeab293a38468d871b64a0a5051df45d52325214b41b9"
FAILURE_PAYLOAD_SHA = "d365a07a52904e9eff347855346fd28353219fe89ab8f8b9e0b08266f3a2f9e4"
PENDING_SHA = "3aebabb04796f750167263f5e1d207506d0d20cea38d9cfdb8ce402cc94c100b"
JOB_SHA = "6dd75c33c1a80d67e84d7bed4cac1c39f8c986c79c8a6432a627e3c755fac105"
BACKUP_SHA = "524f7aeb2e3b365a2835855c65db7286854dedd066a8fd44f11a7216c67f39e9"
ORIGINALS = ("owner.json", "registry.json", "installation.json")
BACKUP_FILES = set(ORIGINALS) | {
    "authorized.json", "owner-paused.json", "deploy-admitted.json",
    "consumer-admission.json", "owner.plist",
    "operations-before-pause.jsonl", "operations-after-pause.jsonl",
} | {"owner-pause-%04d.json" % n for n in range(1, 10)}


def lock_existing(binding, transaction, path):
    fd = transaction.lock(path)
    linked, held = path.lstat(), os.fstat(fd)
    binding.require(stat.S_ISREG(linked.st_mode)
                    and (linked.st_dev, linked.st_ino) == (held.st_dev, held.st_ino),
                    "incident115_lock_identity_changed")
    return fd


def checkpoint_bytes(binding, transaction):
    backup = binding.directory(transaction.backup)
    states = binding.directory(backup / "resource-states")
    binding.require(all(stat.S_IMODE(p.lstat().st_mode) == 0o700 for p in (backup, states))
                    and {p.name for p in backup.iterdir()} == BACKUP_FILES | {"resource-states"},
                    "incident115_backup_progress_or_foreign_files")
    files = {name: binding.read(backup / name) for name in BACKUP_FILES}
    state_files = list(states.iterdir())
    binding.require(len(state_files) == 8, "incident115_backup_state_scope_changed")
    files.update({str(p.relative_to(backup)): binding.read(p) for p in state_files})
    binding.require(binding.sha(binding.encoded({name: binding.sha(data) for name, data in files.items()})) == BACKUP_SHA,
                    "incident115_original_backup_changed")
    return files


def present(binding, transaction):
    """Recheck before the one-shot caller writes its independently owned claim."""
    binding.require(transaction.phase == "deploy-admitted" and not transaction.completed,
                    "incident115_expects_deploy_admitted")
    checkpoint_bytes(binding, transaction)
    transaction.assert_sources()
    binding.require(binding.read(transaction.pending) == transaction.pending_raw
                    and binding.read(transaction.job_path) == transaction.job_raw
                    and all(binding.read(p) == (transaction.id + "\n").encode() for p in transaction.owned_pending),
                    "incident115_original_fence_or_job_changed")
    transaction.assert_pending()
    binding.require(binding.read(transaction.journal) == transaction.journal_raw,
                    "incident115_original_journal_changed")
    binding.journal_idle(transaction.journal_raw)
    binding.require(transaction.io.owner(transaction) is None, "incident115_owner_not_paused")
    ids = transaction.io.inventory_ids(transaction)
    binding.require(CONTAINER in ids and OLD_CONTAINER not in ids
                    and transaction.io.engine(transaction) == ENGINE
                    and transaction.io.image(transaction) == IMAGE_ID,
                    "incident115_runtime_identity_changed")
    current = transaction.io.app(transaction, CONTAINER)
    binding.require(current["Config"]["Image"] == IMAGE, "incident115_current_image_reference_changed")
    transaction.validate_target(current, transaction.target_for(CONTAINER), new=True)
    new_key = "docker:" + ENGINE + ":" + CONTAINER
    new_paths = [transaction.prefix(new_key).with_suffix(suffix) for suffix in (".state.json", ".pending")]
    binding.require(not any(path.exists() or path.is_symlink() for path in new_paths)
                    and all(new_key not in consumer["resource_keys"] for consumer in transaction.consumers["consumers"].values()),
                    "incident115_binding_already_progressed")


def attach(binding, request, failure, runtime, *, io=None):
    """Return the pinned original AppBinding, holding original EX locks.

    The caller verifies both Jenkins envelopes before passing their payloads.
    It owns the durable one-shot claim, retained Redis activation check, and
    any physical mutation. Always close the returned transaction in finally.
    """
    binding.require(binding.sha(binding.encoded(request)) == REQUEST_PAYLOAD_SHA
                    and binding.sha(binding.encoded(failure)) == FAILURE_PAYLOAD_SHA
                    and all(request.get(k) == v for k, v in {
                        "schema_version": 2, "kind": "deployment", "prod_build": 238, "library_revision": LIBRARY,
                        "commit": COMMIT, "deployment_script_revision": COMMIT, "version": VERSION,
                        "image": IMAGE, "digest": DIGEST,
                    }.items())
                    and failure.get("request_sha256") == REQUEST_SHA
                    and failure.get("schema_version") == 1 and failure.get("kind") == "runtime-deployment"
                    and failure.get("status") == "FAILED" and failure.get("mutation_may_have_started") is True
                    and failure.get("previous_container_id") == OLD_CONTAINER and failure.get("previous_image_id") == OLD_IMAGE_ID,
                    "incident115_signed_identity_changed")
    tx = binding.AppBinding(request, runtime, -1, io or binding.NativeIO())
    try:
        binding.directory(tx.runtime)
        binding.directory(tx.root)
        binding.require(stat.S_IMODE(tx.root.stat().st_mode) == 0o700, "incident115_private_root_required")
        tx.lifecycle_fd = lock_existing(binding, tx, tx.runtime / "data/locks/prod.lock")
        lock_existing(binding, tx, tx.owner_directory / "owner-upgrade.lock")
        lock_existing(binding, tx, tx.root / "consumer-helper-upgrade.lock")
        tx.id = TRANSACTION
        tx.backup = tx.owner_directory / ("app-binding-" + tx.id)
        tx.pending_raw = binding.read(tx.pending)
        binding.require(binding.sha(tx.pending_raw) == PENDING_SHA, "incident115_original_installation_marker_changed")
        pending = json.loads(tx.pending_raw, object_pairs_hook=binding.pairs)
        binding.require(pending["transaction_id"] == tx.id and pending["backup"] == str(tx.backup)
                        and pending["request_sha256"] == REQUEST_PAYLOAD_SHA,
                        "incident115_pending_request_mismatch")
        files = checkpoint_bytes(binding, tx)
        def parse(name):
            return json.loads(files[name], object_pairs_hook=binding.pairs)

        authorized = parse("authorized.json")
        last_pause = parse("owner-pause-0009.json")
        binding.require(all(parse(name)["transaction_id"] == tx.id
                            for name in ("authorized.json", "owner-paused.json", "deploy-admitted.json", "owner-pause-0009.json"))
                        and last_pause["bootout_acknowledged"] is True and last_pause["outcome"] == "launchd-not-loaded",
                        "incident115_original_pause_unconfirmed")
        tx.original = {name: files[name] for name in ORIGINALS}
        tx.global_raw = files["consumer-admission.json"]
        tx.config, tx.registry, tx.installed = (parse(name) for name in ORIGINALS)
        tx.consumers = parse("consumer-admission.json")
        tx.pins, tx.states, tx.owner_before, tx.dependencies = (
            authorized[key] for key in ("pins", "state_pins", "owner_before", "dependencies"))
        installed = tx.installed["identity"]
        tx.release = tx.root / "releases" / (installed["version"] + "-" + installed["source_commit"])
        tx.manifest = binding.document(tx.release / "manifest.json")
        tx.artifact_modes = {name: stat.S_IMODE((tx.release / name).lstat().st_mode) for name in tx.manifest["files"]}
        binding.require(tx.installed["state"] == "ready" and installed["environment"] == tx.config["environment"] == "prod"
                        and installed["owner_uid"] == os.getuid()
                        and binding.sha(binding.encoded(tx.manifest)) == installed["artifact_digest"]
                        and tx.owner_before["binary_sha256"] == tx.manifest["files"]["owner-darwin-arm64"]
                        and tx.owner_before["kernel_path"] == tx.config["executable"] == str(tx.release / "owner-darwin-arm64")
                        and tx.installed["owner_config_sha256"] == binding.sha(tx.original["owner.json"])
                        and installed["registry_digest"] == binding.sha(binding.encoded(tx.registry))
                        and installed["consumer_config_digest"] == binding.sha(binding.encoded(tx.consumers)),
                        "incident115_bound_owner_artifact_or_config_changed")
        app = tx.config["services"]["app"]
        [tx.old_target] = app["docker"]["targets"]
        [tx.old_key] = binding.resource_keys(app)
        binding.require(app["actions"] == ["status"] and app["docker"]["engine_id"] == ENGINE
                        and tx.old_target["id"] == OLD_CONTAINER and tx.old_target["image_id"] == OLD_IMAGE_ID
                        and pending["old_app_key"] == tx.old_key
                        and tx.registry["services"]["app"]["docker"] == app["docker"]
                        and tx.registry["services"]["app"]["revision"] == app["revision"],
                        "incident115_original_app_binding_changed")
        consumer = tx.consumers["consumers"][binding.CONSUMER]
        tx.scope = sorted(consumer["resource_keys"])
        tx.keys = sorted(set(tx.scope) | {key for service in tx.config["services"].values() for key in binding.resource_keys(service)})
        binding.require(len(tx.scope) == 6 and len(tx.keys) == 8 and tx.old_key in tx.scope
                        and not consumer.get("shared_lifecycle_locks")
                        and set(tx.states) == {str(tx.prefix(key).with_suffix(".state.json")) for key in tx.keys},
                        "incident115_original_resource_scope_changed")
        for key in tx.keys:
            prefix = tx.prefix(key)
            tx.fds[key] = lock_existing(binding, tx, prefix.with_suffix(".lock"))
            tx.owned_pending.append(prefix.with_suffix(".pending"))
            state = prefix.with_suffix(".state.json")
            binding.require(binding.sha(files["resource-states/" + state.name]) == tx.states[str(state)],
                            "incident115_original_resource_intent_changed")
        tx.job_path = binding.directory(tx.resources / "consumer-jobs") / (tx.id + ".json")
        tx.job_raw = binding.read(tx.job_path)
        binding.require(binding.sha(tx.job_raw) == JOB_SHA, "incident115_original_consumer_job_changed")
        tx.job = json.loads(tx.job_raw, object_pairs_hook=binding.pairs)
        binding.require(tx.job["job_id"] == tx.id and tx.job["consumer_id"] == binding.CONSUMER
                        and tx.job["resource_keys"] == tx.scope, "incident115_original_consumer_scope_changed")
        tx.journal_fd = lock_existing(binding, tx, tx.journal)
        tx.journal_raw = files["operations-after-pause.jsonl"]
        binding.require(tx.journal_raw == files["operations-before-pause.jsonl"], "incident115_original_journal_progressed")
        tx.expected_image_id = IMAGE_ID
        tx.owner_paused = tx.owner_pause_requested = True
        tx.phase = "deploy-admitted"
        present(binding, tx)
        return tx
    except BaseException:
        tx.close()
        raise
