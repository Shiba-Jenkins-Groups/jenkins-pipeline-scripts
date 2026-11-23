"""Exact native adapter for the signed #105 committed binding continuation."""
import contextlib
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
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location("committed_native_finalization", Path(__file__).with_name("release-app-binding-finalize.py"))
Finalize = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Finalize)
Old, B, pin, parse, need = Finalize.Old, Finalize.B, Finalize.pin, Finalize.parse, Finalize.need

OWNER_UID = 501
AUTHORIZED_SHA = "e37d1d116bbe3f32a330e9db7eb7259d091c6eb8b8238ebcb22837e86727d0a8"
PROJECT = "shiba-goditch-prod"
REDIS_VOLUME = PROJECT + "_prod_redis_data"
REDIS_ID = "2efe0843fe3a460a02286158568a09c8723eaaad705eea3141e10434cc926fe1"
GATEWAY_ID = "e300a5bbc20bbe01112aeeaf0a9fa6f2af43eea8cb85d8071259c48ae74b388e"
REDIS_IMAGE = "redis:7.2.16-alpine3.21@sha256:ccd6aa8d45ff3f033d6fa15b8cc1a50579f65c89f38cf9bb607a954c4f2128ed"
GATEWAY_IMAGE = "nginx:1.30.7-alpine@sha256:bef154735288bc38d99fac41ae63730684bcc661503673a04a0f9bc526ba8598"
ARCHIVE = "committed-105-continuation"
SAFE_LABEL = re.compile(r"[a-zA-Z0-9_.-]+\Z")


class CommittedIO(Old.IncidentIO):
    def owner_proof(self, transaction, expected, journal_pin):
        return Finalize.FinalizeIO.owner_proof(self, transaction, expected, journal_pin)

    def observe(self, argv):
        return Finalize.FinalizeIO.observe(self, argv)

    def inactive_job(self, transaction):
        """Observe exact managed registration without treating errors as absence."""
        result = subprocess.run(["/bin/launchctl", "print", transaction.domain], capture_output=True, timeout=5)
        out, err = result.stdout.decode(), result.stderr.decode(errors="replace")
        if result.returncode:
            need(not out.strip() and ("Could not find service" in err or
                 "Could not find specified service" in err), "committed_launchd_state_unknown")
            return "unloaded"
        lines = out.splitlines()
        need(not err.strip() and lines and lines[0] == transaction.domain + " = {",
             "committed_launchd_target_changed")
        fields = {}
        for line in lines[1:]:
            match = re.fullmatch(r"([ \t]+)(path|program|type|state|active count|pid) = (.*)", line)
            if match:
                indent, name, value = match.groups()
                if len(indent.expandtabs(8)) == 8:
                    need(name not in fields, "committed_launchd_target_changed")
                    fields[name] = value
        need(fields.get("path") == str(transaction.plist) and fields.get("program") == transaction.config["executable"]
             and fields.get("type") == "LaunchAgent" and fields.get("active count") == "0"
             and fields.get("state") in {"not running", "spawn scheduled"} and "pid" not in fields
             and re.search(r"(?m)^\s*pid\s*=", out) is None, "committed_owner_not_provably_inactive")
        return "loaded-idle"

    def require_no_listener(self, transaction):
        result = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:" + transaction.config["listen"].rsplit(":", 1)[1],
                                 "-sTCP:LISTEN", "-t"], capture_output=True, timeout=5)
        need(result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip(),
             "committed_owner_listener_present_or_unknown")

    def bootout_idle(self, transaction):
        need(self.inactive_job(transaction) == "loaded-idle", "committed_launchd_target_changed")
        self.require_no_listener(transaction)
        self.command(["/bin/launchctl", "bootout", transaction.domain])

    def container(self, cid, keys):
        """Collect only metadata needed to pin the runtime, never Config.Env."""
        need(B.SHA.fullmatch(cid) and all(SAFE_LABEL.fullmatch(name) for name in keys),
             "committed_invalid_docker_identity")
        labels = ",".join(json.dumps(key) + ":{{json (index .Config.Labels " + json.dumps(key) + ")}}" for key in keys)
        template = ('{"Id":{{json .Id}},"Image":{{json .Image}},"RestartCount":{{json .RestartCount}},'
                    '"State":{"StartedAt":{{json .State.StartedAt}},"Running":{{json .State.Running}},'
                    '"Restarting":{{json .State.Restarting}},"Dead":{{json .State.Dead}},'
                    '"Paused":{{json .State.Paused}},"OOMKilled":{{json .State.OOMKilled}},'
                    '"Health":{"Status":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}},'
                    '"HostConfig":{"RestartPolicy":{"Name":{{json .HostConfig.RestartPolicy.Name}}}},'
                    '"NetworkSettings":{"Ports":{{json .NetworkSettings.Ports}}},'
                    '"Config":{"Image":{{json .Config.Image}},"Labels":{' + labels + '}},'
                    '"Mounts":{{json .Mounts}}}')
        return parse(self.command(["docker", "inspect", "--type", "container", "--format", template, cid]))

    def role_ids(self, role):
        raw = self.command(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}",
                            "--filter", "label=com.docker.compose.project=" + PROJECT,
                            "--filter", "label=com.docker.compose.service=" + role]).decode("ascii").splitlines()
        need(all(B.SHA.fullmatch(value) for value in raw) and len(raw) == len(set(raw)),
             "committed_invalid_docker_inventory")
        return set(raw)

    def volume_ids(self):
        raw = self.command(["docker", "ps", "-a", "--no-trunc", "--filter", "volume=" + REDIS_VOLUME,
                            "--format", "{{.ID}}"]).decode("ascii").splitlines()
        need(all(B.SHA.fullmatch(value) for value in raw) and len(raw) == len(set(raw)),
             "committed_invalid_docker_inventory")
        return set(raw)


def file_identity(identity):
    return {key: value for key, value in identity.items() if key != "path"}


class NativeContinuation:
    def __init__(self, entry, runtime, lifecycle_fd, admitted, *, engine_id, io=None):
        need(sys.platform == "darwin" and os.getuid() == OWNER_UID, "committed_native_owner_uid_required")
        need(isinstance(engine_id, str) and bool(engine_id), "committed_fixed_engine_required")
        self.entry, self.admitted, self.engine_id = entry, admitted, engine_id
        self.runtime = B.directory(Path(runtime))
        self.lifecycle_fd = lifecycle_fd
        self.lifecycle_path = self.runtime / "data/locks/prod.lock"
        self.io = io or CommittedIO()
        self.root = B.directory(Path(self.io.root))
        need(self.root == Path(entry.OWNER_PATH).parents[2]
             and stat.S_IMODE(self.root.lstat().st_mode) == 0o700, "committed_private_owner_root_changed")
        self.t = B.AppBinding({}, self.runtime, lifecycle_fd, self.io)
        self.t.id = entry.TRANSACTION
        self.backup = B.directory(self.t.owner_directory / ("app-binding-" + self.t.id))
        need(stat.S_IMODE(self.backup.lstat().st_mode) == 0o700
             and self.backup.lstat().st_uid == os.getuid(), "committed_private_archive_changed")
        self.t.backup = self.backup
        self.held, self.pins, self.released = {}, {}, set()
        self.fences, self.owner_after = {}, None
        self.archived_pending, self.archived_job = False, False
        self.journal_fd = None
        self.journal_raw = None
        self.archive = self.backup / ARCHIVE
        self.pending_archive = self.archive / "startup.pending.archived.json"
        self.job = None
        self.observed_scope_digest = None

    def registration(self):
        need(self.entry.digest(Path(__file__).read_bytes()) == self.admitted.driver_sha256,
             "committed_native_driver_source_mismatch")
        return self.admitted

    def lifecycle(self):
        current, held = self.lifecycle_path.lstat(), os.fstat(self.lifecycle_fd)
        need(self.lifecycle_fd >= 3 and stat.S_ISREG(held.st_mode) and held.st_uid == os.getuid()
             and stat.S_IMODE(held.st_mode) == 0o600 and (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino),
             "committed_existing_prod_lock_identity_required")
        probe = os.open(self.lifecycle_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(probe)
            need((info.st_dev, info.st_ino) == (held.st_dev, held.st_ino), "committed_prod_lock_drift")
            try:
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise B.BindingError("committed_existing_prod_lock_exclusive_required")
        finally:
            os.close(probe)
        return f"{held.st_dev}:{held.st_ino}"

    hold = Old.Incident.hold
    check_locks = Old.Incident.check_locks

    def lease(self):
        self.check_locks(); lifecycle = self.lifecycle()
        need(self.held and self.fences, "committed_native_remaining_locks_required")
        identity = {"prod": lifecycle, "held": [value for fd, value in sorted(self.held.items())
                                              if fd != self.journal_fd]}
        return self.entry.Lease(self.entry.digest_json(identity), self.entry.LOCKS)

    def remember(self, path, expected=None, mode=0o600):
        raw, identity = pin(path, mode)
        need(expected is None or identity["sha256"] == expected, "committed_reviewed_evidence_changed")
        self.pins[path] = identity
        return raw

    def _read_configs(self):
        e, t = self.entry, self.t
        configs = {name: self.remember(self.root / name if name == "consumer-admission.json"
                                      else t.owner_directory / name, digest)
                   for name, digest in e.CONFIGS.items()}
        t.config, t.registry, t.installed, t.consumers = [
            parse(configs[name]) for name in ("owner.json", "registry.json", "installation.json", "consumer-admission.json")]
        identity, app = t.installed["identity"], t.config["services"]["app"]
        self.engine = app["docker"]["engine_id"]
        need(self.engine == self.engine_id, "committed_fixed_engine_changed")
        self.target = app["docker"]["targets"][0]
        need(t.installed.get("state") == "ready" and identity.get("environment") == t.config.get("environment") == "prod"
             and identity.get("owner_uid") == os.getuid() and t.config.get("enabled") is True
             and t.installed["owner_config_sha256"] == B.sha(configs["owner.json"])
             and identity["registry_digest"] == B.sha(B.encoded(t.registry))
             and identity["consumer_config_digest"] == B.sha(B.encoded(t.consumers))
             and app.get("platform") == "docker_container" and app.get("actions") == ["status"]
             and len(app["docker"]["targets"]) == 1 and app["lifecycle_lock"] == str(self.lifecycle_path)
             and t.registry["services"]["app"]["docker"] == app["docker"]
             and t.registry["services"]["app"]["revision"] == app["revision"]
             and self.target["id"] == e.APP_ID and self.target["image_id"] == e.APP_IMAGE_ID,
             "committed_registry_or_app_identity_changed")
        expected = Path(e.OWNER_PATH)
        need(t.config["executable"] == str(expected) and t.config["driver_directory"] == str(expected.parent)
             and t.config["journal_file"] == str(t.journal) and t.config["resource_lock_directory"] == str(t.resources)
             and t.consumers["resource_lock_directory"] == str(t.resources)
             and t.config["listen"].rsplit(":", 1)[1] == "8120", "committed_owner_layout_changed")
        manifest = parse(self.remember(expected.parent / "manifest.json"))
        need(identity["artifact_digest"] == B.sha(B.encoded(manifest)) and manifest.get("source_dirty") is False
             and manifest["source_commit"] == identity["source_commit"] and manifest["version"] == identity["version"]
             and manifest.get("platform") == "darwin/arm64"
             and manifest["files"].get(expected.name) == e.OWNER_BINARY, "committed_native_artifact_changed")
        for name, expected_sha in manifest["files"].items():
            need(Path(name).name == name and B.SHA.fullmatch(expected_sha), "committed_native_artifact_changed")
            path = expected.parent / name
            mode = stat.S_IMODE(path.lstat().st_mode)
            need(mode in {0o600, 0o700}, "committed_unsafe_native_artifact")
            self.remember(path, expected_sha, mode)
        self.remember(expected, e.OWNER_BINARY, 0o700)
        plist = plistlib.loads(self.remember(t.plist, t.installed["login_agent_sha256"]))
        self.remember(Path(t.installed["login_agent_path"]), t.installed["login_agent_sha256"])
        need(plist["Label"] == B.LABEL and plist["ProgramArguments"] ==
             [str(expected), "--config", str(t.owner_directory / "owner.json")]
             and plist.get("RunAtLoad") is True and plist.get("KeepAlive") is True
             and "UserName" not in plist and "GroupName" not in plist, "committed_launchagent_contract_changed")

    def _read_evidence(self):
        e, t = self.entry, self.t
        for name, digest in e.FILES.items():
            self.remember(self.backup / name, digest)
        authorized = parse(self.remember(self.backup / "authorized.json", AUTHORIZED_SHA))
        t.owner_before = authorized["owner_before"]
        need(authorized["transaction_id"] == t.id and t.owner_before["pid"] == 864
             and t.owner_before["kernel_path"] == e.OWNER_PATH
             and t.owner_before["binary_sha256"] == e.OWNER_BINARY
             and t.owner_before["uid_start"].split(maxsplit=1)[0] == str(os.getuid()),
             "committed_reviewed_prior_owner_changed")
        helper = self.root / "service-consumer-admission.py"
        self.remember(helper, authorized["pins"][str(helper)], 0o700)
        pending, self.pending_pin = pin(t.pending)
        marker = parse(pending)
        need(self.pending_pin["sha256"] == e.PENDING_SHA and marker["transaction_id"] == t.id
             and marker["backup"] == str(self.backup) and marker["kind"] == "active-app-binding",
             "committed_exact_startup_pending_required")
        self.pending_raw = pending
        old_key = marker["old_app_key"]
        new_key = B.resource_keys(t.config["services"]["app"])[0]
        scope = t.consumers["consumers"][B.CONSUMER]["resource_keys"]
        keys = set(scope) | {key for service in t.config["services"].values() for key in B.resource_keys(service)}
        need(old_key not in keys and new_key in keys and 0 < len(keys) < 32, "committed_resource_scope_changed")
        old_keys = (keys - {new_key}) | {old_key}
        states = {str(t.prefix(key).with_suffix(".state.json")) for key in old_keys}
        need(set(authorized["state_pins"]) == states, "committed_original_intent_scope_changed")
        for path, expected in authorized["state_pins"].items():
            self.remember(Path(path), expected)
        new_state = parse(self.remember(t.prefix(new_key).with_suffix(".state.json")))
        need(new_state.get("resource_key") == new_key and new_state.get("desired_state") == "running",
             "committed_new_app_intent_changed")
        for key in sorted(keys | {old_key}):
            self.hold(t.prefix(key).with_suffix(".lock"))
            path = t.prefix(key).with_suffix(".pending")
            raw, identity = pin(path)
            need(raw == (t.id + "\n").encode(), "committed_foreign_resource_fence")
            self.fences[path] = identity
        jobs = B.directory(t.resources / "consumer-jobs")
        actual = set(jobs.iterdir())
        if actual:
            self.job = jobs / (t.id + ".json")
            need(actual == {self.job}, "committed_foreign_consumer_job")
            raw, self.job_pin = pin(self.job)
            job = parse(raw)
            need(job["job_id"] == t.id and job["consumer_id"] == B.CONSUMER
                 and job["resource_keys"] == sorted(scope), "committed_foreign_consumer_job")
        need(not os.path.lexists(self.archive), "committed_continuation_archive_exists")

    def _check_job(self):
        actual = set((self.t.resources / "consumer-jobs").iterdir())
        need(actual == (set() if self.job is None or self.archived_job else {self.job}),
             "committed_foreign_consumer_job")
        if self.job is not None:
            path = self.archive / "consumer-job.archived.json" if self.archived_job else self.job
            need(file_identity(pin(path)[1]) == file_identity(self.job_pin), "committed_consumer_job_changed")

    @contextlib.contextmanager
    def hold_remaining(self):
        self.lifecycle()
        try:
            self.hold(self.t.owner_directory / "owner-upgrade.lock")
            self.hold(self.root / "consumer-helper-upgrade.lock")
            self._read_configs()
            self._read_evidence()
            self.journal_fd = self.hold(self.t.journal)
            self.journal_raw, self.journal_pin = pin(self.t.journal)
            prior = self.remember(self.backup / "operations-after-pause.jsonl")
            need(self.journal_raw.startswith(prior), "committed_journal_prefix_changed")
            B.journal_idle(prior)
            B.journal_idle(self.journal_raw)
            self.io.inactive_job(self.t)
            self.io.require_no_listener(self.t)
            yield
        finally:
            Old.Incident.close(self)
            self.journal_fd = None

    def _runtime(self):
        e, t = self.entry, self.t
        need(self.io.engine(t) == self.engine and self.io.volume_ids() == {REDIS_ID},
             "committed_docker_engine_or_redis_volume_changed")
        facts = {}
        app_image = f"localhost:9290/{e.gate.PRODUCT}/prod/1.1.27@{e.APP_DIGEST}"
        for role, cid, image in (("app", e.APP_ID, app_image), ("prod-redis", REDIS_ID, REDIS_IMAGE),
                                 ("gateway", GATEWAY_ID, GATEWAY_IMAGE)):
            keys = {"com.docker.compose.project", "com.docker.compose.service"}
            if role == "app":
                keys.update(self.target["labels"])
            need(self.io.role_ids(role) == {cid}, "committed_docker_role_changed")
            item = self.io.container(cid, sorted(keys))
            state = item["State"]
            labels = item["Config"]["Labels"]
            need(item["Id"] == cid and B.DIGEST.fullmatch(item["Image"]) and item["Config"]["Image"] == image
                 and labels["com.docker.compose.project"] == PROJECT and labels["com.docker.compose.service"] == role
                 and state["Running"] is True and not any(state[name] for name in ("Restarting", "Dead", "OOMKilled", "Paused"))
                 and state["Health"]["Status"] == "healthy" and isinstance(state["StartedAt"], str) and state["StartedAt"]
                 and type(item["RestartCount"]) is int and item["RestartCount"] >= 0, "committed_docker_role_unhealthy_or_changed")
            if role == "app":
                t.validate_target(item, self.target)
            elif role == "prod-redis":
                mount = [m for m in item["Mounts"] if m["Destination"] == "/data"]
                need(len(mount) == 1 and mount[0]["Type"] == "volume" and mount[0]["Name"] == REDIS_VOLUME
                     and mount[0]["RW"] is True, "committed_original_redis_volume_changed")
            facts[role] = self.entry.digest_json(item)
        return facts

    def scope(self):
        self.lease()
        for path, expected in self.pins.items():
            need(pin(path, expected["mode"])[1] == expected, "committed_reviewed_artifact_identity_changed")
        self._check_job()
        runtime = {"roles": self._runtime(), "evidence": {str(path): value for path, value in self.pins.items()},
                   "journal": self.journal_pin, "consumer_job": self.job_pin if self.job else None,
                   "startup_pending": self.pending_pin,
                   "resource_fences": {str(path): value for path, value in self.fences.items()}}
        self.lease()
        observed = {
            "transaction": self.t.id, "phase": "committed-installation.json", "engine": self.engine,
            "runtime_sha256": self.entry.digest_json(runtime), "app_container": self.entry.APP_ID,
            "app_digest": self.entry.APP_DIGEST, "app_image_id": self.entry.APP_IMAGE_ID, "app_healthy": True,
            "files": dict(self.entry.FILES), "configs": dict(self.entry.CONFIGS),
            "fences": {path.stem: identity["sha256"] for path, identity in self.fences.items()},
        }
        digest = self.entry.digest_json(observed)
        if self.observed_scope_digest is None:
            self.observed_scope_digest = digest
        need(digest == self.observed_scope_digest, "committed_native_scope_changed")
        return observed

    def pending(self):
        t = self.t
        self.check_locks()
        expected = [] if self.archived_pending else [t.pending]
        need(sorted(t.owner_directory.glob("*.pending.json")) == expected
             and not list(t.owner_directory.glob("*.lifecycle.pending")), "committed_foreign_installation_pending")
        path = self.pending_archive if self.archived_pending else t.pending
        _, identity = pin(path)
        need(file_identity(identity) == file_identity(self.pending_pin), "committed_startup_pending_drift")
        return None if self.archived_pending else identity["sha256"]

    def active_fences(self):
        self.check_locks()
        expected = set(self.fences) - self.released
        need(set(self.t.resources.glob("*.pending")) == expected, "committed_foreign_resource_fence")
        for path in expected:
            need(pin(path)[1] == self.fences[path], "committed_resource_fence_changed")
        return {path.stem: self.fences[path]["sha256"] for path in expected}

    def _check_deadline(self, deadline):
        need(datetime.now(timezone.utc) < deadline, "committed_native_review_expired")
        self.lease()

    def _archive_owned(self, path, archive, identity):
        need(pin(path)[1] == identity and not os.path.lexists(archive), "committed_owned_archive_drift")
        os.link(path, archive, follow_symlinks=False)
        B.sync(archive.parent)
        need(file_identity(pin(archive)[1]) == file_identity(identity) and pin(path)[1] == identity,
             "committed_owned_archive_drift")
        path.unlink()
        B.sync(path.parent)

    def resume_committed(self, transaction, expected_hash, deadline):
        t = self.t
        self._check_deadline(deadline)
        scope = self.scope()
        need(transaction == t.id and expected_hash == self.pending() and self.active_fences() == scope["fences"]
             and self.journal_fd is not None and self.owner_after is None
             and pin(t.journal)[1] == self.journal_pin, "committed_resume_preconditions_changed")
        state = self.io.inactive_job(t)
        self.io.require_no_listener(t)
        # Retire only the exact idle registration while the startup marker
        # still prevents a competing spawn from becoming a ready owner.
        if state == "loaded-idle":
            self._check_deadline(deadline)
            self.io.bootout_idle(t)
        need(self.io.inactive_job(t) == "unloaded", "committed_managed_owner_not_unloaded")
        self.io.require_no_listener(t)
        self.archive.mkdir(mode=0o700)
        B.sync(self.archive.parent)
        self._check_deadline(deadline)
        try:
            self._archive_owned(t.pending, self.pending_archive, self.pending_pin)
            self.archived_pending = True
            journal_fd, self.journal_fd = self.journal_fd, None
            self.held.pop(journal_fd)
            os.close(journal_fd)
            t.backup = self.archive

            def before_bootstrap():
                self._check_deadline(deadline)
                need(self.scope() == scope and self.pending() is None and self.active_fences() == scope["fences"]
                     and self.io.inactive_job(t) == "unloaded" and pin(t.journal)[1] == self.journal_pin,
                     "committed_prebootstrap_scope_changed")
                self.io.require_no_listener(t)

            self.owner_after = self.io.resume(t, before_bootstrap=before_bootstrap)
        except BaseException:
            # Restore startup admission closed, without overwriting any file
            # or retrying launchd. Retain the claim, archive, and all fences.
            if not os.path.lexists(t.pending):
                need(file_identity(pin(self.pending_archive)[1]) == file_identity(self.pending_pin),
                     "committed_exact_pending_archive_unconfirmed")
                os.link(self.pending_archive, t.pending, follow_symlinks=False)
                B.sync(t.pending.parent)
            self.archived_pending = False
            raise

    def ready_owner(self):
        t = self.t
        self.lease()
        need(self.owner_after is not None and self.pending() is None, "committed_owner_not_resumed")
        identity = self.io.owner_proof(t, self.owner_after, self.journal_pin)
        for attempt in range(5):
            try:
                raw, current = pin(t.journal)
            except B.BindingError as error:
                if str(error) not in {"incident_input_changed", "file_changed"} or attempt == 4:
                    raise
            else:
                need((current["device"], current["inode"]) == (self.journal_pin["device"], self.journal_pin["inode"])
                     and raw.startswith(self.journal_raw), "committed_journal_prefix_changed")
                if not raw or raw.endswith(b"\n"):
                    B.journal_idle(raw)
                    break
                need(attempt < 4, "committed_journal_append_unconfirmed")
            time.sleep(0.02)
        uid, start = self.owner_after["uid_start"].split(maxsplit=1)
        need(identity["listener_pids"] == [self.owner_after["pid"]]
             and identity["same_pid_journal_open_handle"] is True and identity["journal_lifetime_exclusive_busy"] is True
             and self.io.owner(t) == self.owner_after, "committed_owner_generation_unconfirmed")
        return self.entry.OwnerProof(self.owner_after["pid"], start, int(uid), self.owner_after["kernel_path"],
                                     self.owner_after["binary_sha256"], identity["listener_pids"][0], True)

    def release_owned_fences(self, transaction, fences, proof, deadline):
        self._check_deadline(deadline)
        scope = self.scope()
        need(transaction == self.t.id and self.active_fences() == fences == scope["fences"]
             and self.ready_owner() == proof, "committed_fence_release_review_changed")
        B.create(self.archive / "release-intent.json", B.encoded({
            "transaction": transaction, "fences": fences, "owner_pid": proof.pid, "owner_started": proof.started,
        }) + b"\n")
        if self.job is not None:
            self._check_deadline(deadline)
            need(self.scope() == scope and self.ready_owner() == proof, "committed_owner_generation_unconfirmed")
            self._archive_owned(self.job, self.archive / "consumer-job.archived.json", self.job_pin)
            self.archived_job = True
        for path in sorted(self.fences):
            self._check_deadline(deadline)
            need(self.scope() == scope and self.ready_owner() == proof and path.stem in self.active_fences(),
                 "committed_fence_release_review_changed")
            path.unlink()
            self.released.add(path)
            B.sync(path.parent)
