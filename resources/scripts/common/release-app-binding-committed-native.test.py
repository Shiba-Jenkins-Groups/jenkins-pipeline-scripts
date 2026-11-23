"""Focused native #105 fixtures using real temporary files and locks only."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


Native = load("release-app-binding-committed-native")
Entry = load("release-app-binding-committed")
B = Native.B
ENGINE = "test-engine"
LABELS = {"com.docker.compose.project": Native.PROJECT, "com.docker.compose.service": "app"}


class IO:
    def __init__(self, root):
        self.root = root
        self.launch = "loaded-idle"
        self.events = []
        self.fail_resume = False
        self.owner_value = None
        self.roles = {"app": Entry.APP_ID, "prod-redis": Native.REDIS_ID, "gateway": Native.GATEWAY_ID}
        self.generation = "2026-11-23T14:51:20Z"

    def engine(self, _):
        return ENGINE

    def volume_ids(self):
        return {Native.REDIS_ID}

    def role_ids(self, role):
        return {self.roles[role]}

    def container(self, cid, keys):
        role = next(key for key, value in self.roles.items() if value == cid)
        if role == "app":
            image = f"localhost:9290/{Entry.gate.PRODUCT}/prod/1.1.27@{Entry.APP_DIGEST}"
            mount = {"Destination": "/app/data/db/app", "Type": "bind",
                     "Source": str(self.root.parent / "runtime/data/db/app"), "RW": True}
        elif role == "prod-redis":
            image = Native.REDIS_IMAGE
            mount = {"Destination": "/data", "Type": "volume", "Name": Native.REDIS_VOLUME, "RW": True}
        else:
            image, mount = Native.GATEWAY_IMAGE, None
        return {"Id": cid, "Image": Entry.APP_IMAGE_ID, "RestartCount": 0,
                "Config": {"Image": image, "Labels": dict(LABELS, **{"com.docker.compose.service": role})},
                "State": {"Running": True, "Restarting": False, "Dead": False, "OOMKilled": False, "Paused": False,
                          "StartedAt": self.generation, "Health": {"Status": "healthy"}},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
                "NetworkSettings": {"Ports": {"8090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8090"}]}},
                "Mounts": [mount] if mount else []}

    def inactive_job(self, _):
        B.require(self.launch in {"loaded-idle", "unloaded"}, "fixture_active_owner")
        return self.launch

    def require_no_listener(self, _):
        B.require(self.owner_value is None, "fixture_listener_present")

    def bootout_idle(self, transaction):
        B.require(self.inactive_job(transaction) == "loaded-idle", "fixture_wrong_bootout")
        self.require_no_listener(transaction)
        self.events.append("bootout-idle")
        self.launch = "unloaded"

    def resume(self, t, *, before_bootstrap):
        before_bootstrap()
        self.events.append("bootstrap")
        if self.fail_resume:
            raise B.BindingError("fixture_resume_rejected")
        self.owner_value = {"pid": 12345, "uid_start": f"{os.getuid()} fixture-start",
                            "binary_sha256": Entry.OWNER_BINARY, "kernel_path": Entry.OWNER_PATH}
        return self.owner_value.copy()

    def owner(self, _):
        return self.owner_value

    def owner_proof(self, _t, expected, journal_pin):
        B.require(self.owner_value == expected, "fixture_ready_changed")
        return {"listener_pids": [expected["pid"]], "same_pid_journal_open_handle": True,
                "journal_lifetime_exclusive_busy": True}


class NativeCommittedTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # B.directory requires a real path with no unsafe ancestor, so use
        # the user-private temp directory, not the sticky /tmp alias.
        home_tmp = Path.home().resolve()
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="native-105-", dir=home_tmp)))
        self.root = self.base / "owner-root"
        self.prod = self.root / "prod"
        self.resources = self.root / "resource-locks"
        self.runtime = self.base / "runtime"
        self.tx = self.prod / ("app-binding-" + Entry.TRANSACTION)
        self.exe = self.root / "releases/fixture/owner-darwin-arm64"
        self.plist = self.root / (B.LABEL + ".plist")
        self.pending = self.prod / "owner-upgrade.pending.json"
        self.old_key = "docker:" + ENGINE + ":" + "a" * 64
        self.new_key = "docker:" + ENGINE + ":" + Entry.APP_ID
        self.job = self.resources / "consumer-jobs" / (Entry.TRANSACTION + ".json")
        self.lock = self.runtime / "data/locks/prod.lock"
        for path in (self.tx, self.job.parent, self.exe.parent, self.lock.parent):
            path.mkdir(parents=True, mode=0o700)
        self.root.chmod(0o700)
        self.write(self.exe, b"synthetic resident executable", mode=0o700)
        owner_sha = B.sha(self.exe.read_bytes())
        manifest = dict(source_dirty=False, source_commit="1" * 40, version="test", platform="darwin/arm64",
                        files={self.exe.name: owner_sha})
        self.json(self.exe.parent / "manifest.json", manifest)
        config = {"environment": "prod", "enabled": True, "executable": str(self.exe),
                  "driver_directory": str(self.exe.parent), "journal_file": str(self.prod / "operations.jsonl"),
                  "resource_lock_directory": str(self.resources), "listen": "127.0.0.1:8120",
                  "services": {"app": {"platform": "docker_container", "actions": ["status"], "revision": 7,
                                       "lifecycle_lock": str(self.lock), "docker": {"engine_id": ENGINE,
                                           "targets": [{"id": Entry.APP_ID, "image_id": Entry.APP_IMAGE_ID, "labels": LABELS,
                                                        "restart_policy": "unless-stopped"}]}}}}
        registry = {"services": config["services"]}
        consumers = {"resource_lock_directory": str(self.resources),
                     "consumers": {B.CONSUMER: {"resource_keys": [self.new_key]}}}
        self.write(self.plist, plistlib.dumps({"Label": B.LABEL, "ProgramArguments": [str(self.exe), "--config", str(self.prod / "owner.json")],
                                             "RunAtLoad": True, "KeepAlive": True}))
        config_sha = self.json(self.prod / "owner.json", config)
        configs = {"owner.json": config_sha, "registry.json": self.json(self.prod / "registry.json", registry),
                   "consumer-admission.json": self.json(self.root / "consumer-admission.json", consumers)}
        configs["installation.json"] = self.json(self.prod / "installation.json", {
            "state": "ready", "owner_config_sha256": config_sha, "login_agent_sha256": B.sha(self.plist.read_bytes()),
            "login_agent_path": str(self.plist),
            "identity": {"environment": "prod", "owner_uid": os.getuid(), "registry_digest": B.sha(B.encoded(registry)),
                         "consumer_config_digest": B.sha(B.encoded(consumers)), "source_commit": manifest["source_commit"],
                         "version": manifest["version"], "artifact_digest": B.sha(B.encoded(manifest))}})
        helper = self.root / "service-consumer-admission.py"
        self.write(helper, b"fixture helper", mode=0o700)
        old_state, new_state = [self.resources / (B.sha(key.encode()) + ".state.json") for key in (self.old_key, self.new_key)]
        old_state_sha = self.json(old_state, {"resource_key": self.old_key, "desired_state": "running"})
        self.json(new_state, {"resource_key": self.new_key, "desired_state": "running"})
        self.fence_paths = []
        for key in (self.old_key, self.new_key):
            prefix = self.resources / B.sha(key.encode())
            self.write(prefix.with_suffix(".lock"), b"")
            self.fence_paths.append(prefix.with_suffix(".pending"))
            self.write(self.fence_paths[-1], (Entry.TRANSACTION + "\n").encode())
        self.json(self.job, {"job_id": Entry.TRANSACTION, "consumer_id": B.CONSUMER, "resource_keys": [self.new_key]})
        authorized = {"transaction_id": Entry.TRANSACTION, "owner_before": {"pid": 864, "kernel_path": str(self.exe),
                     "binary_sha256": owner_sha, "uid_start": f"{os.getuid()} fixture-old-start"},
                      "pins": {str(helper): B.sha(helper.read_bytes())}, "state_pins": {str(old_state): old_state_sha}}
        auth_sha = self.json(self.tx / "authorized.json", authorized)
        pending_sha = self.json(self.pending, {"transaction_id": Entry.TRANSACTION, "backup": str(self.tx),
                                               "kind": "active-app-binding", "old_app_key": self.old_key})
        original_archive = self.tx / "owner-upgrade.pending.committed.json"
        self.write(original_archive, self.pending.read_bytes())
        files = {original_archive.name: pending_sha}
        for name in ("recovery-105-attempt.json", "recovery-105-failed.json", "commit-authorized.json"):
            files[name] = self.json(self.tx / name, {"transaction_id": Entry.TRANSACTION})
        self.write(self.prod / "operations.jsonl", b"")
        self.write(self.tx / "operations-after-pause.jsonl", b"")
        for path in (self.lock, self.prod / "owner-upgrade.lock", self.root / "consumer-helper-upgrade.lock"):
            self.write(path, b"")
        self.fd = os.open(self.lock, os.O_RDWR)
        self.stack.callback(os.close, self.fd)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.stack.enter_context(patch.multiple(Entry, OWNER_PATH=str(self.exe), OWNER_BINARY=owner_sha,
                                                 PENDING_SHA=pending_sha, FILES=files, CONFIGS=configs))
        self.stack.enter_context(patch.multiple(Native, AUTHORIZED_SHA=auth_sha, OWNER_UID=os.getuid()))
        self.stack.enter_context(patch.object(sys, "platform", "darwin"))
        self.io = IO(self.root)
        self.admission = Entry.Admission("a" * 40, Entry.digest(Path(Native.__file__).read_bytes()))
        self.native = Native.NativeContinuation(Entry, self.runtime, self.fd, self.admission, engine_id=ENGINE, io=self.io)
        self.deadline = datetime.now(timezone.utc) + timedelta(minutes=2)

    def write(self, path, data, mode=0o600):
        path.write_bytes(data)
        path.chmod(mode)

    def json(self, path, value):
        data = B.encoded(value) + b"\n"
        self.write(path, data)
        return B.sha(data)

    def test_managed_idle_job_resumes_once_then_retires_only_owned_fences(self):
        with self.native.hold_remaining():
            scope = self.native.scope()
            self.assertEqual(self.native.pending(), Entry.PENDING_SHA)
            self.assertFalse(self.native.archive.exists())
            self.assertEqual(self.io.events, [])
            self.native.resume_committed(Entry.TRANSACTION, Entry.PENDING_SHA, self.deadline)
            self.assertEqual(self.native.scope(), scope)
            self.assertEqual(self.native.active_fences(), scope["fences"])
            proof = self.native.ready_owner()
            self.assertEqual(proof.uid, os.getuid())
            self.native.release_owned_fences(Entry.TRANSACTION, scope["fences"], proof, self.deadline)
            self.assertEqual(self.native.scope(), scope)
            self.assertEqual(self.native.active_fences(), {})
            self.assertIsNone(self.native.pending())
            self.assertEqual(self.io.events, ["bootout-idle", "bootstrap"])
            self.assertFalse(self.job.exists())
            self.assertEqual((self.tx / "owner-upgrade.pending.committed.json").read_bytes(),
                             self.native.pending_archive.read_bytes())

    def test_rejected_bootstrap_restores_exact_startup_marker_and_keeps_fences(self):
        self.io.fail_resume = True
        inode = self.pending.stat().st_ino
        with self.native.hold_remaining():
            scope = self.native.scope()
            with self.assertRaisesRegex(B.BindingError, "fixture_resume_rejected"):
                self.native.resume_committed(Entry.TRANSACTION, Entry.PENDING_SHA, self.deadline)
            self.assertEqual(self.native.pending(), Entry.PENDING_SHA)
            self.assertEqual(self.pending.stat().st_ino, inode)
            self.assertEqual(self.native.active_fences(), scope["fences"])
            self.assertTrue(self.job.exists())
        self.assertEqual(self.io.events, ["bootout-idle", "bootstrap"])

    def test_active_owner_does_not_start_recovery(self):
        self.io.launch = "active"
        with self.assertRaisesRegex(B.BindingError, "fixture_active_owner"):
            with self.native.hold_remaining():
                self.fail("active job reached the signed claim")
        self.assertFalse(self.native.archive.exists())
        self.assertEqual(self.io.events, [])

    def test_foreign_fence_or_runtime_drift_do_not_start_recovery(self):
        with self.native.hold_remaining():
            scope = self.native.scope()
            foreign = self.resources / ("b" * 64 + ".pending")
            self.write(foreign, b"foreign")
            with self.assertRaisesRegex(B.BindingError, "committed_foreign_resource_fence"):
                self.native.active_fences()
            foreign.unlink()
            self.io.generation = "changed fixture generation"
            with self.assertRaisesRegex(B.BindingError, "committed_native_scope_changed"):
                self.native.resume_committed(Entry.TRANSACTION, Entry.PENDING_SHA, self.deadline)
            self.assertEqual(self.io.events, [])
            self.assertFalse(self.native.archive.exists())

    def test_same_bytes_on_new_startup_marker_inode_change_review_scope(self):
        with self.native.hold_remaining():
            before = self.native.scope()
        replacement = self.pending.with_name("replacement.fixture")
        self.write(replacement, self.pending.read_bytes())
        os.replace(replacement, self.pending)
        new_reader = Native.NativeContinuation(Entry, self.runtime, self.fd, self.admission, engine_id=ENGINE, io=self.io)
        with new_reader.hold_remaining():
            self.assertEqual(new_reader.pending(), Entry.PENDING_SHA)
            self.assertNotEqual(before, new_reader.scope())
            self.assertEqual(self.io.events, [])
            self.assertFalse(new_reader.archive.exists())

    def test_launchctl_idle_parser_requires_exact_managed_job_without_pid(self):
        t = SimpleNamespace(domain=f"gui/{os.getuid()}/{B.LABEL}", plist=self.plist,
                            config={"executable": str(self.exe)})
        header = (f"{t.domain} = {{\n\tactive count = 0\n\tpath = {t.plist}\n\ttype = LaunchAgent\n"
                  f"\tstate = spawn scheduled\n\tprogram = {self.exe}\n")
        io = Native.CommittedIO()
        samples = ((header + "}\n", True), (header + "\tpid = 42\n}\n", False),
                   (header.replace(str(self.exe), "/foreign/path") + "}\n", False))
        for content, expected in samples:
            result = subprocess.CompletedProcess([], 0, content.encode(), b"")
            with self.subTest(expected=expected), patch.object(Native.subprocess, "run", return_value=result):
                if expected:
                    self.assertEqual(io.inactive_job(t), "loaded-idle")
                else:
                    with self.assertRaisesRegex(B.BindingError, "committed_owner_not_provably_inactive"):
                        io.inactive_job(t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
