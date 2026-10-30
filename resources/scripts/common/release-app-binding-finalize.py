#!/usr/bin/env python3
"""Finalize one already-running, unconfirmed pre-deploy owner restoration.

This entry never bootstraps, bootouts, deploys or calls model/business APIs.
The original recovery records remain immutable. Existing resource fences prevent
physical control until verification; their release restores the original owner
policy, whose metadata probes may already be running.
"""
import argparse
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location("binding_finalize_history", Path(__file__).with_name("release-app-binding-incident.py"))
Old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Old)
B, need, pin, checked, parse = Old.B, Old.need, Old.pin, Old.checked, Old.parse
SOURCE_FILES = ("release-app-binding-finalize.py", *Old.SOURCE_FILES)
CLAIM = "finalize-running-owner-once"
INPUTS = {"original-inactive-metadata.json", "original-collection.json", "original-plan.json",
          "original-recovery-receipt.json", "reviewed-running-owner.json", "postfailure-barriers.json",
          "original-claim.json", "original-claim-failure.json"}
ORIGINAL_CLAIM_FILES = {"claim.json", "failure.json", "owner-upgrade.pending.archived.json"}
EXPECTED_PID = 21988


def git_source(revision, *, historical=None):
    """Current runtime files must be clean; old producer uses immutable blobs."""
    need(B.COMMIT.fullmatch(revision or ""), "finalize_exact_source_required")
    directory = Path(__file__).resolve().parent
    root = Path(subprocess.check_output(["git", "-C", str(directory), "rev-parse", "--show-toplevel"], text=True).strip())
    names = SOURCE_FILES if historical is None else Old.SOURCE_FILES
    paths = {name: str((directory / name).relative_to(root)) for name in names}
    objects = {name: B.sha(subprocess.check_output(["git", "-C", str(root), "show", revision + ":" + path]))
               for name, path in paths.items()}
    if historical is not None:
        need(historical == {"revision": revision, "files": objects}, "finalize_historical_producer_changed")
        return historical
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "--", *paths.values()], text=True).splitlines()
    clean = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", *paths.values()], check=False)
    need(head == revision and set(tracked) == set(paths.values()) and clean.returncode == 0
         and all(pin(directory / name, stat.S_IMODE((directory / name).stat().st_mode))[1]["sha256"] == digest
                 for name, digest in objects.items()), "finalize_current_producer_changed")
    return {"revision": revision, "files": objects}


def private_directory(path):
    path = B.directory(Path(path))
    info = path.lstat()
    need(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "finalize_private_directory_required")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def same_without_path(left, right):
    return {k: v for k, v in left.items() if k != "path"} == {k: v for k, v in right.items() if k != "path"}


def normalized(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


class FinalizeIO(Old.IncidentIO):
    def pause(self, transaction): raise B.BindingError("finalize_lifecycle_mutation_forbidden")
    def resume(self, transaction, **kwargs): raise B.BindingError("finalize_lifecycle_mutation_forbidden")

    def command(self, argv):
        need(argv[:3] in (["docker", "ps", "-a"], ["docker", "container", "inspect"])
             or argv[:2] == ["docker", "inspect"] or argv == ["docker", "info", "--format", "{{.ID}}"],
             "finalize_only_readonly_metadata_commands")
        return super().command(argv)

    def owner(self, transaction, **kwargs):
        value = B.NativeIO.owner(self, transaction, **kwargs)
        if value is not None:
            value = dict(value, uid_start=normalized(value.get("uid_start")))
        return value

    def observe(self, argv):
        result = subprocess.run(argv, capture_output=True, timeout=5)
        need(result.returncode == 0 and not result.stderr.strip(), "finalize_positive_observation_required")
        return result.stdout

    def owner_proof(self, transaction, expected, journal_pin):
        current = self.owner(transaction)
        need(current == expected, "finalize_running_owner_generation_changed")
        listeners = self.observe(["/usr/sbin/lsof", "-nP", "-iTCP:8120", "-sTCP:LISTEN", "-t"]).decode("ascii").splitlines()
        need(set(listeners) == {str(expected["pid"])} and len(listeners) == 1, "finalize_unique_owner_listener_required")
        raw = self.observe(["/usr/sbin/lsof", "-a", "-p", str(expected["pid"]), "-F", "pftnDi", str(transaction.journal)])
        process, rows, row = None, [], {}
        for field in raw.decode("utf-8").splitlines():
            need(bool(field), "finalize_journal_handle_unknown")
            if field[0] == "p":
                if row: rows.append((process, row)); row = {}
                process = field[1:]
            elif field[0] == "f":
                if row: rows.append((process, row))
                row = {"f": field[1:]}
            else:
                need(field[0] in {"t", "n", "D", "i"} and field[0] not in row, "finalize_journal_handle_unknown")
                row[field[0]] = field[1:]
        if row: rows.append((process, row))
        matching = [row for pid, row in rows if pid == str(expected["pid"]) and row.get("t") == "REG"
                    and re.fullmatch(r"[0-9]+[rwu]?", row.get("f", ""))
                    and row.get("n") == str(transaction.journal) and row.get("i", "").isdigit()
                    and int(row["i"]) == journal_pin["inode"] and re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", row.get("D", ""))
                    and int(row["D"], 16) == journal_pin["device"]]
        need(bool(matching), "finalize_same_pid_journal_handle_required")
        fd = os.open(transaction.journal, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            need((info.st_dev, info.st_ino) == (journal_pin["device"], journal_pin["inode"])
                 and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600, "finalize_journal_inode_changed")
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                busy = True
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                busy = False
            after = transaction.journal.lstat()
            need((after.st_dev, after.st_ino) == (info.st_dev, info.st_ino) and busy,
                 "finalize_owner_lifetime_journal_lock_required")
        finally:
            os.close(fd)
        need(self.owner(transaction) == expected, "finalize_running_owner_generation_changed")
        return {"owner": current, "listener_pids": [expected["pid"]], "journal_device": journal_pin["device"],
                "journal_inode": journal_pin["inode"], "same_pid_journal_open_handle": True,
                "journal_lifetime_exclusive_busy": True}


class RunningFinalization:
    def __init__(self, bundle_path, bundle_sha, transaction_id, io, verify_source=git_source):
        need(Old.TX.fullmatch(transaction_id or ""), "finalize_exact_original_transaction_required")
        self.bundle_path, self.bundle_sha = Path(bundle_path), bundle_sha
        self.bundle, self.bundle_pin = checked(self.bundle_path, bundle_sha)
        need(set(self.bundle.get("files", {})) == INPUTS and self.bundle.get("live_probes_this_export") is False,
             "finalize_reviewed_offline_bundle_required")
        self.documents, self.evidence_pins = {}, {str(self.bundle_path): self.bundle_pin}
        for name in sorted(INPUTS):
            spec = self.bundle["files"][name]
            document, copied = checked(self.bundle_path.parent / name, spec["sha256"])
            original, source = checked(Path(spec["source"]), spec["sha256"])
            need(copied["bytes"] == source["bytes"] == spec["bytes"] and original == document,
                 "finalize_reviewed_evidence_changed")
            self.documents[name] = document
            self.evidence_pins[copied["path"]] = copied
            self.evidence_pins[source["path"]] = source
        d = self.documents
        claim, plan, collection, failure = [d[n] for n in ("original-claim.json", "original-plan.json", "original-collection.json", "original-claim-failure.json")]
        self.original = collection["snapshot"]
        old = self.original
        self.id, self.io, self.verify_source = transaction_id, io, verify_source
        need(collection.get("schema_version") == 1 and collection.get("kind") == "app-binding-incident-collection"
             and collection.get("managed_mutations") == 0 and old["transaction_id"] == transaction_id
             and old["uid"] == os.getuid() and old["owner_state"] == "explicitly-unloaded",
             "finalize_original_collection_required")
        need(plan.get("kind") == "app-binding-incident-plan" and plan.get("schema_version") == 1
             and plan["reviewed"] == collection and plan.get("resume_limit") == 1 and plan.get("automatic_retry") is False
             and 0 < plan["expires_at"] - plan["created_at"] <= 900, "finalize_original_plan_required")
        self.historical_source = collection["source"]
        need(self.bundle.get("base") == self.historical_source["revision"] == failure.get("source_revision")
             and claim.get("source") == self.historical_source, "finalize_old_producer_lineage_changed")
        verify_source(self.historical_source["revision"], historical=self.historical_source)
        paths = {name: spec["source"] for name, spec in self.bundle["files"].items()}
        old_plan_sha = self.bundle["files"]["original-plan.json"]["sha256"]
        self.old_plan_sha = old_plan_sha
        need(claim.get("transaction_id") == transaction_id and claim.get("resume_limit") == 1
             and claim.get("automatic_retry") is False and claim["plan"]["path"] == paths["original-plan.json"]
             and claim["plan"]["sha256"] == old_plan_sha and claim["plan"] == self.evidence_pins[paths["original-plan.json"]]
             and plan["collection"] == self.evidence_pins[paths["original-collection.json"]]
             and old["metadata"] == self.evidence_pins[paths["original-inactive-metadata.json"]],
             "finalize_original_raw_chain_changed")
        self.base = Old.Incident(paths["original-inactive-metadata.json"], old["metadata"]["sha256"], old["runtime"], transaction_id, io)
        need(old["root"] == str(self.base.root) and old["backup"] == str(self.base.backup), "finalize_original_layout_changed")
        self.claim = self.base.claim / CLAIM
        need(paths["original-claim.json"] == str(self.base.claim / "claim.json")
             and paths["original-claim-failure.json"] == str(self.base.claim / "failure.json"), "finalize_original_claim_path_changed")
        expected_pending = sorted(p for p in old["input_pins"] if p.endswith(".pending"))
        need(len(expected_pending) == 8 and failure == d["original-recovery-receipt.json"]
             and failure.get("schema_version") == 1 and failure.get("kind") == "app-binding-incident-recovery"
             and failure.get("status") == "outcome-unconfirmed" and failure.get("phase") == "resume-submitted"
             and failure.get("transaction_id") == transaction_id and failure.get("claim_path") == str(self.base.claim)
             and failure.get("plan_sha256") == old_plan_sha and failure.get("claim_owned") is True
             and failure.get("claim_present") is True and failure.get("resume_submitted") is True
             and failure.get("owner_verified") is False and failure.get("installation_pending_archived") is True
             and failure.get("automatic_retry") is False and failure.get("automatic_rollback") is False
             and failure.get("resource_fences_released") == [] and failure.get("resource_fences_unknown") == []
             and failure.get("resource_fences_present") == expected_pending and failure.get("original_app") == old["app"],
             "finalize_exact_unconfirmed_resume_required")
        barriers = d["postfailure-barriers.json"]
        need(barriers.get("original_startup_pending_archived") is True and barriers.get("claim_present") is True
             and barriers.get("completed_receipt_exists") is False and barriers.get("transaction_id") == transaction_id
             and barriers.get("resource_fences_released") == [] and barriers.get("no_new_bootstrap") is True
             and barriers.get("no_runtime_mutation") is True
             and barriers.get("resource_fences_still_present_and_unchanged") == [
                 {"path": p, "sha256": old["input_pins"][p]["sha256"], "inode": old["input_pins"][p]["inode"]}
                 for p in expected_pending], "finalize_reviewed_barrier_evidence_required")
        observation = d["reviewed-running-owner.json"]
        kernel = observation["kernel_path"]
        process = observation["uid_start_comm"]
        need(process.endswith(kernel), "finalize_reviewed_running_owner_required")
        start = normalized(process[:-len(kernel)])
        self.expected_owner = {"pid": observation["pid"], "kernel_path": kernel, "uid_start": start,
                               "binary_sha256": observation["binary_sha256"]}
        need(observation.get("launchctl_exit") == observation.get("ps_exit") == observation.get("listener_exit") == 0
             and observation.get("pid") == EXPECTED_PID and observation.get("proc_pidpath_return", 0) > 0
             and observation.get("proc_pidpath_errno") == 0 and observation.get("path_matches_original") is True
             and observation.get("binary_matches_original") is True and observation.get("listener_pids") == [str(EXPECTED_PID)]
             and kernel == observation.get("expected_original_binary") == old["owner_before"]["kernel_path"]
             and observation["binary_sha256"] == observation.get("expected_binary_sha256") == old["owner_before"]["binary_sha256"]
             and start.split(maxsplit=1)[0] == str(os.getuid()) and len(start.split(maxsplit=1)) == 2,
             "finalize_exact_reviewed_running_owner_required")
        self.claimed = self.owner_verified = False
        self.phase, self.released = "preflight", self.base.released
        self.claim_pin, self.original_claim_pin, self.original_claim_pins = None, None, {}
        self.source = None
        self.review_pins, self.claim_records = {}, {}
        self.producer_pins = {str(Path(__file__).with_name(name)): pin(Path(__file__).with_name(name), stat.S_IMODE(Path(__file__).with_name(name).stat().st_mode))[1] for name in SOURCE_FILES}

    def check_records(self):
        need(private_directory(self.base.claim) == self.original_claim_pin, "finalize_original_claim_identity_changed")
        allowed = ORIGINAL_CLAIM_FILES | ({CLAIM} if self.claimed else set())
        need({p.name for p in self.base.claim.iterdir()} == allowed, "finalize_original_progress_or_claim_present")
        for name, expected in self.original_claim_pins.items():
            need(pin(Path(name))[1] == expected, "finalize_original_record_changed")
        if self.claimed:
            need(private_directory(self.claim) == self.claim_pin, "finalize_once_claim_identity_changed")
            for name, expected in self.claim_records.items():
                need(pin(Path(name))[1] == expected, "finalize_once_record_identity_changed")
        for name, expected in self.producer_pins.items():
            need(pin(Path(name), expected["mode"])[1] == expected, "finalize_current_producer_identity_changed")
        for name, expected in self.evidence_pins.items():
            need(pin(Path(name))[1] == expected, "finalize_reviewed_evidence_identity_changed")
        # Parent backup cannot acquire deployment/commit/new-intent progress.
        allowed_backup = Old.ORIGINAL_FILES | {"resource-states", Old.CLAIM} | {Path(p).name for p in self.original["input_pins"] if Path(p).parent == self.base.backup}
        need({p.name for p in self.base.backup.iterdir()} == allowed_backup, "finalize_postdeploy_or_foreign_evidence_present")

    def read_journal(self, prefix):
        path = self.base.transaction.journal
        original = self.original["input_pins"][str(path)]
        for attempt in range(5):
            self.base.check_locks(); self.base.check_markers(archived=True, released=self.released)
            need(self.io.owner(self.base.transaction) == self.expected_owner, "finalize_running_owner_generation_changed")
            before = path.lstat()
            need((before.st_dev, before.st_ino) == (original["device"], original["inode"]), "finalize_journal_inode_changed")
            try:
                raw, identity = pin(path)
            except B.BindingError as error:
                if str(error) not in {"file_changed", "incident_input_changed"} or attempt == 4: raise
            else:
                need((identity["device"], identity["inode"]) == (original["device"], original["inode"])
                     and len(raw) >= prefix["bytes"] and B.sha(raw[:prefix["bytes"]]) == prefix["sha256"],
                     "finalize_checked_journal_prefix_changed")
                if not raw or raw.endswith(b"\n"):
                    B.journal_idle(raw)
                    return raw, identity
                need(attempt < 4, "partial_owner_journal")
            time.sleep(0.02)
        raise B.BindingError("finalize_journal_read_unconfirmed")

    def open(self, reviewed=None):
        base, old = self.base, self.original
        need(not os.path.lexists(self.claim), "finalize_once_claim_exists_no_retry")
        self.original_claim_pin = private_directory(base.claim)
        need({p.name for p in base.claim.iterdir()} == ORIGINAL_CLAIM_FILES, "finalize_original_progress_or_claim_present")
        self.original_claim_pins = {str(p): pin(p)[1] for p in base.claim.iterdir()}
        lifecycle = base.hold(base.runtime / "data/locks/prod.lock")
        t = base.transaction = B.AppBinding({}, base.runtime, lifecycle, self.io)
        t.id, t.backup = self.id, base.backup
        base.hold(t.owner_directory / "owner-upgrade.lock")
        base.hold(base.root / "consumer-helper-upgrade.lock")
        t.config, t.registry, t.installed, t.consumers = [parse(pin(base.config_path(n))[0]) for n in Old.CONFIGS]
        installed = t.installed
        t.release = base.root / "releases" / (installed["identity"]["version"] + "-" + installed["identity"]["source_commit"])
        t.manifest = parse(pin(t.release / "manifest.json")[0])
        t.artifact_modes = {name: stat.S_IMODE((t.release / name).lstat().st_mode) for name in t.manifest["files"]}
        t.old_target = t.config["services"]["app"]["docker"]["targets"][0]
        t.old_key = B.resource_keys(t.config["services"]["app"])[0]
        t.keys = sorted({key for service in t.config["services"].values() for key in B.resource_keys(service)} |
                        set(t.consumers["consumers"][B.CONSUMER]["resource_keys"]))
        need(len(t.keys) == 8 and set(base.metadata["authorized"]["state_pins"]) ==
             {str(t.prefix(key).with_suffix(".state.json")) for key in t.keys}, "finalize_exact_eight_resource_scope_required")
        t.fds = {key: base.hold(t.prefix(key).with_suffix(".lock")) for key in t.keys}
        expected_locks = [v for v in old["lock_pins"] if v["path"] != str(t.journal)]
        need(sorted(base.held.values(), key=lambda p: p["path"]) == expected_locks,
             "finalize_original_lock_identity_changed")
        base.pins = copy.deepcopy(old["input_pins"])
        base.state_inventory = copy.deepcopy(old["state_inventory"])
        base.pending_raw = pin(base.claim / "owner-upgrade.pending.archived.json")[0]
        base.archived = True
        t.owner_before = copy.deepcopy(old["owner_before"])
        base.owner_after = self.expected_owner
        base.engine, base.app = old["app"]["engine_id"], old["app"]
        original_journal = old["input_pins"][str(t.journal)]
        raw, identity = self.read_journal(original_journal)
        self.journal_prefix = copy.deepcopy(reviewed["journal_prefix"] if reviewed else identity)
        need(self.journal_prefix["path"] == str(t.journal) and self.journal_prefix["mode"] == 0o600
             and (self.journal_prefix["device"], self.journal_prefix["inode"]) == (original_journal["device"], original_journal["inode"])
             and self.journal_prefix["bytes"] >= original_journal["bytes"]
             and len(raw) >= self.journal_prefix["bytes"] and B.sha(raw[:self.journal_prefix["bytes"]]) == self.journal_prefix["sha256"],
             "finalize_reviewed_journal_prefix_changed")
        base.journal_raw = raw[:self.journal_prefix["bytes"]]
        B.journal_idle(base.journal_raw)
        self.owner_proof = self.io.owner_proof(t, self.expected_owner, original_journal)
        self.recheck()
        self.owner_verified = True
        return self

    def recheck(self):
        self.check_records()
        base = self.base
        base.check_locks()
        for name, expected in base.pins.items():
            if name in {str(base.transaction.pending), str(base.transaction.journal)} or name in self.released: continue
            need(pin(Path(name), expected["mode"])[1] == expected, "finalize_reviewed_input_identity_changed")
        need(checked(base.metadata_path, base.metadata_sha)[1] == base.metadata_pin, "finalize_metadata_identity_changed")
        need(base.transaction.source_pins() == base.metadata["authorized"]["pins"], "finalize_protected_source_changed")
        need(base.inventory_states() == base.state_inventory, "finalize_state_inventory_changed")
        base.check_markers(archived=True, released=self.released)
        need(base.inspect_app() == base.app, "finalize_original_app_changed")
        need(self.io.owner(base.transaction) == self.expected_owner, "finalize_running_owner_generation_changed")
        for name, expected in self.review_pins.items():
            need(pin(Path(name))[1] == expected, "finalize_reviewed_plan_changed")
        self.read_journal(self.journal_prefix)
        need(self.io.owner_proof(self.base.transaction, self.expected_owner,
                                 self.original["input_pins"][str(self.base.transaction.journal)]) == self.owner_proof,
             "finalize_owner_proof_changed")

    def snapshot(self):
        return {"transaction_id": self.id, "root": str(self.base.root), "runtime": str(self.base.runtime),
                "backup": str(self.base.backup), "bundle": self.bundle_pin, "evidence_pins": self.evidence_pins, "producer_pins": self.producer_pins,
                "original_claim_directory": self.original_claim_pin, "original_claim_files": self.original_claim_pins,
                "original_plan_sha256": self.old_plan_sha, "original_source": self.historical_source,
                "original_app": self.original["app"], "journal_prefix": self.journal_prefix, "owner_proof": self.owner_proof}

    def outcome(self, status, plan_sha, source, error=None):
        present, unknown, removed = [], [], []
        if hasattr(self.base, "transaction"):
            for key in getattr(self.base.transaction, "keys", ()):
                name = str(self.base.transaction.prefix(key).with_suffix(".pending"))
                if name in self.released:
                    try: Path(name).lstat()
                    except FileNotFoundError: removed.append(name)
                    except OSError: unknown.append(name)
                    else: unknown.append(name)
                    continue
                try:
                    need(pin(Path(name))[1] == self.base.pins[name], "finalize_pending_identity_changed")
                    present.append(name)
                except (OSError, ValueError, KeyError): unknown.append(name)
        return {"schema_version": 1, "kind": "app-binding-running-owner-finalization", "status": status,
                "transaction_id": self.id, "phase": self.phase, "claim_path": str(self.claim),
                "claim_owned": self.claimed, "plan_sha256": plan_sha, "source_revision": source["revision"],
                "original_plan_sha256": self.old_plan_sha, "original_source_revision": self.historical_source["revision"],
                "original_claim_path": str(self.base.claim), "original_app": self.original["app"],
                "owner_verified": self.owner_verified, "owner_after": self.expected_owner,
                "resource_fences_released": sorted(removed), "resource_fences_present": sorted(present),
                "resource_fence_unlink_completed": list(self.released),
                "resource_fences_unknown": sorted(unknown), "bootstrap_performed": False,
                "deployment_performed": False, "automatic_retry": False, "automatic_rollback": False,
                "helper_modified_original_records": False, "helper_modified_config_or_intent": False,
                "checked_journal_prefix_preserved": True if status == "completed" else None,
                "journal_prefix_checked_before_failure": self.owner_verified if status != "completed" else False,
                "original_autorecovery_permitted_after_release": len(removed) == 8 and not unknown,
                "startup_metadata_probes": "possible under unchanged original owner policy",
                **({"code": str(error) if isinstance(error, B.BindingError) else type(error).__name__} if error else {})}

    def write_record(self, name, value):
        raw = B.encoded(value) + b"\n"
        path = self.claim / name
        B.create(path, raw)
        actual, identity = pin(path)
        need(actual == raw, "finalize_once_record_identity_changed")
        self.claim_records[str(path)] = identity

    def close(self): self.base.close()


def collect(bundle, expected_bundle_sha256, transaction_id, revision, *, io=None, verify_source=git_source):
    source = verify_source(revision)
    current = RunningFinalization(bundle, expected_bundle_sha256, transaction_id, io or FinalizeIO(), verify_source)
    try:
        current.open(); current.recheck()
        return {"schema_version": 1, "kind": "app-binding-running-owner-collection", "source": source,
                "snapshot": current.snapshot(), "managed_mutations": 0, "bootstrap_performed": False}
    finally: current.close()


def plan(collection_path, expected_collection_sha256, *, io=None, verify_source=git_source, now=None):
    value, identity = checked(Path(collection_path), expected_collection_sha256)
    need(value.get("kind") == "app-binding-running-owner-collection" and value.get("schema_version") == 1
         and value.get("managed_mutations") == 0 and value.get("bootstrap_performed") is False,
         "finalize_collection_required")
    source = verify_source(value["source"]["revision"])
    need(source == value["source"], "finalize_current_producer_changed")
    old = value["snapshot"]
    current = RunningFinalization(old["bundle"]["path"], old["bundle"]["sha256"], old["transaction_id"], io or FinalizeIO(), verify_source)
    try:
        current.open(old); current.recheck()
        need(current.snapshot() == old, "finalize_reviewed_collection_changed")
    finally: current.close()
    now = time.time() if now is None else now
    return {"schema_version": 1, "kind": "app-binding-running-owner-plan", "created_at": now, "expires_at": now + 900,
            "collection": identity, "reviewed": value, "automatic_retry": False, "bootstrap_limit": 0, "finalize_limit": 1}


def apply(plan_path, expected_plan_sha256, *, io=None, verify_source=git_source, now=None):
    value, plan_pin = checked(Path(plan_path), expected_plan_sha256)
    now = time.time() if now is None else now
    started = time.monotonic()
    def valid_review():
        need(set(value) == {"schema_version", "kind", "created_at", "expires_at", "collection", "reviewed", "automatic_retry", "bootstrap_limit", "finalize_limit"}
             and value["schema_version"] == 1 and value["kind"] == "app-binding-running-owner-plan"
             and value["automatic_retry"] is False and value["bootstrap_limit"] == 0 and value["finalize_limit"] == 1
             and value["created_at"] <= now + time.monotonic() - started < value["expires_at"]
             and 0 < value["expires_at"] - value["created_at"] <= 900, "finalize_plan_expired_or_invalid")
    valid_review()
    reviewed, old = value["reviewed"], value["reviewed"]["snapshot"]
    source = verify_source(reviewed["source"]["revision"])
    need(source == reviewed["source"], "finalize_current_producer_changed")
    collection, identity = checked(Path(value["collection"]["path"]), value["collection"]["sha256"])
    need(collection == reviewed and identity == value["collection"], "finalize_reviewed_collection_changed")
    current = RunningFinalization(old["bundle"]["path"], old["bundle"]["sha256"], old["transaction_id"], io or FinalizeIO(), verify_source)
    try:
        current.review_pins = {str(plan_path): plan_pin, identity["path"]: identity}
        current.open(old); current.recheck()
        need(current.snapshot() == old and pin(Path(plan_path))[1] == plan_pin, "finalize_reviewed_plan_changed")
        valid_review()
        current.claim.mkdir(mode=0o700); current.claimed = True
        current.claim_pin = private_directory(current.claim); B.sync(current.base.claim)
        current.write_record("claim.json", {"transaction_id": current.id, "plan": plan_pin,
                 "source": source, "original_plan_sha256": current.old_plan_sha, "original_source": current.historical_source,
                 "bootstrap_limit": 0, "finalize_limit": 1, "automatic_retry": False})
        current.recheck()
        release = sorted(str(current.base.transaction.prefix(key).with_suffix(".pending")) for key in current.base.transaction.keys)
        current.write_record("release-intent.json", {"transaction_id": current.id,
                 "plan_sha256": expected_plan_sha256, "owner_proof": current.owner_proof,
                 "markers": {name: current.base.pins[name] for name in release}, "bootstrap_performed": False})
        current.phase = "release-resource-fences"
        for name in release:
            current.recheck()
            need(pin(Path(name))[1] == current.base.pins[name], "finalize_pending_identity_changed")
            valid_review()  # Fresh at the first unlink, also prevents a late partial release continuing.
            Path(name).unlink(); current.released.append(name); B.sync(current.base.transaction.resources)
        current.recheck(); current.phase = "final-release-verification"
        result = current.outcome("completed", expected_plan_sha256, source)
        need(len(result["resource_fences_released"]) == 8 and result["resource_fences_present"] == []
             and result["resource_fences_unknown"] == [], "finalize_release_outcome_unconfirmed")
        current.phase = result["phase"] = "completed"
        B.create(current.claim / "completed.json", B.encoded(result) + b"\n")
        return result
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt, SystemExit) as error:
        result = current.outcome("outcome-unconfirmed" if current.claimed else "blocked", expected_plan_sha256, source, error)
        if current.claimed:
            try: B.create(current.claim / "failure.json", B.encoded(result) + b"\n")
            except (OSError, ValueError): pass
        if isinstance(error, (KeyboardInterrupt, SystemExit)): raise
        return result
    finally: current.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--bundle", type=Path, required=True); c.add_argument("--expected-bundle-sha256", required=True)
    c.add_argument("--transaction-id", required=True); c.add_argument("--source-revision", required=True)
    c.add_argument("--approved-read-only-collection", action="store_true", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--collection", type=Path, required=True); p.add_argument("--expected-collection-sha256", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--plan", type=Path, required=True); a.add_argument("--expected-plan-sha256", required=True)
    a.add_argument("--approved-running-owner-fence-finalization-once", action="store_true", required=True)
    for command in (c, p, a): command.add_argument("--output", type=Path, required=True)
    args, result = parser.parse_args(argv), None
    try:
        private_directory(args.output.parent)
        need(not os.path.lexists(args.output), "finalize_exclusive_output_required")
        if args.action == "collect":
            bundle, _ = checked(args.bundle, args.expected_bundle_sha256)
            original, _ = checked(args.bundle.parent / "original-collection.json", bundle["files"]["original-collection.json"]["sha256"])
            old = original["snapshot"]
        else:
            document, _ = checked(args.collection if args.action == "plan" else args.plan,
                                  args.expected_collection_sha256 if args.action == "plan" else args.expected_plan_sha256)
            old = document["snapshot"] if args.action == "plan" else document["reviewed"]["snapshot"]
        roots = (Path(old["root"]), Path(old["runtime"]))
        need(args.output.is_absolute() and args.output.resolve() == args.output
             and not any(args.output == root or root in args.output.parents for root in roots), "finalize_output_must_be_outside_managed_roots")
        if args.action == "collect": result = collect(args.bundle, args.expected_bundle_sha256, args.transaction_id, args.source_revision)
        elif args.action == "plan": result = plan(args.collection, args.expected_collection_sha256)
        else: result = apply(args.plan, args.expected_plan_sha256)
        B.create(args.output, B.encoded(result) + b"\n")
        print(json.dumps({"status": result.get("status", "review_required"), "output_sha256": B.sha(B.read(args.output))}))
        return 0 if result.get("status") in {None, "completed"} else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if args.action == "apply" and result is not None:
            known = {k: result[k] for k in ("transaction_id", "claim_path", "plan_sha256", "source_revision", "owner_verified",
                     "resource_fences_released", "resource_fences_present", "resource_fences_unknown", "bootstrap_performed")}
            print(json.dumps({"status": "outcome-unconfirmed", "code": "finalize_output_publication_failed",
                              "operation_result_status": result["status"], "automatic_retry": False, **known}))
        else:
            print(json.dumps({"status": "blocked", "automatic_retry": False, "bootstrap_performed": False,
                              "code": str(error) if isinstance(error, B.BindingError) else "finalize_input_or_runtime_unconfirmed"}))
        return 1


if __name__ == "__main__": sys.exit(main())
