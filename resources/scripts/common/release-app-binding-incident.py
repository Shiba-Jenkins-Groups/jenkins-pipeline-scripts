#!/usr/bin/env python3
"""One reviewed pre-deploy App-binding incident recovery; never a deployment.

collect/plan observe existing files and runtime metadata without creating managed
locks, transactions or fences. apply archives only the exact installation marker,
resumes its original native binary once, then releases only its resource fences.
Owner startup may perform its existing metadata probes; resource fences protect
physical control until verification. No model/business API is called here.
"""
import argparse
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location("binding_incident_contract", Path(__file__).with_name("release-app-binding.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)
need = B.require
TX = re.compile(r"[0-9a-f]{32}\Z")
CONFIGS = ("owner.json", "registry.json", "installation.json", "consumer-admission.json")
ORIGINAL_FILES = {*CONFIGS, "owner.plist", "authorized.json", "operations-before-pause.jsonl"}
CLAIM = "predeploy-owner-recovery"
SOURCE_FILES = ("release-app-binding-incident.py", "release-app-binding.py")


def parse(raw):
    value = json.loads(raw, object_pairs_hook=B.pairs)
    need(isinstance(value, dict), "incident_object_required")
    return value


def pin(path, mode=0o600):
    path = Path(path)
    before = path.lstat()
    raw = B.read(path, mode)
    after = path.lstat()
    identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, stat.S_IMODE(x.st_mode), x.st_uid)
    need(identity(before) == identity(after), "incident_input_changed")
    return raw, {"path": str(path), "sha256": B.sha(raw), "device": after.st_dev,
                 "inode": after.st_ino, "bytes": len(raw), "mode": mode}


def checked(path, expected):
    need(B.SHA.fullmatch(expected or ""), "expected_raw_sha256_required")
    raw, identity = pin(path)
    need(identity["sha256"] == expected, "reviewed_raw_input_changed")
    return parse(raw), identity


def source_identity(revision):
    need(B.COMMIT.fullmatch(revision or ""), "exact_source_revision_required")
    directory = Path(__file__).resolve().parent
    root = subprocess.check_output(["git", "-C", str(directory), "rev-parse", "--show-toplevel"], text=True).strip()
    head = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
    paths = [str((directory / name).relative_to(root)) for name in SOURCE_FILES]
    tracked = subprocess.check_output(["git", "-C", root, "ls-files", "--", *paths], text=True).splitlines()
    clean = subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD", "--", *paths], check=False)
    need(head == revision and set(tracked) == set(paths) and clean.returncode == 0, "reviewed_incident_source_changed")
    return {"revision": revision, "files": {name: B.sha((directory / name).read_bytes()) for name in SOURCE_FILES}}


class IncidentIO(B.NativeIO):
    def resume(self, transaction, *, before_bootstrap):
        # Retain NativeIO.resume's kernel/listener/journal-lock proof while
        # checking the short-lived review immediately before its sole command.
        actual = self
        class Guard:
            submitted = False
            def owner(self, value, **kwargs): return actual.owner(value, **kwargs)
            def command(self, argv):
                need(argv[:2] == ["/bin/launchctl", "bootstrap"] and not self.submitted,
                     "incident_single_bootstrap_required")
                before_bootstrap()
                self.submitted = True
                return actual.command(argv)
        return B.NativeIO.resume(Guard(), transaction)

    def app_inventory(self, transaction):
        raw = self.command(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}",
                            "--filter", "label=com.docker.compose.project=shiba-goditch-prod",
                            "--filter", "label=com.docker.compose.service=app"])
        values = raw.decode("ascii").splitlines()
        need(all(B.SHA.fullmatch(value) for value in values) and len(values) == len(set(values)), "incident_app_inventory_unknown")
        return set(values)


class Incident:
    def __init__(self, metadata_path, metadata_sha, runtime, transaction_id, io):
        need(TX.fullmatch(transaction_id or ""), "exact_original_transaction_required")
        self.metadata_path, self.metadata_sha = Path(metadata_path), metadata_sha
        self.metadata, self.metadata_pin = checked(self.metadata_path, metadata_sha)
        need(self.metadata.get("transaction_id") == transaction_id and self.metadata.get("live_probe_performed") is False
             and self.metadata.get("credentials_exported") is False and self.metadata.get("journal_records_exported") is False,
             "reviewed_inactive_metadata_required")
        self.id, self.io, self.runtime = transaction_id, io, B.directory(Path(runtime))
        self.root = B.directory(Path(io.root))
        need(stat.S_IMODE(self.root.stat().st_mode) == 0o700, "private_root_required")
        self.backup = self.root / "prod" / ("app-binding-" + self.id)
        need(self.metadata.get("backup") == str(self.backup), "foreign_incident_backup")
        B.directory(self.backup)
        self.claim = self.backup / CLAIM
        self.held, self.pins, self.released = {}, {}, []
        self.archived = self.claimed = self.resume_submitted = self.resumed = False
        self.owner_after = None
        self.phase = "preflight"

    def hold(self, path):
        path = Path(path)
        B.directory(path.parent)
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600,
                 "incident_private_existing_lock_required")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held[fd] = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
            self.check_locks()
            return fd
        except BaseException:
            self.held.pop(fd, None)
            os.close(fd)
            raise

    def check_locks(self):
        for fd, expected in self.held.items():
            held, current = os.fstat(fd), Path(expected["path"]).lstat()
            need((held.st_dev, held.st_ino) == (expected["device"], expected["inode"]) == (current.st_dev, current.st_ino)
                 and stat.S_ISREG(current.st_mode) and current.st_uid == os.getuid()
                 and stat.S_IMODE(current.st_mode) == 0o600, "incident_lock_identity_changed")

    def remember(self, path, mode=0o600):
        raw, identity = pin(path, mode)
        self.pins[str(path)] = identity
        return raw

    def config_path(self, name):
        return self.root / name if name == "consumer-admission.json" else self.root / "prod" / name

    def inspect_app(self):
        t = self.transaction
        need(self.io.engine(t) == self.engine, "incident_docker_engine_changed")
        need(self.io.app_inventory(t) == {t.old_target["id"]}, "incident_replacement_app_present")
        raw = self.io.app(t, t.old_target["id"])
        t.validate_target(raw, t.old_target)
        need(isinstance(raw["State"].get("StartedAt"), str) and raw["State"]["StartedAt"]
             and type(raw.get("RestartCount")) is int and raw["RestartCount"] >= 0
             and not raw["State"].get("Dead") and not raw["State"].get("OOMKilled") and not raw["State"].get("Paused"),
             "incident_app_generation_unknown")
        return {"container_id": raw["Id"], "image_id": raw["Image"], "engine_id": self.engine,
                "started_at": raw["State"]["StartedAt"], "restart_count": raw["RestartCount"], "healthy": True,
                **{name + "_sha256": B.sha(B.encoded(raw[name])) for name in ("Config", "HostConfig", "Mounts", "NetworkSettings")}}

    def inventory_states(self):
        values = {}
        for path in sorted(self.transaction.resources.glob("*.state.json")):
            raw, identity = pin(path)
            value = parse(raw)
            key = value.get("resource_key")
            need(value.get("schema_version") == 1 and isinstance(key, str) and key
                 and path.name == B.sha(key.encode()) + ".state.json"
                 and value.get("desired_state") in {"running", "stopped", "unknown"}, "incident_invalid_resource_state")
            values[str(path)] = identity
        return values

    def check_markers(self, *, archived=False, released=()):
        t = self.transaction
        expected = {str(t.prefix(key).with_suffix(".pending")) for key in t.keys} - set(released)
        actual = {str(p) for p in t.resources.glob("*.pending")}
        need(actual == expected and not list(t.owner_directory.glob("*.lifecycle.pending"))
             and not list((t.resources / "consumer-jobs").iterdir()), "incident_foreign_fence_or_job")
        for name in actual:
            raw, identity = pin(Path(name))
            need(raw == (self.id + "\n").encode(), "incident_foreign_resource_pending")
            if name in self.pins: need(identity == self.pins[name], "incident_pending_identity_changed")
        pending = list(t.owner_directory.glob("*.pending.json"))
        need(pending == ([] if archived else [t.pending]), "incident_foreign_installation_pending")
        path = self.claim / "owner-upgrade.pending.archived.json" if archived else t.pending
        raw, identity = pin(path)
        need(raw == self.pending_raw and {k: v for k, v in identity.items() if k != "path"} ==
             {k: v for k, v in self.pins[str(t.pending)].items() if k != "path"}, "incident_installation_marker_changed")

    def open(self):
        need(not os.path.lexists(self.claim), "incident_once_claim_exists_no_retry")
        lifecycle = self.hold(self.runtime / "data/locks/prod.lock")
        t = self.transaction = B.AppBinding({}, self.runtime, lifecycle, self.io)
        t.id, t.backup = self.id, self.backup
        self.hold(t.owner_directory / "owner-upgrade.lock")
        self.hold(self.root / "consumer-helper-upgrade.lock")
        originals = {name: self.remember(self.config_path(name)) for name in CONFIGS}
        t.config, t.registry, t.installed, t.consumers = [parse(originals[n]) for n in CONFIGS]
        installed, config = t.installed, t.config
        need(installed.get("state") == "ready" and installed["identity"]["environment"] == config.get("environment") == "prod"
             and installed["identity"]["owner_uid"] == os.getuid() and config.get("enabled") is True
             and set(config["services"]) == B.SERVICES, "incident_active_installation_required")
        need(installed["owner_config_sha256"] == B.sha(originals["owner.json"])
             and installed["identity"]["registry_digest"] == B.sha(B.encoded(t.registry))
             and installed["identity"]["consumer_config_digest"] == B.sha(B.encoded(t.consumers)), "incident_binding_drift")
        t.release = self.root / "releases" / (installed["identity"]["version"] + "-" + installed["identity"]["source_commit"])
        t.manifest = parse(self.remember(t.release / "manifest.json"))
        need(B.sha(B.encoded(t.manifest)) == installed["identity"]["artifact_digest"]
             and t.manifest.get("source_dirty") is False and t.manifest["source_commit"] == installed["identity"]["source_commit"]
             and t.manifest["version"] == installed["identity"]["version"] and t.manifest.get("platform") == "darwin/arm64",
             "incident_native_manifest_changed")
        t.artifact_modes = {}
        for name, digest in t.manifest["files"].items():
            need(Path(name).name == name and B.SHA.fullmatch(digest), "incident_invalid_artifact_manifest")
            path = t.release / name
            mode = stat.S_IMODE(path.lstat().st_mode)
            need(mode in {0o600, 0o700} and B.sha(self.remember(path, mode)) == digest, "incident_native_artifact_changed")
            t.artifact_modes[name] = mode
        need(config["executable"] == str(t.release / "owner-darwin-arm64") and config["driver_directory"] == str(t.release)
             and config["journal_file"] == str(t.journal) and config["resource_lock_directory"] == str(t.resources)
             and t.consumers["resource_lock_directory"] == str(t.resources), "incident_owner_layout_changed")
        source_pins = t.source_pins()
        need(source_pins == self.metadata["authorized"]["pins"], "incident_authorized_source_changed")
        for name, digest in source_pins.items():
            path = Path(name)
            mode = 0o700 if path == self.root / "service-consumer-admission.py" else t.artifact_modes[path.name] if path.parent == t.release and path.name != "manifest.json" else 0o600
            need(B.sha(self.remember(path, mode)) == digest, "incident_protected_source_changed")
        need(source_pins[str(t.plist)] == installed["login_agent_sha256"] == source_pins[installed["login_agent_path"]], "incident_plist_changed")
        plist = plistlib.loads(B.read(t.plist))
        need(plist["Label"] == B.LABEL and plist["ProgramArguments"] == [config["executable"], "--config", str(t.owner_directory / "owner.json")]
             and plist["RunAtLoad"] is True and plist["KeepAlive"] is True, "incident_launchagent_contract_changed")
        need(set(self.metadata["backup_files"]) == ORIGINAL_FILES, "incident_backup_contract_changed")
        for name, expected in self.metadata["backup_files"].items():
            raw = self.remember(self.backup / name)
            need(B.sha(raw) == expected["sha256"] and len(raw) == expected["bytes"], "incident_original_backup_changed")
            if name in CONFIGS: need(raw == originals[name], "incident_binding_already_committed")
        authorized = parse(B.read(self.backup / "authorized.json"))
        need(authorized.get("transaction_id") == self.id and all(authorized.get(k) == v for k, v in self.metadata["authorized"].items()),
             "incident_authorized_record_changed")
        failures = list(self.backup.glob("failure-*.json"))
        need(len(failures) == 1 and re.fullmatch(r"failure-[0-9a-f]{8}\.json", failures[0].name), "incident_exact_failure_required")
        failure = parse(self.remember(failures[0]))
        need(failure == self.metadata["failure"] and failure.get("transaction_id") == self.id and failure.get("backup") == str(self.backup)
             and failure.get("kind") == "active-app-binding" and failure.get("status") == "pending-preserved"
             and failure.get("phase") == "authorized" and failure.get("owner_pause_requested") is True
             and failure.get("owner_paused") is False and failure.get("owner_state") == "unconfirmed"
             and failure.get("code") == "owner_kernel_path_unavailable" and failure.get("automatic_retry") is False
             and failure.get("automatic_rollback") is False, "incident_predeploy_failure_required")
        need({p.name for p in self.backup.iterdir()} == ORIGINAL_FILES | {"resource-states", failures[0].name},
             "incident_progress_or_extra_evidence_present")
        app = config["services"]["app"]
        need(app["platform"] == "docker_container" and app["actions"] == ["status"] and len(app["docker"]["targets"]) == 1
             and app["lifecycle_lock"] == str(self.runtime / "data/locks/prod.lock")
             and t.registry["services"]["app"]["docker"] == app["docker"], "incident_app_binding_changed")
        t.old_target = app["docker"]["targets"][0]
        need(t.old_target["labels"].get("com.docker.compose.project") == "shiba-goditch-prod"
             and t.old_target["labels"].get("com.docker.compose.service") == "app", "incident_wrong_app")
        t.old_key = B.resource_keys(app)[0]
        t.keys = sorted({key for service in config["services"].values() for key in B.resource_keys(service)} |
                        set(t.consumers["consumers"][B.CONSUMER]["resource_keys"]))
        need(0 < len(t.keys) <= 32 and t.old_key in t.keys, "incident_resource_scope_changed")
        t.fds = {key: self.hold(t.prefix(key).with_suffix(".lock")) for key in t.keys}
        expected_states = {str(t.prefix(key).with_suffix(".state.json")) for key in t.keys}
        need(set(authorized["state_pins"]) == expected_states, "incident_active_state_scope_changed")
        need({p.name for p in (self.backup / "resource-states").iterdir()} == {Path(p).name for p in expected_states}, "incident_state_backup_changed")
        for name, digest in authorized["state_pins"].items():
            raw = self.remember(Path(name))
            value = parse(raw)
            need(B.sha(raw) == digest and value.get("desired_state") == "running"
                 and str(t.prefix(value.get("resource_key", "")).with_suffix(".state.json")) == name,
                 "incident_active_intent_changed")
            need(B.sha(self.remember(self.backup / "resource-states" / Path(name).name)) == digest, "incident_state_backup_changed")
        self.state_inventory = self.inventory_states()
        self.pins.update(self.state_inventory)
        self.pending_raw = self.remember(t.pending)
        pending = parse(self.pending_raw)
        need(pending.get("schema_version") == 1 and pending.get("kind") == "active-app-binding"
             and pending.get("transaction_id") == self.id and pending.get("backup") == str(self.backup)
             and pending.get("old_app_key") == t.old_key and B.SHA.fullmatch(pending.get("request_sha256", "")),
             "incident_exact_installation_marker_required")
        for key in t.keys: self.remember(t.prefix(key).with_suffix(".pending"))
        self.check_markers()
        self.journal_fd = self.hold(t.journal)
        self.journal_raw = self.remember(t.journal)
        before = B.read(self.backup / "operations-before-pause.jsonl")
        need(self.journal_raw.startswith(before), "incident_journal_prefix_changed")
        B.journal_idle(before); B.journal_idle(self.journal_raw)
        t.owner_before = copy.deepcopy(authorized["owner_before"])
        need(type(t.owner_before.get("pid")) is int and t.owner_before["pid"] > 0
             and t.owner_before.get("kernel_path") == config["executable"]
             and t.owner_before.get("binary_sha256") == t.manifest["files"]["owner-darwin-arm64"]
             and t.owner_before.get("uid_start", "").split(maxsplit=1)[0] == str(os.getuid()), "incident_original_owner_identity_changed")
        need(self.io.owner(t) is None, "incident_owner_must_be_explicitly_unloaded")
        self.engine = app["docker"]["engine_id"]
        self.app = self.inspect_app()
        self.check_locks()
        return self

    def snapshot(self):
        return {"transaction_id": self.id, "root": str(self.root), "backup": str(self.backup), "runtime": str(self.runtime),
                "uid": os.getuid(), "metadata": self.metadata_pin, "input_pins": self.pins,
                "state_inventory": self.state_inventory, "lock_pins": sorted(self.held.values(), key=lambda x: x["path"]),
                "owner_before": self.transaction.owner_before, "owner_state": "explicitly-unloaded", "app": self.app}

    def recheck(self, *, resumed=False):
        self.check_locks()
        for name, expected in self.pins.items():
            if name == str(self.transaction.pending) and self.archived: continue
            if name == str(self.transaction.journal) and resumed: continue
            if name in self.released: continue
            need(pin(Path(name), expected["mode"])[1] == expected, "incident_reviewed_input_identity_changed")
        need(checked(self.metadata_path, self.metadata_sha)[1] == self.metadata_pin, "incident_metadata_identity_changed")
        need(self.transaction.source_pins() == self.metadata["authorized"]["pins"], "incident_protected_source_changed")
        need(self.inventory_states() == self.state_inventory, "incident_state_inventory_changed")
        self.check_markers(archived=self.archived, released=self.released)
        need(self.inspect_app() == self.app, "incident_old_app_changed")
        if resumed:
            self.checked_journal()
            need(self.io.owner(self.transaction) == self.owner_after, "incident_resumed_owner_changed")
        else:
            need(self.io.owner(self.transaction) is None, "incident_owner_must_be_explicitly_unloaded")

    def checked_journal(self):
        # Only a concurrent byte append/unfinished line may be re-read. A full
        # active/unknown record is never waited into a later terminal state.
        old = self.pins[str(self.transaction.journal)]
        for attempt in range(5):
            self.check_locks(); self.check_markers(archived=True, released=self.released)
            current = self.transaction.journal.lstat()
            need((current.st_dev, current.st_ino) == (old["device"], old["inode"]), "incident_checked_journal_prefix_changed")
            need(self.io.owner(self.transaction) == self.owner_after, "incident_resumed_owner_changed")
            try:
                raw, identity = pin(self.transaction.journal)
            except B.BindingError as error:
                if str(error) not in {"file_changed", "incident_input_changed"} or attempt == 4: raise
            else:
                need((identity["device"], identity["inode"]) == (old["device"], old["inode"])
                     and raw.startswith(self.journal_raw), "incident_checked_journal_prefix_changed")
                if not raw or raw.endswith(b"\n"):
                    B.journal_idle(raw)
                    return
                need(attempt < 4, "partial_owner_journal")
            time.sleep(0.02)
        raise B.BindingError("incident_journal_read_unconfirmed")

    def close(self):
        for fd in reversed(list(self.held)): os.close(fd)
        self.held.clear()


def collect(metadata, expected_metadata_sha256, runtime, transaction_id, revision, *, io=None, verify_source=source_identity):
    source = verify_source(revision)
    incident = Incident(metadata, expected_metadata_sha256, runtime, transaction_id, io or IncidentIO())
    try:
        incident.open(); incident.recheck()
        return {"schema_version": 1, "kind": "app-binding-incident-collection", "source": source,
                "snapshot": incident.snapshot(), "managed_mutations": 0,
                "scope": "helper metadata reads only; native owner startup may later perform its own metadata probes"}
    finally:
        incident.close()


def plan(collection_path, expected_collection_sha256, *, io=None, verify_source=source_identity, now=None):
    value, identity = checked(Path(collection_path), expected_collection_sha256)
    need(value.get("schema_version") == 1 and value.get("kind") == "app-binding-incident-collection", "incident_collection_required")
    old = value["snapshot"]
    current = collect(old["metadata"]["path"], old["metadata"]["sha256"], old["runtime"], old["transaction_id"],
                      value["source"]["revision"], io=io, verify_source=verify_source)
    need(current == value, "incident_collection_changed")
    now = time.time() if now is None else now
    return {"schema_version": 1, "kind": "app-binding-incident-plan", "created_at": now, "expires_at": now + 900,
            "collection": identity, "reviewed": value, "automatic_retry": False, "resume_limit": 1}


def apply(plan_path, expected_plan_sha256, *, io=None, verify_source=source_identity, now=None):
    value, plan_pin = checked(Path(plan_path), expected_plan_sha256)
    now = time.time() if now is None else now
    started = time.monotonic()
    need(set(value) == {"schema_version", "kind", "created_at", "expires_at", "collection", "reviewed", "automatic_retry", "resume_limit"}
         and value["schema_version"] == 1 and value["kind"] == "app-binding-incident-plan" and value["automatic_retry"] is False
         and value["resume_limit"] == 1 and value["created_at"] <= now < value["expires_at"]
         and 0 < value["expires_at"] - value["created_at"] <= 900, "incident_plan_expired_or_invalid")
    def valid_review():
        need(now + time.monotonic() - started < value["expires_at"], "incident_plan_expired_or_invalid")
    reviewed = value["reviewed"]
    need(verify_source(reviewed["source"]["revision"]) == reviewed["source"], "incident_producer_changed")
    collection, identity = checked(Path(value["collection"]["path"]), value["collection"]["sha256"])
    need(identity == value["collection"] and collection == reviewed, "incident_reviewed_collection_changed")
    old = reviewed["snapshot"]
    incident = Incident(old["metadata"]["path"], old["metadata"]["sha256"], old["runtime"], old["transaction_id"], io or IncidentIO())
    try:
        incident.open(); incident.recheck()
        need(incident.snapshot() == old and pin(Path(plan_path))[1] == plan_pin, "incident_reviewed_plan_changed")
        valid_review()
        incident.claim.mkdir(mode=0o700); incident.claimed = True; B.sync(incident.backup)
        B.create(incident.claim / "claim.json", B.encoded({"transaction_id": incident.id, "plan": plan_pin,
                 "source": reviewed["source"], "automatic_retry": False, "resume_limit": 1}) + b"\n")
        incident.recheck()
        valid_review()
        incident.phase = "archive-installation-marker"
        archive = incident.claim / "owner-upgrade.pending.archived.json"
        # Exclusive link+unlink preserves the original inode and cannot replace
        # an existing archive. Recheck both names immediately before removal.
        os.link(incident.transaction.pending, archive, follow_symlinks=False)
        expected_marker = incident.pins[str(incident.transaction.pending)]
        need({k: v for k, v in pin(archive)[1].items() if k != "path"} ==
             {k: v for k, v in expected_marker.items() if k != "path"}, "incident_installation_marker_changed")
        need(pin(incident.transaction.pending)[1] == expected_marker, "incident_installation_marker_changed")
        incident.transaction.pending.unlink()
        incident.archived = True
        B.sync(incident.transaction.owner_directory); B.sync(incident.claim)
        incident.recheck()
        # Startup must obtain its lifetime journal EX itself. Resource EX leases
        # and every resource pending marker remain held throughout bootstrap.
        os.close(incident.journal_fd); incident.held.pop(incident.journal_fd)
        incident.phase = "resume-validation"
        def before_bootstrap():
            valid_review()
            incident.resume_submitted = True; incident.phase = "resume-submitted"
        incident.owner_after = incident.io.resume(incident.transaction, before_bootstrap=before_bootstrap)
        before, after = incident.transaction.owner_before, incident.owner_after
        need(type(after.get("pid")) is int and after["pid"] > 0 and after["pid"] != before["pid"]
             and after.get("kernel_path") == before["kernel_path"] == incident.transaction.config["executable"]
             and after.get("binary_sha256") == before["binary_sha256"]
             and after.get("uid_start", "").split(maxsplit=1)[0] == str(os.getuid()), "incident_resume_identity_changed")
        incident.resumed = True; incident.phase = "owner-verified"
        incident.recheck(resumed=True)
        release = sorted(str(incident.transaction.prefix(key).with_suffix(".pending")) for key in incident.transaction.keys)
        B.create(incident.claim / "release-intent.json", B.encoded({"transaction_id": incident.id,
                 "markers": {name: incident.pins[name] for name in release}, "owner_after": after}) + b"\n")
        incident.phase = "release-resource-fences"
        for name in release:
            incident.recheck(resumed=True)
            need(pin(Path(name))[1] == incident.pins[name], "incident_pending_identity_changed")
            Path(name).unlink(); incident.released.append(name); B.sync(incident.transaction.resources)
        incident.recheck(resumed=True)
        result = {"schema_version": 1, "kind": "app-binding-incident-recovery", "status": "completed",
                  "transaction_id": incident.id, "owner_after": after, "owner_binary_unchanged": True,
                  "plan_sha256": expected_plan_sha256, "source_revision": reviewed["source"]["revision"],
                  "claim_path": str(incident.claim), "original_app": old["app"],
                  "installation_pending_archived": True, "resource_fences_released": release,
                  "config_intent_unchanged": True, "checked_journal_prefix_preserved": True,
                  "automatic_retry": False, "automatic_rollback": False, "deployment_performed": False,
                  "startup_metadata_probes": "possible under unchanged original owner policy"}
        B.create(incident.claim / "completed.json", B.encoded(result) + b"\n")
        return result
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt, SystemExit) as error:
        result = {"schema_version": 1, "kind": "app-binding-incident-recovery", "status": "outcome-unconfirmed" if incident.resume_submitted else "blocked",
                  "transaction_id": incident.id, "phase": incident.phase, "claim_present": os.path.lexists(incident.claim),
                  "plan_sha256": expected_plan_sha256, "source_revision": reviewed["source"]["revision"],
                  "claim_path": str(incident.claim), "original_app": old["app"],
                  "claim_owned": incident.claimed,
                  "installation_pending_archived": incident.archived, "resume_submitted": incident.resume_submitted,
                  "owner_verified": incident.resumed, "resource_fences_released": incident.released,
                  "resource_fences_present": [], "resource_fences_unknown": [], "automatic_retry": False,
                  "automatic_rollback": False, "code": str(error) if isinstance(error, B.BindingError) else type(error).__name__}
        if hasattr(incident, "transaction") and hasattr(incident.transaction, "keys"):
            for key in incident.transaction.keys:
                name = str(incident.transaction.prefix(key).with_suffix(".pending"))
                if name in incident.released: continue
                try:
                    need(pin(Path(name))[1] == incident.pins[name], "incident_pending_identity_changed")
                    result["resource_fences_present"].append(name)
                except (OSError, ValueError, KeyError): result["resource_fences_unknown"].append(name)
        result["resource_fences_present"].sort(); result["resource_fences_unknown"].sort()
        if incident.claimed:
            try: B.create(incident.claim / "failure.json", B.encoded(result) + b"\n")
            except (OSError, ValueError): pass
        if isinstance(error, (KeyboardInterrupt, SystemExit)): raise
        return result
    finally:
        incident.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--metadata", type=Path, required=True); c.add_argument("--expected-metadata-sha256", required=True)
    c.add_argument("--runtime", type=Path, required=True); c.add_argument("--transaction-id", required=True)
    c.add_argument("--source-revision", required=True); c.add_argument("--approved-read-only-collection", action="store_true", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--collection", type=Path, required=True); p.add_argument("--expected-collection-sha256", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--plan", type=Path, required=True); a.add_argument("--expected-plan-sha256", required=True)
    a.add_argument("--approved-predeploy-incident-owner-resume-once", action="store_true", required=True)
    for command in (c, p, a): command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = None
    try:
        B.directory(args.output.parent)
        need(stat.S_IMODE(args.output.parent.stat().st_mode) == 0o700, "private_incident_output_directory_required")
        need(not os.path.lexists(args.output), "exclusive_incident_output_required")
        if args.action == "collect":
            metadata, _ = checked(args.metadata, args.expected_metadata_sha256)
            roots = (Path(metadata["backup"]).parent.parent, args.runtime)
        else:
            data, _ = checked(args.collection if args.action == "plan" else args.plan,
                              args.expected_collection_sha256 if args.action == "plan" else args.expected_plan_sha256)
            snapshot = data["snapshot"] if args.action == "plan" else data["reviewed"]["snapshot"]
            roots = (Path(snapshot["root"]), Path(snapshot["runtime"]))
        need(args.output.is_absolute() and args.output.resolve() == args.output
             and not any(args.output == root or root in args.output.parents for root in roots), "incident_output_must_be_outside_managed_roots")
        if args.action == "collect": result = collect(args.metadata, args.expected_metadata_sha256, args.runtime, args.transaction_id, args.source_revision)
        elif args.action == "plan": result = plan(args.collection, args.expected_collection_sha256)
        else: result = apply(args.plan, args.expected_plan_sha256)
        B.create(args.output, B.encoded(result) + b"\n")
        print(json.dumps({"status": result.get("status", "review_required"), "output_sha256": B.sha(B.read(args.output))}))
        return 0 if result.get("status") in {None, "completed"} else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if args.action == "apply" and result is not None:
            print(json.dumps({"status": "outcome-unconfirmed", "code": "incident_output_publication_failed",
                              "operation_result_status": result.get("status"), "transaction_id": result["transaction_id"],
                              "claim_path": result["claim_path"], "plan_sha256": result["plan_sha256"],
                              "source_revision": result["source_revision"],
                              "owner_verified": result.get("status") == "completed" or result.get("owner_verified", False),
                              "resource_fences_released": result["resource_fences_released"],
                              "resource_fences_present": result.get("resource_fences_present", []),
                              "resource_fences_unknown": result.get("resource_fences_unknown", []), "automatic_retry": False}))
            return 1
        print(json.dumps({"status": "blocked", "automatic_retry": False,
                          "code": str(error) if isinstance(error, B.BindingError) else "incident_input_or_runtime_unconfirmed"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
