"""Read-only closure proof for the already released #101 binding transaction."""

import importlib.util
import os
from pathlib import Path
import re
import stat
import time


_spec = importlib.util.spec_from_file_location("postdeploy_close_state", Path(__file__).with_name("release-app-binding-postdeploy-state.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)
B = S.B
need = B.require
CLAIM = "postdeploy-recovery-101"
CONTINUATION = "after-app-management"
PENDING_ID = re.compile(rb"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\n\Z")


def _private_directory(path):
    path = B.directory(path)
    info = path.lstat()
    need(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
         "postdeploy_close_private_checkpoint_required")
    return path


def _config_hashes(configurations):
    return {str(path): B.sha(raw) for path, raw in configurations.items()}


def _no_original_barriers(tx):
    # Normal owner work may reclaim a released resource path. The original
    # transaction's marker contents, not the name alone, identify this incident.
    need(not list(tx.owner_directory.glob("*.pending.json"))
         and not os.path.lexists(tx.resources / "consumer-jobs" / (tx.id + ".json")),
         "postdeploy_close_original_admission_present")
    original = (tx.id + "\n").encode()
    for path in tx.resources.glob("*.pending"):
        for attempt in range(5):
            try:
                raw = B.read(path)
            except FileNotFoundError:
                break  # The running owner may have finished its own operation.
            except B.BindingError as error:
                if str(error) != "file_changed" or attempt == 4:
                    raise
            else:
                need(raw != original, "postdeploy_close_original_resource_fence_present")
                if raw:
                    need(PENDING_ID.fullmatch(raw), "postdeploy_close_unknown_resource_fence")
                    break
                need(attempt < 4, "postdeploy_close_unknown_resource_fence")
            # The owner creates a new mode-0600 file before writing its ID.
            time.sleep(0.02)


def _journal_prefix(checkpoint):
    tx = checkpoint.transaction
    before = B.read(tx.backup / "operations-before-pause.jsonl")
    after = B.read(tx.backup / "operations-after-pause.jsonl")
    need(after.startswith(before), "postdeploy_close_historical_journal_changed")
    B.journal_idle(before)
    B.journal_idle(after)
    for attempt in range(5):
        try:
            identity, raw = checkpoint._snapshot(tx.journal)
        except B.BindingError as error:
            if str(error) not in {"file_changed", "postdeploy_file_changed"} or attempt == 4:
                raise
            time.sleep(0.02)
        else:
            need(raw.startswith(after), "postdeploy_close_historical_journal_changed")
            return identity[:2]
    raise B.BindingError("postdeploy_close_journal_unconfirmed")


def verify(runtime, request, original_failure, plan, applied, io):
    """Return the exact live binding after proving continuity without mutations."""
    runtime = B.directory(Path(runtime))
    # Reuse the original transaction's config, artifact and Docker validators
    # without taking locks against the already-running native owner.
    checkpoint = S.Checkpoint(runtime, runtime, request, original_failure, io)
    tx = checkpoint.transaction = B.AppBinding(request, runtime, None, io)
    tx.id = S.TRANSACTION
    tx.backup = _private_directory(tx.owner_directory / ("app-binding-" + tx.id))
    parent = _private_directory(tx.backup / CLAIM)
    attempt = _private_directory(parent / CONTINUATION)
    failed_binding = original_failure["app_binding"]
    need(all(value.get("transaction_id") == tx.id for value in (failed_binding, plan, applied))
         and failed_binding.get("backup") == str(tx.backup)
         and failed_binding.get("phase") == "committed-installation.json"
         and failed_binding.get("code") == "owner_resume_unconfirmed",
         "postdeploy_close_incident_identity_changed")
    for receipt in (request, original_failure, applied):
        need(receipt.get("commit") == S.COMMIT and receipt.get("version") == S.VERSION
             and receipt.get("digest") == S.DIGEST, "postdeploy_close_release_identity_changed")
    committed = B.document(tx.backup / "commit-authorized.json")
    authorized = B.document(tx.backup / "authorized.json")
    need(committed["transaction_id"] == authorized["transaction_id"] == tx.id
         and plan["app"] == applied["app"] == committed["observed"], "postdeploy_close_runtime_evidence_changed")
    checkpoint.observed = committed["observed"]

    configurations = {checkpoint._config_path(name): B.read(checkpoint._config_path(name)) for name in S.CONFIGS}
    checkpoint.configs = {path: B.read(attempt / ("before-" + path.name)) for path in configurations}
    need(_config_hashes(checkpoint.configs) == plan["before"] == committed["after_sha256"]
         and _config_hashes(configurations) == plan["after"]
         and all(B.read(parent / ("before-" + path.name)) == raw for path, raw in checkpoint.configs.items()),
         "postdeploy_close_configuration_evidence_changed")
    need(B.read(attempt / "candidate-owner.json") == configurations[checkpoint._config_path("owner.json")],
         "postdeploy_close_candidate_owner_changed")
    checkpoint._check_repin_scope(configurations)
    checkpoint._set_configs(configurations)

    initial_owner = B.document(tx.backup / "owner.json")
    initial_consumer = B.document(tx.backup / "consumer-admission.json")
    initial_app = initial_owner["services"]["app"]
    tx.old_target = initial_app["docker"]["targets"][0]
    tx.old_key = B.resource_keys(initial_app)[0]
    need((tx.old_target["id"], tx.old_target["image_id"]) ==
         (original_failure["previous_container_id"], original_failure["previous_image_id"]),
         "postdeploy_close_previous_app_changed")
    tx.expected_image_id = checkpoint.observed["image_id"]
    tx.qualify_observed(checkpoint.observed)
    app = tx.config["services"]["app"]
    checkpoint.engine_id = app["docker"]["engine_id"]
    need(app["docker"]["targets"] == [tx.target_for(checkpoint.observed["container_id"])],
         "postdeploy_close_bound_app_changed")
    tx.new_key = B.resource_keys(app)[0]

    keys = set(initial_consumer["consumers"][B.CONSUMER]["resource_keys"])
    for service in initial_owner["services"].values():
        keys.update(B.resource_keys(service))
    need(len(keys) == 8 and tx.new_key not in keys, "postdeploy_close_original_resource_scope_changed")
    planned = sorted(str(tx.prefix(key).with_suffix(".pending")) for key in keys | {tx.new_key})
    need(sorted(applied["released_fences"]) == planned, "postdeploy_close_resource_release_evidence_changed")
    need(set(authorized["state_pins"]) == {str(tx.prefix(key).with_suffix(".state.json")) for key in keys},
         "postdeploy_close_original_state_scope_changed")

    historical = B.read(tx.backup / "owner-upgrade.pending.committed.json")
    archive = attempt / "startup-pending.archived.json"
    need(B.read(archive) == historical, "postdeploy_close_startup_archive_changed")
    marker = B.json.loads(historical, object_pairs_hook=B.pairs)
    need(marker["schema_version"] == 1 and marker["kind"] == "active-app-binding"
         and marker["transaction_id"] == tx.id and marker["backup"] == str(tx.backup)
         and marker["old_app_key"] == tx.old_key and marker["request_sha256"] == B.sha(B.encoded(request)),
         "postdeploy_close_startup_archive_identity_changed")

    checkpoint._load_protected(authorized)
    need(tx.config["journal_file"] == str(tx.journal)
         and tx.config["resource_lock_directory"] == tx.consumers["resource_lock_directory"] == str(tx.resources),
         "postdeploy_close_owner_locations_changed")
    expected = applied["owner"]
    need(type(expected["pid"]) is int and expected["pid"] > 0 and expected["pid"] != tx.owner_before["pid"]
         and expected["kernel_path"] == tx.owner_before["kernel_path"]
         and expected["binary_sha256"] == tx.owner_before["binary_sha256"]
         and expected["uid_start"].split(maxsplit=1)[0] == str(os.getuid()),
         "postdeploy_close_owner_provenance_changed")
    io.prove_running(tx, expected)
    journal_inode = _journal_prefix(checkpoint)
    _no_original_barriers(tx)
    checkpoint._assert_app()
    need(all(B.read(path) == raw for path, raw in configurations.items()) and tx.source_pins() == tx.pins
         and B.read(archive) == historical and _journal_prefix(checkpoint) == journal_inode,
         "postdeploy_close_identity_changed_during_verification")
    _no_original_barriers(tx)
    io.prove_running(tx, expected)
    return tx
