#!/usr/bin/env python3
"""LOCAL native binding fixtures: no real Docker, launchd, network or DB."""
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("binding", Path(__file__).with_name("release-app-binding.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)

HELPER = b'''import hashlib,json
from pathlib import Path
def check_admission(config,key):
    p=Path(config["resource_lock_directory"])/hashlib.sha256(key.encode()).hexdigest()
    if p.with_suffix(".pending").exists(): raise ValueError("pending")
    state=json.loads(p.with_suffix(".state.json").read_text())
    if state["resource_key"]!=key or state["desired_state"]!="running": raise ValueError("not running")
'''


def store(path, value, mode=0o600):
    raw = value if isinstance(value, bytes) else B.encoded(value) + b"\n"
    path.write_bytes(raw)
    path.chmod(mode)


class FakeIO:
    def __init__(self, root):
        self.root = root
        self.paused, self.pauses, self.resumes = False, 0, 0
        self.fault = None
        self.dependency_calls = []
        self.engine_id = "fixture-engine"
        self.image_id = "sha256:" + "e" * 64

    def owner(self, transaction):
        if self.paused:
            return None
        return {"pid": 100 + self.resumes, "kernel_path": transaction.config["executable"],
                "uid_start": str(os.getuid()) + " private-fixture-start",
                "binary_sha256": B.sha(B.read(Path(transaction.config["executable"]), 0o700))}

    def pause(self, transaction):
        assert self.owner(transaction) == transaction.owner_before
        self.pauses += 1
        self.paused = True

    def resume(self, transaction):
        if self.fault == "resume":
            raise B.BindingError("fixture_resume_failed")
        assert self.paused
        # All pointers and own job move to new scope before managed resume.
        config = B.document(self.root / "prod/owner.json")
        assert config["services"]["app"]["docker"]["targets"][0]["id"] == "b" * 64
        assert not transaction.pending.exists()
        assert len(transaction.owned_pending) > 0
        self.paused = False
        self.resumes += 1
        return self.owner(transaction)

    def app(self, transaction, container_id):
        old = copy.deepcopy(transaction.old_target)
        new = container_id != old["id"]
        labels = copy.deepcopy(old["labels"])
        if new:
            labels["org.opencontainers.image.revision"] = transaction.request["commit"]
        return {"Id": container_id, "Image": self.image_id if new else old["image_id"],
                "Config": {"Labels": labels, "Env": ["APP_VERSION=" + transaction.request["version"], "BRANCH=prod"]},
                "State": {"Running": True, "Restarting": False, "Health": {"Status": "healthy"}},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
                "NetworkSettings": {"Ports": {"8090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8090"}]}},
                "Mounts": [{"Destination": "/app/data/db/app", "Type": "bind", "RW": True,
                            "Source": str(transaction.runtime / "data/db/app")}]}

    def engine(self, transaction):
        return self.engine_id

    def image(self, transaction):
        return self.image_id

    def dependency_status(self, transaction, name):
        assert not self.paused and transaction.backup is None and not transaction.pending.exists()
        self.dependency_calls.append(name)
        return {"id": name, "environment": "prod", "registry_revision": transaction.config["services"][name]["revision"],
                "desired_state": "running", "process_state": "running",
                "contract_health": "unavailable" if self.fault == "dependency-" + name else "ready"}

    def checkpoint(self, phase, transaction):
        if phase == self.fault:
            raise OSError("secret-fixture-must-not-appear")
        if self.fault in {"race-before-pause", "race-after-pause"} and phase == (
                "authorized" if self.fault == "race-before-pause" else "owner-paused"):
            with transaction.journal.open("ab") as output:
                output.write(B.encoded({"type": "operation", "operation": {"id": "f" * 32, "state": "queued"}}) + b"\n")


class BindingTests(unittest.TestCase):
    def setUp(self):
        # CI /tmp may be world writable; use an isolated private child of the
        # checkout, whose ancestors satisfy the production file-policy checks.
        self.temp = tempfile.TemporaryDirectory(prefix=".binding-fixture-", dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.base.chmod(0o700)
        self.root, self.runtime = self.base / "owner", self.base / "app"
        self.prod, self.locks = self.root / "prod", self.root / "resource-locks"
        self.client = self.base / "client"
        release = self.root / "releases" / ("1.1.16-" + "1" * 40)
        for path in (self.prod / "credentials", self.prod / "consumers", self.locks / "consumer-jobs",
                     self.client, release, self.runtime / "data/locks", self.runtime / "data/db/app"):
            path.mkdir(parents=True, mode=0o700)
        for path in self.base.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
        store(self.runtime / "data/locks/prod.lock", b"")
        self.lifecycle = os.open(self.runtime / "data/locks/prod.lock", os.O_RDWR)
        fcntl.flock(self.lifecycle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, self.lifecycle)
        store(release / "owner-darwin-arm64", b"LOCAL fixture, never executable\n", 0o700)
        store(release / "service-consumer-admission.py", HELPER, 0o700)
        store(self.root / "service-consumer-admission.py", HELPER, 0o700)
        manifest = {"schema_version": 1, "version": "1.1.16", "source_commit": "1" * 40, "source_dirty": False,
                    "platform": "darwin/arm64", "files": {name: B.sha((release / name).read_bytes())
                     for name in ("owner-darwin-arm64", "service-consumer-admission.py")}}
        store(release / "manifest.json", manifest)
        services = {}
        for index, name in enumerate(sorted(B.SERVICES)):
            service = {"platform": "docker_container", "actions": ["status"], "revision": 3,
                       "resource_scope": "fixture", "consumers": ["app-prod"], "dependencies": [],
                       "lifecycle_lock": str(self.runtime / "data/locks/prod.lock"),
                       "docker": {"engine_id": "fixture-engine", "socket": "/fixture/no-socket",
                                  "manual_stop_verified": True, "stop_signal": "SIGTERM", "targets": [{
                                      "id": ("a" if name == "app" else str(index + 1)) * 64,
                                      "image_id": "sha256:" + "d" * 64, "restart_policy": "unless-stopped",
                                      "labels": {"com.docker.compose.project": "shiba-goditch-prod",
                                                 "com.docker.compose.service": name,
                                                 "org.opencontainers.image.revision": "0" * 40}}]}}
            services[name] = service
        self.config = {"enabled": True, "environment": "prod", "listen": "127.0.0.1:8120",
                       "executable": str(release / "owner-darwin-arm64"), "driver_directory": str(release),
                       "journal_file": str(self.prod / "operations.jsonl"), "resource_lock_directory": str(self.locks),
                       "principals": {"fixture": {"grants": ["unchanged"], "disabled": False}},
                       "recovery": {"services": ["ocrsvc"]}, "services": services}
        self.registry = {"schema_version": 1, "environment": "prod", "registry_revision": 4,
                         "services": copy.deepcopy(services), "evidence": {
                             name: {"verified": name in {"app", "ocrsvc", "recognition"},
                                    "checked_at": "2026-10-23T00:00:00Z", "note": "原始資格-" + name}
                             for name in services}}
        # The registry's historical App control proposal must NOT be used to
        # grant it: the actual installed owner stays explicitly status-only.
        self.registry["services"]["app"]["actions"] = ["status", "restart"]
        keys = {name: B.resource_keys(value)[0] for name, value in services.items()}
        self.consumers = {"schema_version": 1, "resource_lock_directory": str(self.locks), "consumers": {
            B.CONSUMER: {"resource_keys": sorted(keys[n] for n in ("app", "minio", "notify", "ollama", "omlx")),
                         "release_absent_paths": []},
            "backup-prod": {"resource_keys": sorted([keys["app"], keys["minio"]]),
                            "release_absent_paths": [str(self.runtime / "no-backup")],
                            "shared_lifecycle_locks": [str(self.runtime / "data/locks/prod.lock")]},
            "recognition-lifecycle-prod": {"resource_keys": sorted([keys["app"], keys["recognition"], keys["ocrsvc"]]),
                                           "release_absent_paths": []},
            # Deliberately valid, unsorted unrelated entry: no gratuitous
            # normalization is allowed during an App-only CID handoff.
            "app-lifecycle-dev": {"resource_keys": sorted([keys["omlx"], keys["ollama"]], reverse=True),
                                  "release_absent_paths": []}}}
        for key in keys.values():
            store(self.locks / (B.sha(key.encode()) + ".state.json"),
                  {"schema_version": 1, "resource_key": key, "desired_state": "running", "updated_at": "2026-10-23T00:00:00Z"})
        for path in (self.client / "client.json", self.client / "client-key.pem", self.prod / "credentials/server-key.pem",
                     self.prod / "drain.token", self.prod / "consumers/recognition-prod.token",
                     self.prod / "credentials-stage0.json"):
            store(path, b"private fixture bytes; never print\n")
        plist = plistlib.dumps({"Label": B.LABEL, "ProgramArguments": [self.config["executable"], "--config", str(self.prod / "owner.json")],
                               "RunAtLoad": True, "KeepAlive": True})
        store(self.root / (B.LABEL + ".plist"), plist)
        store(self.root / "login.plist", plist)
        self.installation = {"state": "ready", "identity": {"environment": "prod", "owner_uid": os.getuid(),
             "version": "1.1.16", "source_commit": "1" * 40, "artifact_digest": B.sha(B.encoded(manifest)),
             "registry_digest": B.sha(B.encoded(self.registry)), "consumer_config_digest": B.sha(B.encoded(self.consumers))},
             "owner_config_sha256": B.sha(B.encoded(self.config) + b"\n"), "consumer_helper_sha256": B.sha(HELPER),
             "credential_identity": {"host_root": str(self.root), "owner_directory": str(self.prod),
                                      "owner_uid": os.getuid(), "environment": "prod", "client_directory": str(self.client)},
             "client_config_sha256": B.sha((self.client / "client.json").read_bytes()),
             "credential_stage0_receipt_sha256": B.sha((self.prod / "credentials-stage0.json").read_bytes()),
             "login_agent_path": str(self.root / "login.plist"), "login_agent_sha256": B.sha(plist)}
        store(self.prod / "owner.json", self.config)
        store(self.prod / "registry.json", self.registry)
        store(self.prod / "installation.json", self.installation)
        store(self.root / "consumer-admission.json", self.consumers)
        store(self.prod / "operations.jsonl", B.encoded({"type": "operation", "operation": {"id": "7" * 32, "state": "succeeded"}}) + b"\n")
        self.io = FakeIO(self.root)
        self.request = {"commit": "2" * 40, "version": "1.1.16", "digest": "sha256:" + "f" * 64,
                        "image": "localhost:9290/shiba-go-ditch-api-project/prod/1.1.16@sha256:" + "f" * 64}
        self.observed = {"container_id": "b" * 64, "image_id": self.io.image_id, "commit": self.request["commit"],
                         "version": "1.1.16", "digest": self.request["digest"], "heartbeat": 200,
                         "compose_project": "shiba-goditch-prod"}

    def begin(self):
        tx = B.AppBinding.begin(self.request, self.runtime, self.lifecycle, io=self.io)
        self.addCleanup(tx.close)
        return tx

    def test_success_keeps_other_services_credentials_journal_and_old_intent(self):
        journal_before = (self.prod / "operations.jsonl").read_bytes()
        tx = self.begin()
        states_before = {path: path.read_bytes() for path in self.locks.glob("*.state.json")}
        pins_before = tx.source_pins()
        result = tx.finish(self.observed)
        self.assertEqual(result["status"], "completed")
        config, registry = B.document(self.prod / "owner.json"), B.document(self.prod / "registry.json")
        for name in B.SERVICES - {"app"}:
            self.assertEqual(config["services"][name], self.config["services"][name])
            self.assertEqual(registry["services"][name], self.registry["services"][name])
            self.assertEqual(registry["evidence"][name], self.registry["evidence"][name])
        self.assertEqual(config["principals"], self.config["principals"])
        self.assertEqual(config["recovery"], self.config["recovery"])
        self.assertEqual(config["services"]["app"]["actions"], ["status"])
        self.assertEqual(registry["services"]["app"]["actions"], ["status", "restart"])
        self.assertEqual(tx.source_pins(), pins_before)
        self.assertEqual((self.prod / "operations.jsonl").read_bytes(), journal_before)
        for path, data in states_before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(B.document(tx.prefix(tx.new_key).with_suffix(".state.json"))["desired_state"], "running")
        installed, consumers = B.document(self.prod / "installation.json"), B.document(self.root / "consumer-admission.json")
        self.assertEqual(installed["identity"]["registry_digest"], B.sha(B.encoded(registry)))
        self.assertEqual(installed["identity"]["consumer_config_digest"], B.sha(B.encoded(consumers)))
        self.assertEqual(installed["identity"]["artifact_digest"], self.installation["identity"]["artifact_digest"])
        for name, entry in consumers["consumers"].items():
            original = copy.deepcopy(self.consumers["consumers"][name])
            original["resource_keys"] = [tx.new_key if k == tx.old_key else k for k in original["resource_keys"]]
            self.assertEqual(entry, original)
        self.assertFalse(list(self.locks.glob("*.pending")))
        self.assertFalse(list((self.locks / "consumer-jobs").iterdir()))
        self.assertEqual((self.io.pauses, self.io.resumes), (1, 1))
        self.assertEqual(self.io.dependency_calls, ["ocrsvc", "recognition"])
        self.assertTrue((tx.backup / "completed.json").exists())

    def test_nested_old_lease_is_exact_and_lifecycle_ex_is_never_downgraded(self):
        tx = self.begin()
        env = tx.env_for_deploy({"TEST": "fixture"})
        descriptors = json.loads(env["SHIBA_SERVICE_CONTROL_ADMISSION_FDS"])
        self.assertEqual(set(descriptors), set(tx.scope))
        self.assertEqual(B.document(tx.job_path)["resource_keys"], tx.scope)
        self.assertEqual(env["SHIBA_SERVICE_CONTROL_JOB_ID"], tx.id)
        self.assertEqual(set(tx.pass_fds()), {self.lifecycle, *descriptors.values()})
        other = os.open(self.runtime / "data/locks/prod.lock", os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(other)
        self.assertTrue(tx.pending.exists())
        self.assertTrue(self.io.paused)

    def test_foreign_operation_race_before_and_after_pause_blocks_deploy(self):
        for fault, expected_pauses in (("race-before-pause", 0), ("race-after-pause", 1)):
            with self.subTest(fault=fault):
                # Each subcase uses the complete private fixture independently.
                fixture = BindingTests("test_success_keeps_other_services_credentials_journal_and_old_intent")
                fixture.setUp()
                try:
                    fixture.io.fault = fault
                    with self.assertRaises(B.BindingError) as error:
                        fixture.begin()
                    receipt = error.exception.receipt
                    self.assertEqual(receipt["status"], "pending-preserved")
                    self.assertEqual(fixture.io.pauses, expected_pauses)
                    self.assertFalse(list((fixture.locks / "consumer-jobs").iterdir()))
                    self.assertTrue((fixture.prod / "owner-upgrade.pending.json").exists())
                    self.assertEqual((fixture.prod / "owner.json").read_bytes(), B.encoded(fixture.config) + b"\n")
                finally:
                    fixture.doCleanups()

    def test_stopped_unknown_or_foreign_fence_never_overwritten(self):
        for desired in ("stopped", "unknown"):
            key = B.resource_keys(self.config["services"]["app"])[0]
            path = self.locks / (B.sha(key.encode()) + ".state.json")
            value = B.document(path)
            value["desired_state"] = desired
            store(path, value)
            original = path.read_bytes()
            with self.assertRaises(B.BindingError):
                self.begin()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.io.pauses, 0)
        value["desired_state"] = "running"
        store(path, value)
        pending = path.with_name(path.name.replace(".state.json", ".pending"))
        store(pending, b"foreign-owner\n")
        with self.assertRaises(B.BindingError):
            self.begin()
        self.assertEqual(pending.read_bytes(), b"foreign-owner\n")
        self.assertEqual(self.io.pauses, 0)

    def test_busy_shared_holder_blocks_before_pause_and_durable_transaction(self):
        key = B.resource_keys(self.config["services"]["app"])[0]
        path = self.locks / (B.sha(key.encode()) + ".lock")
        store(path, b"")
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaises(B.BindingError):
                self.begin()
            self.assertEqual(self.io.pauses, 0)
            self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())
        finally:
            os.close(fd)

    def test_partial_commit_preserves_fences_backup_and_refuses_blind_retry(self):
        tx = self.begin()
        self.io.fault = "committed-consumer-admission.json"
        with self.assertRaises(B.BindingError) as error:
            tx.finish(self.observed)
        self.assertNotIn("secret-fixture", json.dumps(error.exception.receipt))
        self.assertTrue(tx.pending.exists())
        self.assertTrue(tx.job_path.exists())
        self.assertTrue(tx.owned_pending[0].exists())
        self.assertEqual((tx.backup / "owner.json").read_bytes(), B.encoded(self.config) + b"\n")
        tx.close()
        with self.assertRaises(B.BindingError):
            self.begin()
        self.assertEqual(self.io.resumes, 0)

    def test_new_unknown_key_is_preserved_and_no_commit_occurs(self):
        tx = self.begin()
        new_key = "docker:fixture-engine:" + "b" * 64
        path = tx.prefix(new_key).with_suffix(".state.json")
        store(path, {"desired_state": "unknown", "evidence": "foreign"})
        with self.assertRaises(B.BindingError):
            tx.finish(self.observed)
        self.assertEqual(B.document(path)["desired_state"], "unknown")
        self.assertEqual((self.prod / "owner.json").read_bytes(), B.encoded(self.config) + b"\n")

    def test_wrong_observation_and_credential_drift_preserve_pending(self):
        tx = self.begin()
        bad = dict(self.observed, image_id="sha256:" + "0" * 64)
        with self.assertRaises(B.BindingError):
            tx.finish(bad)
        self.assertFalse(hasattr(tx, "new_key"))
        store(self.prod / "credentials/server-key.pem", b"foreign-private-material\n")
        with self.assertRaises(B.BindingError):
            tx.finish(self.observed)
        self.assertEqual(self.io.resumes, 0)
        self.assertTrue(tx.pending.exists())

    def test_resume_failure_keeps_startup_and_resource_admission_closed(self):
        tx = self.begin()
        self.io.fault = "resume"
        with self.assertRaises(B.BindingError):
            tx.finish(self.observed)
        self.assertTrue(tx.pending.exists())
        self.assertTrue(tx.job_path.exists())
        self.assertTrue(all(path.exists() for path in tx.owned_pending))
        self.assertFalse((tx.backup / "completed.json").exists())

    def test_missing_activated_global_registry_is_never_legacy(self):
        (self.root / "consumer-admission.json").unlink()
        with self.assertRaises(B.BindingError):
            self.begin()

    def test_unfenced_unknown_and_ambiguous_recovery_block_before_pending(self):
        cases = [{"type": "operation", "operation": {"id": "e" * 32, "state": "unknown"}}]
        cases += [{"type": "recovery", "recovery": {"service": "ocrsvc", "outcome": value}}
                  for value in ("attempting", "uncertain", "stopped", "")]
        original = (self.prod / "operations.jsonl").read_bytes()
        for record in cases:
            with self.subTest(record=record):
                store(self.prod / "operations.jsonl", original + B.encoded(record) + b"\n")
                with self.assertRaises(B.BindingError):
                    self.begin()
                self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())
                self.assertEqual(self.io.pauses, 0)

    def test_unavailable_dependency_blocks_before_pending_or_pause(self):
        for name in ("ocrsvc", "recognition"):
            with self.subTest(service=name):
                self.io.fault = "dependency-" + name
                with self.assertRaises(B.BindingError) as error:
                    self.begin()
                self.assertEqual(error.exception.receipt["code"], "deployment_dependency_not_ready")
                self.assertIsNone(error.exception.receipt["backup"])
                self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())
                self.assertFalse(list(self.locks.glob("*.pending")))
                self.assertEqual(self.io.pauses, 0)


if __name__ == "__main__":
    unittest.main()
