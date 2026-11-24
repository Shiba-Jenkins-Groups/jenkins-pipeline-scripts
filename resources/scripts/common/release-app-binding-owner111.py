#!/usr/bin/env python3
"""Recover the exact App owner #111 admission after the unchanged App is verified.

Run in the managed owner job with a private fixed workspace and the release
artifact verification key. Plan performs read-only observation of managed
resources. Apply resumes only the existing native owner. It never deploys an
App, changes dependency lifecycles, or opens SQLite.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def load(file, module):
    spec = importlib.util.spec_from_file_location(module, Path(__file__).with_name(file))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


I = load("release-app-binding-incident.py", "owner111_incident_contract")
control = load("release-promotion.py", "owner111_signature_contract")
B, need = I.B, I.need
TX = "2d6c64221b9291e53d6a711e9ddac46b"
ORIGINAL_LIBRARY = "6b799683623af09719aecf11d8f37929c04956ee"
FAILED_COMMIT = "1e61bdc952b4c5330eb726a83fa6483a04503794"
FAILED_SOURCE = "89d0883ae8772fbd0e193abcbb40bdcc837e44f6"
GOOD_COMMIT = "db4261d0397cae3f11d0f1602cfed065689c5237"
FAILED_DIGEST = "sha256:7c8f061848243a6ebdb80f568e14a94bdc727cb202bb7242dfd2b356cba50051"
GOOD_DIGEST = "sha256:1673f789cdcba68446bd75d9b1e180c45be9c442b49c77d3bc50fa333dbd117f"
OLD_CONTAINER = "1828f5def8c53dcf6797fa5f112cb6260e4db968e418f74434a04c14cf851ca8"
OLD_IMAGE = "sha256:856bad5166318ae0b7f5326e0bf478eabea4c5ef65af73651bacd806a95f7596"
OLD_STARTED_AT = "2026-11-24T11:41:36.482606301Z"
OLD_RESTART_COUNT = 1
HASHES = {
    "failed_request": "eeaf44292cf8e74286489b27a9ec67e7a5a49bd0f24b5125014b2b79a02e2fb4",
    "failed_receipt": "d884540a41b4cf26f0a8a38d9daae3404c85c52352791ad000dd842f3629958f",
    "last_good_receipt": "ec88c89847fea3d9fc04948cbf5721f1b819dccb57f719e5d0f876d09decf8bf",
}
PRODUCT = control.gate.PRODUCT
SOURCE_FILES = (*I.SOURCE_FILES, Path(__file__).name, "release-promotion.py", "release-gate.py")
PAUSE_STEPS = (
    ("pre-pause", "identity-observed", False),
    ("bootout-requested", "request-durable", False),
    ("bootout-acknowledged", "command-succeeded", True),
    ("post-bootout", "launchd-not-loaded", True),
)
PAUSE_FILES = tuple("owner-pause-%04d.json" % n for n in range(1, len(PAUSE_STEPS) + 1))
PHASE_FILES = {"operations-after-pause.jsonl", "owner-paused.json", "deploy-admitted.json", *PAUSE_FILES}


def source_identity(revision):
    return I.source_identity(revision, SOURCE_FILES)


def verify_evidence(inputs, key):
    need(set(inputs) == set(HASHES), "owner111_exact_evidence_required")
    payloads = {}
    for name, raw in inputs.items():
        need(B.sha(raw) == HASHES[name], "owner111_archive_hash_changed")
        envelope = I.parse(raw)
        need(set(envelope) == {"payload", "signature"}, "owner111_invalid_signed_envelope")
        payloads[name] = control.verify(envelope, key)
    request = payloads["failed_request"]
    failed = payloads["failed_receipt"]
    good = payloads["last_good_receipt"]
    for value, kind, schema, commit, version, digest in (
        (request, "deployment", 2, FAILED_COMMIT, "1.1.35", FAILED_DIGEST),
        (failed, "runtime-deployment", 1, FAILED_COMMIT, "1.1.35", FAILED_DIGEST),
        (good, "runtime-deployment", 1, GOOD_COMMIT, "1.1.34", GOOD_DIGEST),
    ):
        expected = (kind, schema, PRODUCT, commit, version, digest, ORIGINAL_LIBRARY, commit)
        fields = ("kind", "schema_version", "product", "commit", "version", "digest",
                  "library_revision", "deployment_script_revision")
        need(tuple(value.get(field) for field in fields) == expected, "owner111_signed_release_identity_changed")
    promotion = control.verify(request["promotion"], key)
    need(request.get("prod_job") == PRODUCT + "/prod" and request.get("prod_build") == 234
         and promotion.get("develop_build") == 400 and promotion.get("source_commit") == FAILED_SOURCE
         and promotion.get("merge_commit") == FAILED_COMMIT and "recovery" not in request,
         "owner111_signed_build_identity_changed")
    need(failed.get("status") == "FAILED" and failed.get("request_sha256") == HASHES["failed_request"]
         and failed.get("new_backup_and_migration_files") == [] and failed.get("automatic_rollback") is False
         and failed.get("mutation_may_have_started") is True and "app_binding" not in failed
         and failed.get("previous_container_id") == OLD_CONTAINER and failed.get("previous_image_id") == OLD_IMAGE,
         "owner111_failed_receipt_scope_changed")
    observed = good["observed"]
    need(good.get("status") == "SUCCESS" and good["app_binding"].get("status") == "completed"
         and all(observed.get(field) == good[field] for field in ("commit", "version", "digest"))
         and observed.get("compose_project") == "shiba-goditch-prod"
         and observed.get("container_id") == OLD_CONTAINER and observed.get("image_id") == OLD_IMAGE,
         "owner111_last_good_runtime_changed")
    timestamp = control.gate.timestamp
    failure_start, failure_end = (timestamp(failed[field]) for field in ("started_at", "finished_at"))
    need(timestamp(good["finished_at"]) < timestamp(request["created_at"]) <= failure_start
         < failure_end < timestamp(request["expires_at"]), "owner111_signed_timeline_changed")
    return request, failed, good


class Owner111Incident(I.Incident):
    claim_name = "owner111-admitted-recovery"
    backup_files = I.ORIGINAL_FILES | PHASE_FILES

    def __init__(self, metadata, metadata_sha, runtime, transaction_id, io, *, key):
        need(transaction_id == TX, "owner111_exact_transaction_required")
        super().__init__(metadata, metadata_sha, runtime, transaction_id, io)
        self.key = key

    def expected_consumer_jobs(self):
        return set() if str(self.job) in self.archived_inputs else {self.job}

    def verify_phase(self, authorized):
        t = self.transaction
        need({p.name for p in self.backup.iterdir()} == self.backup_files | {"resource-states"},
             "owner111_backup_phase_changed")
        artifact_paths = self.metadata["artifact_paths"]
        inputs = {name: self.remember(Path(path)) for name, path in artifact_paths.items()}
        self.request, failed, good = verify_evidence(inputs, self.key)
        self.failed_started = control.gate.timestamp(failed["started_at"])
        state = B.directory(Path(self.metadata["state_directory"]))
        need(stat.S_IMODE(state.stat().st_mode) == 0o700, "owner111_private_state_required")
        self.hold(state / "promotion.lock")
        need(self.remember(state / (FAILED_COMMIT + ".json")) == inputs["failed_receipt"]
             and self.remember(state / (GOOD_COMMIT + ".json")) == inputs["last_good_receipt"],
             "owner111_live_release_state_changed")
        need(good["app_binding"].get("new_app_key") == t.old_key
             and t.old_target.get("id") == OLD_CONTAINER and t.old_target.get("image_id") == OLD_IMAGE,
             "owner111_bound_old_runtime_changed")
        pending = I.parse(self.pending_raw)
        need(pending.get("request_sha256") == B.sha(B.encoded(self.request)), "owner111_pending_request_mismatch")
        consumer = t.consumers["consumers"][B.CONSUMER]
        scope = sorted(consumer["resource_keys"])
        need(t.old_key in scope and not consumer.get("shared_lifecycle_locks"), "owner111_consumer_scope_changed")
        self.job = t.resources / "consumer-jobs" / (self.id + ".json")
        job = I.parse(self.remember(self.job))
        need(job == {"schema_version": 1, "job_id": self.id, "consumer_id": B.CONSUMER,
                     "resource_keys": scope, "started_at": job.get("started_at")}, "owner111_consumer_checkpoint_changed")
        timestamp = control.gate.timestamp
        last = self.failed_started
        for value in (pending["started_at"], authorized["at"]):
            current = timestamp(value)
            need(current >= last, "owner111_pause_timeline_changed")
            last = current
        for name, (phase, outcome, acknowledged) in zip(PAUSE_FILES, PAUSE_STEPS):
            pause = I.parse(B.read(self.backup / name))
            owner_before = authorized["owner_before"]
            need(pause.get("schema_version") == 1 and pause.get("kind") == "owner-pause-observation"
                 and pause.get("transaction_id") == self.id and pause.get("phase") == phase and pause.get("outcome") == outcome
                 and pause.get("bootout_acknowledged") is acknowledged and pause.get("expected_pid") == owner_before["pid"]
                 and pause.get("pid") == (None if acknowledged else owner_before["pid"])
                 and pause.get("binary_sha256") == (None if acknowledged else owner_before["binary_sha256"])
                 and pause.get("errno") is None and pause.get("proc_pidpath_return") is None,
                 "owner111_original_pause_proof_changed")
            current = timestamp(pause["at"])
            need(current >= last, "owner111_pause_timeline_changed")
            last = current
        paused, admitted = (I.parse(B.read(self.backup / name)) for name in ("owner-paused.json", "deploy-admitted.json"))
        for event in (paused, admitted):
            need(event == {"transaction_id": self.id, "at": event.get("at")}, "owner111_phase_identity_changed")
        need(last <= timestamp(paused["at"]) <= timestamp(job["started_at"]) <= timestamp(admitted["at"])
             <= timestamp(failed["finished_at"]), "owner111_admission_timeline_changed")

    def verify_paused_journal(self):
        super().verify_paused_journal()
        need(self.journal_raw == B.read(self.backup / "operations-after-pause.jsonl"),
             "owner111_journal_changed_after_pause")

    def inspect_app(self):
        observed = super().inspect_app()
        timestamp = control.gate.timestamp
        need(observed["container_id"] == OLD_CONTAINER and observed["image_id"] == OLD_IMAGE
             and observed["started_at"] == OLD_STARTED_AT and observed["restart_count"] == OLD_RESTART_COUNT
             and timestamp(observed["started_at"]) < self.failed_started,
             "owner111_healthy_old_app_generation_required")
        return observed

    def after_owner_verified(self):
        self.phase = "archive-consumer-checkpoint"
        self.archive_input(self.job, self.claim / "consumer-admission.archived.json")
        self.recheck(resumed=True)


def incident_factory(key):
    return lambda *args: Owner111Incident(*args, key=key)


def prepare_plan(runtime, state_directory, paths, key, revision, metadata, collection, *, io=None, verify_source=source_identity):
    native = io or I.IncidentIO()
    root = B.directory(Path(native.root))
    backup = B.directory(root / "prod" / ("app-binding-" + TX))
    artifacts = {name: B.read(path) for name, path in paths.items()}
    verify_evidence(artifacts, key)
    recorded = {}
    for name in Owner111Incident.backup_files:
        raw = B.read(backup / name)
        recorded[name] = {"sha256": B.sha(raw), "bytes": len(raw)}
    value = {"transaction_id": TX, "backup": str(backup), "authorized": I.parse(B.read(backup / "authorized.json")),
             "backup_files": recorded, "live_probe_performed": False, "credentials_exported": False,
             "journal_records_exported": False, "artifact_paths": {name: str(path) for name, path in paths.items()},
             "state_directory": str(B.directory(state_directory))}
    raw = B.encoded(value) + b"\n"
    B.create(metadata, raw)
    options = {"io": native, "verify_source": verify_source, "incident_factory": incident_factory(key)}
    observed = I.collect(metadata, B.sha(raw), runtime, TX, revision, **options)
    raw = B.encoded(observed) + b"\n"
    B.create(collection, raw)
    return I.plan(collection, B.sha(raw), **options)


def managed_output(path, roots):
    B.directory(path.parent)
    need(stat.S_IMODE(path.parent.stat().st_mode) == 0o700, "owner111_private_output_required")
    need(path.is_absolute() and path.resolve() == path and not os.path.lexists(path)
         and not any(path == root or root in path.parents for root in roots), "owner111_exclusive_external_output_required")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    for name in ("runtime", "state-directory", "metadata", "collection", "failed-request", "failed-receipt", "last-good-receipt"):
        plan.add_argument("--" + name, required=True, type=Path)
    plan.add_argument("--source-revision", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan", required=True, type=Path)
    apply.add_argument("--expected-plan-sha256", required=True)
    apply.add_argument("--approved-owner111-resume-once", required=True, action="store_true")
    for command in (plan, apply):
        command.add_argument("--key-file", required=True, type=Path)
        command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = None
    try:
        mode = stat.S_IMODE(args.key_file.lstat().st_mode)
        need(mode in {0o400, 0o600}, "owner111_private_key_required")
        key = B.read(args.key_file, mode)
        if args.command == "plan":
            roots = (Path(I.IncidentIO().root), args.runtime, args.state_directory)
            outputs = (args.metadata, args.collection, args.output)
            need(len(set(outputs)) == len(outputs), "owner111_distinct_output_paths_required")
            for path in outputs:
                managed_output(path, roots)
            paths = {name: getattr(args, name) for name in HASHES}
            result = prepare_plan(args.runtime, args.state_directory, paths, key, args.source_revision,
                                  args.metadata, args.collection)
        else:
            approved, _ = I.checked(args.plan, args.expected_plan_sha256)
            snapshot = approved["reviewed"]["snapshot"]
            metadata, _ = I.checked(Path(snapshot["metadata"]["path"]), snapshot["metadata"]["sha256"])
            managed_output(args.output, tuple(Path(path) for path in
                           (snapshot["root"], snapshot["runtime"], metadata["state_directory"])))
            result = I.apply(args.plan, args.expected_plan_sha256, verify_source=source_identity,
                             incident_factory=incident_factory(key))
        raw = B.encoded(result) + b"\n"
        B.create(args.output, raw)
        print(json.dumps({"status": result.get("status", "review_required"), "output_sha256": B.sha(raw)}))
        return 0 if result.get("status") in {None, "completed"} else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if args.command == "apply" and result is not None:
            print(json.dumps({"status": "outcome-unconfirmed", "code": "owner111_result_publication_failed",
                              "operation_result_status": result["status"], "claim_path": result["claim_path"],
                              "transaction_id": TX, "automatic_retry": False}))
        else:
            print(json.dumps({"status": "blocked", "automatic_retry": False,
                              "code": str(error) if isinstance(error, B.BindingError) else "owner111_input_or_runtime_unconfirmed"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
