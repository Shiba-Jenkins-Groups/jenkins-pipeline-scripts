"""Exact committed checkpoint for the App PROD #101 binding incident."""

from contextlib import contextmanager
from datetime import datetime
import copy
import fcntl
import importlib.util
import os
from pathlib import Path
import re
import stat
import time


_spec = importlib.util.spec_from_file_location("postdeploy_binding", Path(__file__).with_name("release-app-binding.py"))
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)

TRANSACTION = "f52ecd166595666ecd7a248f100583da"
COMMIT = "669025dd28ee2e2f6b01dccf358f78a7c2fb11eb"
DIGEST = "sha256:3d0d2d7dd8816668ea7029bde7ae36ac6e956dea367c264dd3b1562ad62883de"
VERSION = "1.1.25"
OMLX_PROGRAM = "a59b7416fa2dc0a94e750bb72ff90f03b0fbbc06dbe3763b158f2cf2d19ea6f2"
OMLX_OWNER = "f331b32de359c3ec029efbfa92a3b6a780fe032938eff21ed91b1af557ad2512"
EVENTS = (
    "authorized", "owner-paused", "deploy-admitted", "commit-authorized", "new-intent-written",
    "committed-owner.json", "committed-registry.json", "committed-consumer-admission.json",
    "committed-installation.json",
)
CONFIGS = ("owner.json", "registry.json", "consumer-admission.json", "installation.json")
PRODUCT = "shiba-go-ditch-api-project"
PROJECT = "shiba-goditch-prod"
need = B.require


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    need(parsed.tzinfo is not None, "postdeploy_invalid_timestamp")
    return parsed


class Checkpoint:
    """Hold the original release, admission and maintenance domains throughout recovery."""

    def __init__(self, runtime, state_directory, request, failed, io):
        self.runtime, self.state_directory = Path(runtime), Path(state_directory)
        self.request, self.failed, self.io = request, failed, io
        self._locks = {}
        self._pins = {}
        self._fences = {}
        self._journal_fd = None
        self._journal_loaned = False
        self._last_configs = None
        self._job_released = False
        self.released_fences = []
        self.transaction = None

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_uid, info.st_mode

    def _snapshot(self, path, mode=0o600):
        before = self._identity(path.lstat())
        raw = B.read(path, mode)
        need(self._identity(path.lstat()) == before, "postdeploy_file_changed")
        return before, raw

    def _remember(self, path, mode=0o600):
        identity, raw = self._snapshot(path, mode)
        self._pins[path] = (mode, identity, B.sha(raw))
        return raw

    def _check_pin(self, path):
        mode, expected, digest = self._pins[path]
        identity, raw = self._snapshot(path, mode)
        need(identity == expected and B.sha(raw) == digest, "postdeploy_pinned_file_changed")

    def _lock(self, path):
        B.directory(path.parent)
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            current = os.fstat(fd)
            need(stat.S_ISREG(current.st_mode) and current.st_uid == os.getuid()
                 and stat.S_IMODE(current.st_mode) == 0o600, "postdeploy_existing_private_lock_required")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locks[fd] = (path, current.st_dev, current.st_ino)
            self._check_locks()
        except BaseException:
            self._locks.pop(fd, None)
            os.close(fd)
            raise
        return fd

    def _check_locks(self):
        for fd, (path, device, inode) in self._locks.items():
            current, held = path.lstat(), os.fstat(fd)
            need((current.st_dev, current.st_ino) == (device, inode) == (held.st_dev, held.st_ino)
                 and stat.S_ISREG(current.st_mode) and current.st_uid == os.getuid()
                 and stat.S_IMODE(current.st_mode) == 0o600, "postdeploy_lock_identity_changed")

    @contextmanager
    def locked(self):
        need(self.transaction is None, "postdeploy_checkpoint_not_reusable")
        try:
            lifecycle = self._lock(self.runtime / "data/locks/prod.lock")
            self._lock(self.state_directory / "promotion.lock")
            self.transaction = B.AppBinding(self.request, self.runtime, lifecycle, self.io)
            self._load()
            self.check_after(self.configs)
            yield self
        finally:
            for fd in reversed(self._locks):
                os.close(fd)
            self._locks.clear()

    def _load(self):
        request, failed, t = self.request, self.failed, self.transaction
        t.id = TRANSACTION
        t.backup = B.directory(t.owner_directory / ("app-binding-" + t.id))
        need(stat.S_IMODE(t.backup.lstat().st_mode) == 0o700, "postdeploy_private_checkpoint_required")
        self._lock(t.owner_directory / "owner-upgrade.lock")
        self._lock(t.root / "consumer-helper-upgrade.lock")

        for payload in (request, failed):
            need(payload["product"] == PRODUCT and payload["version"] == VERSION
                 and payload["commit"] == COMMIT and payload["digest"] == DIGEST, "postdeploy_release_identity_mismatch")
        need(request["kind"] == "deployment" and request["schema_version"] == 2
             and "recovery" not in request and "mode" not in request, "postdeploy_normal_signed_request_required")
        need(failed["kind"] == "runtime-deployment" and failed["schema_version"] == 1 and failed["status"] == "FAILED"
             and failed["automatic_rollback"] is False and failed["mutation_may_have_started"] is True
             and bool(failed["new_backup_and_migration_files"]), "postdeploy_failed_deployment_required")
        recorded = B.json.loads(self._remember(self.state_directory / (COMMIT + ".json")), object_pairs_hook=B.pairs)
        need(set(recorded) == {"payload", "signature"} and recorded["payload"] == failed
             and B.SHA.fullmatch(recorded["signature"]), "postdeploy_failed_state_changed")
        failure = failed["app_binding"]
        need(failure == {"schema_version": 1, "kind": "active-app-binding", "status": "pending-preserved",
             "transaction_id": t.id, "phase": "committed-installation.json", "owner_paused": True,
             "owner_pause_requested": True, "owner_state": "paused", "backup": str(t.backup),
             "code": "owner_resume_unconfirmed", "automatic_retry": False, "automatic_rollback": False},
             "postdeploy_exact_incident_required")
        failures = list(t.backup.glob("failure-*.json"))
        need(len(failures) == 1 and re.fullmatch(r"failure-[0-9a-f]{8}\.json", failures[0].name)
             and B.json.loads(self._remember(failures[0]), object_pairs_hook=B.pairs) == failure,
             "postdeploy_checkpoint_failure_changed")
        need(not any((t.backup / (name + ".json")).exists() for name in ("completed", "owner-resumed")),
             "postdeploy_completed_or_foreign_progress")

        events = {}
        for name in EVENTS:
            event = B.json.loads(self._remember(t.backup / (name + ".json")), object_pairs_hook=B.pairs)
            need(event["transaction_id"] == t.id, "postdeploy_checkpoint_transaction_changed")
            events[name] = event
        dates = [timestamp(events[name]["at"]) for name in EVENTS]
        need(dates == sorted(dates) and dates[-1] <= timestamp(failed["finished_at"])
             and timestamp(failed["started_at"]) <= dates[0],
             "postdeploy_checkpoint_event_order_changed")

        authorized = events["authorized"]
        committed = events["commit-authorized"]
        self.observed = copy.deepcopy(committed["observed"])
        self.configs = {self._config_path(name): B.read(self._config_path(name)) for name in CONFIGS}
        self.original_configs = {name: self._remember(t.backup / name) for name in CONFIGS}
        need(committed["after_sha256"] == {str(path): B.sha(raw) for path, raw in self.configs.items()},
             "postdeploy_committed_config_changed")
        self._set_configs(self.configs)
        original_owner = self._original_data("owner.json")
        original_consumers = self._original_data("consumer-admission.json")
        t.old_target = original_owner["services"]["app"]["docker"]["targets"][0]
        need(t.old_target["id"] == failed["previous_container_id"]
             and t.old_target["image_id"] == failed["previous_image_id"], "postdeploy_original_app_changed")
        t.old_key = B.resource_keys(original_owner["services"]["app"])[0]
        t.expected_image_id = self.observed["image_id"]
        t.qualify_observed(self.observed)
        t.new_key = B.resource_keys(t.config["services"]["app"])[0]
        need(t.new_key != t.old_key and t.config["services"]["app"]["docker"]["targets"] ==
             [t.target_for(self.observed["container_id"])], "postdeploy_committed_app_target_changed")
        self.engine_id = t.config["services"]["app"]["docker"]["engine_id"]
        self._load_protected(authorized)
        self._load_admission(authorized, original_owner, original_consumers)
        self._load_startup_marker()
        self._load_journal()

    def _config_path(self, name):
        t = self.transaction
        return (t.root if name == "consumer-admission.json" else t.owner_directory) / name

    def _original_data(self, name):
        return B.json.loads(self.original_configs[name], object_pairs_hook=B.pairs)

    def _set_configs(self, configurations):
        t = self.transaction
        need(set(configurations) == {self._config_path(name) for name in CONFIGS},
             "postdeploy_review_requires_all_configurations")
        t.config, t.registry, t.consumers, t.installed = [
            B.json.loads(configurations[self._config_path(name)], object_pairs_hook=B.pairs) for name in CONFIGS
        ]
        need(t.installed["state"] == "ready" and t.config["enabled"] is True
             and t.installed["identity"]["environment"] == t.config["environment"] == "prod"
             and t.installed["identity"]["owner_uid"] == os.getuid()
             and t.installed["owner_config_sha256"] == B.sha(configurations[self._config_path("owner.json")])
             and t.installed["identity"]["registry_digest"] == B.sha(B.encoded(t.registry))
             and t.installed["identity"]["consumer_config_digest"] == B.sha(B.encoded(t.consumers)),
             "postdeploy_installation_identity_changed")
        service = t.config["services"]["app"]
        need(service["actions"] == ["status"] and service["docker"] == t.registry["services"]["app"]["docker"]
             and service["revision"] == t.registry["services"]["app"]["revision"]
             and service["lifecycle_lock"] == str(self.runtime / "data/locks/prod.lock"),
             "postdeploy_app_status_only_required")

    def _load_protected(self, authorized):
        t = self.transaction
        identity = t.installed["identity"]
        t.release = B.directory(t.root / "releases" / (identity["version"] + "-" + identity["source_commit"]))
        t.manifest = B.document(t.release / "manifest.json")
        need(B.sha(B.encoded(t.manifest)) == identity["artifact_digest"], "postdeploy_owner_artifact_changed")
        t.artifact_modes = {name: stat.S_IMODE((t.release / name).lstat().st_mode) for name in t.manifest["files"]}
        t.owner_before = copy.deepcopy(authorized["owner_before"])
        t.pins = authorized["pins"]
        need(t.source_pins() == t.pins
             and t.owner_before["kernel_path"] == t.config["executable"] == str(t.release / "owner-darwin-arm64")
             and t.manifest["files"]["owner-darwin-arm64"] == t.owner_before["binary_sha256"]
             and t.pins[str(t.plist)] == t.installed["login_agent_sha256"] == t.pins[t.installed["login_agent_path"]]
             and self._remember(t.backup / "owner.plist") == B.read(t.plist), "postdeploy_protected_owner_source_changed")

    def _load_admission(self, authorized, original_owner, original_consumers):
        t = self.transaction
        t.scope = sorted(original_consumers["consumers"][B.CONSUMER]["resource_keys"])
        keys = set(t.scope)
        for service in original_owner["services"].values():
            keys.update(B.resource_keys(service))
        t.keys = sorted(keys | {t.new_key})
        need(t.old_key in t.scope and len(keys) == 8 and t.new_key not in keys, "postdeploy_resource_scope_changed")
        expected_states = {t.prefix(key).with_suffix(".state.json"): key for key in keys}
        need(set(authorized["state_pins"]) == {str(path) for path in expected_states},
             "postdeploy_original_state_scope_changed")
        for key in t.keys:
            prefix = t.prefix(key)
            self._lock(prefix.with_suffix(".lock"))
            fence = prefix.with_suffix(".pending")
            need(self._remember(fence) == (t.id + "\n").encode(), "postdeploy_foreign_resource_fence")
            self._fences[fence] = key
        for path, key in expected_states.items():
            raw = self._remember(path)
            need(B.sha(raw) == authorized["state_pins"][str(path)]
                 and self._remember(t.backup / "resource-states" / path.name) == raw,
                 "postdeploy_original_intent_changed")
            self._check_intent(raw, key)
        self._check_intent(self._remember(t.prefix(t.new_key).with_suffix(".state.json")), t.new_key)
        t.job_path = t.resources / "consumer-jobs" / (t.id + ".json")
        t.job_raw = self._remember(t.job_path)
        t.job = B.json.loads(t.job_raw, object_pairs_hook=B.pairs)
        expected_scope = sorted(t.new_key if key == t.old_key else key for key in t.scope)
        need(t.job["schema_version"] == 1 and t.job["job_id"] == t.id and t.job["consumer_id"] == B.CONSUMER
             and t.job["resource_keys"] == expected_scope, "postdeploy_owned_admission_job_changed")
        self._assert_admission()

    @staticmethod
    def _check_intent(raw, key):
        data = B.json.loads(raw, object_pairs_hook=B.pairs)
        need(data["schema_version"] == 1 and data["resource_key"] == key and data["desired_state"] == "running",
             "postdeploy_resource_intent_changed")

    def _load_startup_marker(self):
        t = self.transaction
        self._marker_pin, t.pending_raw = self._snapshot(t.pending)
        need(self._remember(t.backup / "owner-upgrade.pending.committed.json") == t.pending_raw,
             "postdeploy_startup_marker_changed")
        marker = B.json.loads(t.pending_raw, object_pairs_hook=B.pairs)
        need(marker["schema_version"] == 1 and marker["kind"] == "active-app-binding"
             and marker["transaction_id"] == t.id and marker["backup"] == str(t.backup)
             and marker["old_app_key"] == t.old_key and marker["request_sha256"] == B.sha(B.encoded(self.request)),
             "postdeploy_startup_marker_identity_changed")

    def _load_journal(self):
        t = self.transaction
        self._journal_fd = self._lock(t.journal)
        self._journal_inode, self._journal = self._snapshot(t.journal)
        before = self._remember(t.backup / "operations-before-pause.jsonl")
        after = self._remember(t.backup / "operations-after-pause.jsonl")
        need(self._journal == after and after.startswith(before), "postdeploy_historical_journal_changed")
        B.journal_idle(before)
        B.journal_idle(after)

    def _assert_admission(self):
        t = self.transaction
        need(set(t.resources.glob("*.pending")) == set(self._fences) - set(self.released_fences)
             and not list(t.owner_directory.glob("*.lifecycle.pending")), "postdeploy_foreign_resource_fence")
        expected = set() if self._job_released else {t.job_path}
        need(set(t.job_path.parent.iterdir()) == expected, "postdeploy_foreign_admission_job")

    def _assert_journal(self):
        t = self.transaction
        for attempt in range(5):
            try:
                identity, raw = self._snapshot(t.journal)
                need(identity[:2] == self._journal_inode[:2]
                     and (raw.startswith(self._journal) if self._journal_loaned else raw == self._journal),
                     "postdeploy_historical_journal_changed")
                if not raw or raw.endswith(b"\n"):
                    B.journal_idle(raw)
                    return
            except B.BindingError as err:
                if str(err) not in {"file_changed", "postdeploy_file_changed"}:
                    raise
            if not self._journal_loaned or attempt == 4:
                break
            time.sleep(0.02)
        raise B.BindingError("postdeploy_journal_unconfirmed")

    def _assert_app(self):
        t = self.transaction
        need(self.io.engine(t) == self.engine_id and self.io.image(t) == self.observed["image_id"],
             "postdeploy_runtime_image_changed")
        ids = self.io.command(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}",
                               "--filter", "label=com.docker.compose.project=" + PROJECT,
                               "--filter", "label=com.docker.compose.service=app"]).decode("ascii").splitlines()
        need(ids == [self.observed["container_id"]], "postdeploy_app_inventory_changed")
        t.validate_target(self.io.app(t, self.observed["container_id"]), t.target_for(self.observed["container_id"]), True)

    def _check_repin_scope(self, configurations):
        if configurations == self.configs:
            return
        owner, registry, admission, installation = [self._config_path(name) for name in CONFIGS]
        need(configurations[admission] == self.configs[admission], "postdeploy_consumer_policy_change_forbidden")
        baseline = {path: B.json.loads(raw, object_pairs_hook=B.pairs) for path, raw in self.configs.items()}
        evidence = B.json.loads(configurations[registry], object_pairs_hook=B.pairs)["evidence"]["omlx"]
        need(set(evidence) == {"verified", "checked_at", "note"} and evidence["verified"] is True
             and isinstance(evidence["note"], str) and 0 < len(evidence["note"].strip()) <= 1024,
             "postdeploy_reviewed_omlx_evidence_required")
        timestamp(evidence["checked_at"])
        for path in (owner, registry):
            spec = baseline[path]["services"]["omlx"]
            need(spec["revision"] == 2, "postdeploy_original_omlx_revision_changed")
            spec["revision"] = 3
            spec["mac"].update(program_sha256=OMLX_PROGRAM, owner_sha256=OMLX_OWNER)
        need(baseline[registry]["registry_revision"] == 15, "postdeploy_original_registry_revision_changed")
        baseline[registry]["registry_revision"] = 16
        baseline[registry]["evidence"]["omlx"] = evidence
        baseline[installation]["owner_config_sha256"] = B.sha(B.encoded(baseline[owner]) + b"\n")
        baseline[installation]["identity"]["registry_digest"] = B.sha(B.encoded(baseline[registry]))
        need(all(configurations[path] == B.encoded(baseline[path]) + b"\n" for path in (owner, registry, installation)),
             "postdeploy_omlx_repin_scope_exceeded")

    def check_after(self, expected_configs):
        """Revalidate pinned barriers and runtime after the exact reviewed config update."""
        need(bool(self._locks), "postdeploy_locks_required")
        self._check_locks()
        need(set(expected_configs) == set(self.configs)
             and all(B.read(path) == raw for path, raw in expected_configs.items()), "postdeploy_reviewed_config_changed")
        self._check_repin_scope(expected_configs)
        self._set_configs(expected_configs)
        t = self.transaction
        need(t.source_pins() == t.pins and t.config["executable"] == t.owner_before["kernel_path"]
             and t.config["journal_file"] == str(t.journal)
             and t.config["resource_lock_directory"] == t.consumers["resource_lock_directory"] == str(t.resources)
             and t.config["services"]["app"]["docker"]["targets"] == [t.target_for(self.observed["container_id"])]
             and expected_configs[self._config_path("consumer-admission.json")] == self.configs[self._config_path("consumer-admission.json")],
             "postdeploy_protected_binding_changed")
        self._assert_admission()
        for path in self._pins:
            if path not in self.released_fences and (path != t.job_path or not self._job_released):
                self._check_pin(path)
        startup_pending = set(t.owner_directory.glob("*.pending.json"))
        if self._journal_loaned or not startup_pending:
            need(not startup_pending, "postdeploy_startup_marker_present")
        else:
            need(startup_pending == {t.pending} and self._snapshot(t.pending) == (self._marker_pin, t.pending_raw),
                 "postdeploy_startup_marker_changed")
        self._assert_journal()
        self._assert_app()
        self._check_locks()
        self._last_configs = dict(expected_configs)

    def release_journal(self):
        """Transfer only the exclusive lifetime journal descriptor to verified native startup."""
        t = self.transaction
        need(bool(self._locks) and self._journal_fd in self._locks and not os.path.lexists(t.pending),
             "postdeploy_startup_marker_archive_required")
        self.check_after(self._last_configs)
        os.close(self._journal_fd)
        del self._locks[self._journal_fd]
        self._journal_loaned = True

    @property
    def planned_fences(self):
        return tuple(sorted(self._fences))

    def release_fences(self, owner):
        """Use the independently proven owner generation for every admission release."""
        t = self.transaction
        need(self._journal_loaned and bool(self._last_configs), "postdeploy_owner_start_verification_required")
        need(owner and owner["pid"] != t.owner_before["pid"] and owner["binary_sha256"] == t.owner_before["binary_sha256"]
             and owner["kernel_path"] == t.config["executable"] and self.io.owner(t) == owner,
             "postdeploy_fresh_owner_required")
        deadline = time.monotonic() + 30

        def verify():
            self.check_after(self._last_configs)
            need(self.io.owner(t, deadline=deadline) == owner and self.io._resume_ready(t, owner, deadline),
                 "postdeploy_owner_readiness_changed")

        verify()
        self._check_pin(t.job_path)
        t.job_path.unlink()
        self._job_released = True
        B.sync(t.job_path.parent)
        for path in self.planned_fences:
            verify()
            self._check_pin(path)
            path.unlink()
            self.released_fences.append(path)
            B.sync(t.resources)
        verify()
        return [str(path) for path in self.released_fences]
