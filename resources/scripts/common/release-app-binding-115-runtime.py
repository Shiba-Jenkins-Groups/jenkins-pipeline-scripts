#!/usr/bin/env python3
"""Verify and close the runtime portion of App owner build #115.

This module is called under the incident owner's recovered locks. It never
starts a container, removes a volume, opens SQLite, or reruns a migration.
"""
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import types


spec = importlib.util.spec_from_file_location("binding_115_files", Path(__file__).with_name("release-app-binding.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)
need = B.require

OWNER_BUILD = 115
TRANSACTION_ID = "ad99ceab78822c71b8fe99bebb52dd8a"
ENGINE_ID = "9f2f05b6-4637-45c7-ab13-cf5a35e2a539"
COMMIT = "84c2b0a6eae1fb74944af0083859450d321dbcbd"
VERSION = "1.1.39"
DIGEST = "sha256:8a83cd3ea683caf29e3c44952061970618e53c2fdb1da595e85f5393eb9fde89"
REPOSITORY = "localhost:9290/shiba-go-ditch-api-project/prod/"
IMAGE = REPOSITORY + VERSION + "@" + DIGEST
IMAGE_ID = "sha256:861ae9baf79fa6f8021da642143ed720dc9370dc23239fda99ad6f59395acbaf"
CONTAINER_ID = "4aee6e553ba3a886e2772240f23d1c2067b9f51b7571150b82f1dba9967606b7"
PREVIOUS_IMAGE = REPOSITORY + "1.1.34@sha256:1673f789cdcba68446bd75d9b1e180c45be9c442b49c77d3bc50fa333dbd117f"
PREVIOUS_IMAGE_ID = "sha256:856bad5166318ae0b7f5326e0bf478eabea4c5ef65af73651bacd806a95f7596"
PREVIOUS_CONTAINER_ID = "1828f5def8c53dcf6797fa5f112cb6260e4db968e418f74434a04c14cf851ca8"
PROJECT = "shiba-goditch-prod"
MIGRATION = "migration-20261125-110023"
MIGRATION_APPLY_SHA = "0b8db031c2f91e649d68ae91147218135b00a7429fdbf97b1c9b9fb46d0f23f6"
MIGRATION_VERIFY_SHA = "1e1ce2f20c4d61900758c55780ed28b6a4cebf974d292bed58122928178b2ccc"
MIGRATION_BACKUP_SHA = "712c25a700b2865d161dc3499d2cfcfa13d9bbdf18354b226aa41074833df3c0"
MIGRATION_BACKUP_SIZE = 12005376
MIGRATION_FIELDS = {
    "database": "/data/shiba-go-ditch-api.db", "required_migration_id": 37,
    "migration_name": "20261116_local_openai_candidate", "integrity_check": "ok",
    "migration_checksum": "b494d964694ad2ab1cf5b559c99acd541b29a8229bb84f7cb35d4649b238fd0c",
    "schema_checksum": "618908aca0764149c6241600e28a4407ae00dad98464a7b44dc08c3947ea9d10",
    "foreign_key_violations": 0, "required_columns_present": True, "required_indexes_present": True,
    "required_tables_present": True,
}
REDIS_VERIFIER = "scripts/lib/app-redis812-readiness.py"
REDIS_SOURCE = {
    REDIS_VERIFIER: "d278d982e77b9825775dc925968c8263450bfb7a1e04df89950536ab87a50b28",
    "scripts/lib/app-redis812-evidence.py": "7a7100b090033d047ef6e0458a535771e1d001f01dea41d3b7d0d91c298dedbe",
    "scripts/lib/redis-compose-recovery.py": "2302f08244cce67bfcfcdfae5ac59d1730ec9b49b30bf38c5be004e6222dab4c",
    "docker-compose.notification.redis-812-openssl.prod.yml": "4fc1b1a83abbe65ed599e71a503bb8ddf200dcd3a2e64ce2c09ae89164ae8fdf",
}
IMAGE_ASSIGNMENT = re.compile(rb"[ \t]*(?:export[ \t]+)?APP_IMAGE[ \t]*=")


def same_fields(document, expected):
    return all(key in document and type(document[key]) is type(value) and document[key] == value
               for key, value in expected.items())


def qualify_request(request):
    need(same_fields(request, {"commit": COMMIT, "version": VERSION, "digest": DIGEST, "image": IMAGE}),
         "owner115_target_release_changed")


def verify_backup(path):
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    B.directory(path.parent)
    before = path.lstat()
    need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and before.st_nlink == 1
         and stat.S_IMODE(before.st_mode) == 0o600 and before.st_size == MIGRATION_BACKUP_SIZE,
         "owner115_original_migration_backup_changed")
    result = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        need(identity(os.fstat(source.fileno())) == identity(before), "owner115_original_migration_backup_changed")
        for block in iter(lambda: source.read(1048576), b""):
            result.update(block)
        need(identity(os.fstat(source.fileno())) == identity(before) == identity(path.lstat())
             and result.hexdigest() == MIGRATION_BACKUP_SHA, "owner115_original_migration_backup_changed")


def verify_migration_evidence(runtime, failed):
    """Pin the original JSON receipts and hash the closed backup as opaque bytes."""
    runtime = B.directory(Path(runtime))
    directory = runtime / "data/backups/prod-predeploy"
    backup = directory / (MIGRATION + ".db")
    apply = directory / (MIGRATION + "-apply.json")
    verify = directory / (MIGRATION + "-verify.json")
    need(same_fields(failed, {"kind": "runtime-deployment", "status": "FAILED", "commit": COMMIT,
                             "version": VERSION, "digest": DIGEST, "previous_container_id": PREVIOUS_CONTAINER_ID,
                             "previous_image_id": PREVIOUS_IMAGE_ID, "mutation_may_have_started": True,
                             "backup_directory": str(directory),
                             "deploy_journal": str(runtime / "data/logs/prod-deploy-journal.log")})
         and sorted(failed.get("new_backup_and_migration_files", [])) == sorted(map(str, (apply, verify, backup))),
         "owner115_failed_receipt_migration_scope_changed")
    apply_raw, verify_raw = B.read(apply), B.read(verify)
    need(B.sha(apply_raw) == MIGRATION_APPLY_SHA and B.sha(verify_raw) == MIGRATION_VERIFY_SHA,
         "owner115_migration_receipt_bytes_changed")
    expected_apply = {**MIGRATION_FIELDS, "applied": True, "backup_path": "/backup/" + backup.name,
                      "backup_sha256": MIGRATION_BACKUP_SHA}
    apply_receipt, verify_receipt = (json.loads(raw, object_pairs_hook=B.pairs) for raw in (apply_raw, verify_raw))
    need(same_fields(apply_receipt, expected_apply) and same_fields(verify_receipt, {**MIGRATION_FIELDS, "applied": False})
         and "backup_path" not in verify_receipt and "backup_sha256" not in verify_receipt,
         "owner115_migration_result_changed")
    verify_backup(backup)
    return {"required_migration_id": MIGRATION_FIELDS["required_migration_id"],
            "migration_checksum": MIGRATION_FIELDS["migration_checksum"],
            "schema_checksum": MIGRATION_FIELDS["schema_checksum"],
            "apply_sha256": MIGRATION_APPLY_SHA, "verify_sha256": MIGRATION_VERIFY_SHA,
            "backup_path": str(backup), "backup_sha256": MIGRATION_BACKUP_SHA, "migration_rerun": False}


def observe_app(deploy, request, runtime):
    """Use the original deployment final identity and pin the existing CID."""
    qualify_request(request)
    need(deploy.run(["docker", "info", "--format", "{{.ID}}"]) == ENGINE_ID,
         "owner115_docker_engine_changed")
    deploy.no_host_writer(runtime)
    need(deploy.image_identity(request) == IMAGE_ID, "owner115_image_id_changed")
    observed = deploy.final_identity(request, IMAGE_ID, runtime)
    need(same_fields(observed, {"container_id": CONTAINER_ID, "image_id": IMAGE_ID, "commit": COMMIT,
                               "version": VERSION, "digest": DIGEST, "heartbeat": 200, "compose_project": PROJECT}),
         "owner115_running_app_identity_changed")
    return observed


def verify_retained_redis(source, runtime, request, observed, previous_target):
    """Run the exact original post-activation check after removal of the stale volume."""
    qualify_request(request)
    need(same_fields(observed, {"container_id": CONTAINER_ID, "image_id": IMAGE_ID,
                               "commit": COMMIT, "version": VERSION, "digest": DIGEST})
         and same_fields(previous_target, {"id": PREVIOUS_CONTAINER_ID, "image_id": PREVIOUS_IMAGE_ID}),
         "owner115_activation_redis_scope_changed")
    source = B.directory(Path(source))
    sources = {name: B.read(source / name, mode=0o644) for name in REDIS_SOURCE}
    need(all(B.sha(sources[name]) == digest for name, digest in REDIS_SOURCE.items()),
         "owner115_redis_verifier_source_changed")
    module = types.ModuleType("owner115_original_redis_verifier")
    module.__file__ = str(source / REDIS_VERIFIER)
    exec(compile(sources[REDIS_VERIFIER], module.__file__, "exec"), module.__dict__)
    try:
        evidence = module.read_evidence(runtime)
        need(evidence["engine_id"] == ENGINE_ID and evidence["app_id"] == PREVIOUS_CONTAINER_ID
             and B.sha(sources[module.OVERLAY]) == evidence["redis_overlay_sha256"],
             "owner115_redis_cutover_scope_changed")
        module.verify_runtime(runtime, evidence, "activated", CONTAINER_ID, IMAGE, COMMIT, previous_target)
    except module.Blocked as exc:
        raise B.BindingError("owner115_redis_activated_check_failed: " + str(exc)) from exc
    return {"phase": "activated", "redis_id": evidence["redis_id"], "redis_volume": evidence["redis_volume"],
            "rollback_redis_id": evidence["source_812_redis_id"], "handoff_sha256": evidence["handoff_sha256"]}


def activation_inputs(runtime, request, previous_ref):
    qualify_request(request)
    need(previous_ref == PREVIOUS_IMAGE, "owner115_previous_image_changed")
    runtime = B.directory(Path(runtime))
    env_path = runtime / ".env.prod"
    journal_path = runtime / "data/logs/prod-deploy-journal.log"
    old_env, journal = B.read(env_path), B.read(journal_path, mode=0o644)
    old_assignment = b"APP_IMAGE=" + PREVIOUS_IMAGE.encode("ascii") + b"\n"
    new_assignment = b"APP_IMAGE=" + IMAGE.encode("ascii") + b"\n"
    lines = old_env.splitlines(keepends=True)
    need([line for line in lines if IMAGE_ASSIGNMENT.match(line)] == [old_assignment],
         "owner115_persisted_image_predecessor_changed")
    directory = runtime / "data/backups/prod-predeploy"
    backup, verify = directory / (MIGRATION + ".db"), directory / (MIGRATION + "-verify.json")
    expected_tail = (
        f"2026-11-25T11:00:11+0800\tDEPLOY-BEGIN prev={PREVIOUS_IMAGE} new={IMAGE}\n"
        f"2026-11-25T11:00:24+0800\tMIGRATION-OK commit={COMMIT} backup={backup} evidence={verify}\n"
    ).encode("utf-8")
    need(journal.endswith(expected_tail), "owner115_deploy_journal_tail_changed")
    new_env = b"".join(new_assignment if line == old_assignment else line for line in lines)
    return env_path, old_env, new_env, journal_path, journal


def verify_activation_inputs(runtime, request, previous_ref):
    """Qualify the existing image assignment and journal before the apply claim."""
    activation_inputs(runtime, request, previous_ref)


def persist_completed_activation(runtime, request, previous_ref, transaction_id):
    """Write the proven image ref and append completion to the original App journal."""
    need(transaction_id == TRANSACTION_ID, "owner115_activation_transaction_changed")
    env_path, original_env, expected_env, journal_path, original_journal = activation_inputs(
        runtime, request, previous_ref)
    fd = os.open(journal_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    try:
        held, current = os.fstat(fd), journal_path.lstat()
        need(stat.S_ISREG(held.st_mode) and held.st_uid == os.getuid() and stat.S_IMODE(held.st_mode) == 0o644
             and (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)
             and B.read(journal_path, mode=0o644) == original_journal, "owner115_deploy_journal_changed")
        # Rewrite only the immutable APP_IMAGE assignment, with the original
        # credentials kept in owner-private storage and out of the receipt.
        need(B.read(env_path) == original_env, "owner115_persisted_image_predecessor_changed")
        B.replace(env_path, expected_env)
        need(B.read(env_path) == expected_env, "owner115_persisted_image_unconfirmed")
        stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        suffix = (f"{stamp}\tDEPLOY-RECOVERED owner_build={OWNER_BUILD} transaction={transaction_id} image={IMAGE}\n"
                  f"{stamp}\tDEPLOY-OK   {IMAGE}\n").encode("ascii")
        need(os.write(fd, suffix) == len(suffix), "owner115_deploy_journal_append_incomplete")
        os.fsync(fd)
        need(B.read(journal_path, mode=0o644) == original_journal + suffix,
             "owner115_deploy_journal_append_unconfirmed")
    finally:
        os.close(fd)
    return {"image": IMAGE, "previous_image": PREVIOUS_IMAGE, "deploy_journal": str(journal_path),
            "deploy_journal_sha256": B.sha(original_journal + suffix), "app_deploy_journal_completed": True}
