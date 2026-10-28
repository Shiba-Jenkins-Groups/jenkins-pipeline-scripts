#!/usr/bin/env python3
"""Ready-repair fixtures; never call real Docker, launchd, network or SQLite."""
import copy
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("binding_fixtures", Path(__file__).with_name("release-app-binding.test.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
B = F.B


class RepairIO(F.FakeIO):
    def __init__(self, root):
        super().__init__(root)
        self.old_present = False
        self.old_running = False
        self.old_foreign = False
        self.inventory_error = False
        self.inspect_ids = []
        self.phases = []
        self.transaction = None

    def inventory_ids(self, transaction):
        if self.inventory_error:
            raise B.BindingError("native_binding_command_failed")
        return {"b" * 64} | ({"a" * 64} if self.old_present else set())

    def app(self, transaction, container_id):
        self.inspect_ids.append(container_id)
        raw = super().app(transaction, container_id)
        if container_id == "a" * 64:
            raw["State"]["Running"] = self.old_running
            raw["HostConfig"]["PortBindings"] = raw["NetworkSettings"]["Ports"]
            raw["NetworkSettings"]["Ports"] = None  # Docker stops publishing an inactive port.
            if self.old_foreign:
                raw["Config"]["Labels"]["com.docker.compose.project"] = "foreign"
        return raw

    def checkpoint(self, phase, transaction):
        self.transaction = transaction
        self.phases.append(phase)
        # Repair must retain all exclusive leases and must never create an
        # admitted deployment child, even during pause and commit.
        assert not hasattr(transaction, "job_path")
        assert not list((transaction.resources / "consumer-jobs").iterdir())
        for key in transaction.fds:
            other = os.open(transaction.prefix(key).with_suffix(".lock"), os.O_RDWR)
            try:
                try:
                    fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("repair resource lease was downgraded")
            finally:
                os.close(other)
        if self.fault in {"pins-before-pause", "pins-after-pause"} and phase == (
                "authorized" if self.fault == "pins-before-pause" else "owner-paused"):
            path = transaction.owner_directory / "owner.json"
            path.write_bytes(path.read_bytes() + b" ")
        super().checkpoint(phase, transaction)


class RepairTests(unittest.TestCase):
    def setUp(self):
        F.BindingTests.setUp(self)
        self.io = RepairIO(self.root)
        self.request.update(operation_id="a0f62503-a78d-47d2-8c95-977e69edb586",
                            previous_container_id="a" * 64, previous_image_id="sha256:" + "d" * 64)
        self.pins = {name: B.sha((self.root / name if name == "consumer-admission.json" else self.prod / name).read_bytes())
                     for name in ("owner.json", "registry.json", "installation.json", "consumer-admission.json")}

    def repair(self):
        return B.AppBinding.repair(self.request, self.observed, self.runtime, self.lifecycle, self.pins, io=self.io)

    def assert_closed(self):
        if self.io.transaction is not None:
            self.assertEqual(self.io.transaction.lock_fds, [])
        os.fstat(self.lifecycle)  # The caller's App EX descriptor remains its property.

    def test_missing_old_container_repairs_only_binding_and_keeps_history(self):
        before_states = {path: path.read_bytes() for path in self.locks.glob("*.state.json")}
        before_journal = (self.prod / "operations.jsonl").read_bytes()
        result = self.repair()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["mode"], "ready-repair")
        self.assertEqual(result["transaction_id"], self.request["operation_id"])
        self.assertNotIn("a" * 64, self.io.inspect_ids)
        self.assertEqual((self.io.pauses, self.io.resumes), (1, 1))
        self.assertNotIn("deploy-admitted", self.io.phases)
        config, registry = B.document(self.prod / "owner.json"), B.document(self.prod / "registry.json")
        self.assertEqual(config["services"]["app"]["docker"]["targets"][0]["id"], "b" * 64)
        self.assertEqual(config["services"]["app"]["actions"], ["status"])
        self.assertEqual(registry["services"]["app"]["actions"], ["status", "restart"])
        for name in B.SERVICES - {"app"}:
            self.assertEqual(config["services"][name], self.config["services"][name])
            self.assertEqual(registry["services"][name], self.registry["services"][name])
        self.assertEqual(config["principals"], self.config["principals"])
        self.assertEqual(config["recovery"], self.config["recovery"])
        self.assertEqual((self.prod / "operations.jsonl").read_bytes(), before_journal)
        for path, data in before_states.items():
            self.assertEqual(path.read_bytes(), data)
        consumers, installed = B.document(self.root / "consumer-admission.json"), B.document(self.prod / "installation.json")
        for name, original in self.consumers["consumers"].items():
            expected = copy.deepcopy(original)
            expected["resource_keys"] = [result["new_app_key"] if key == result["old_app_key"] else key
                                         for key in original["resource_keys"]]
            self.assertEqual(consumers["consumers"][name], expected)
        self.assertEqual(installed["identity"]["artifact_digest"], self.installation["identity"]["artifact_digest"])
        self.assertEqual(installed["owner_config_sha256"], B.sha((self.prod / "owner.json").read_bytes()))
        self.assertEqual(installed["identity"]["registry_digest"], B.sha(B.encoded(registry)))
        self.assertEqual(installed["identity"]["consumer_config_digest"], B.sha(B.encoded(consumers)))
        backup = Path(result["backup"])
        for name in self.pins:
            self.assertEqual(B.sha((backup / name).read_bytes()), self.pins[name])
        self.assertFalse(list(self.locks.glob("*.pending")))
        self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())
        self.assert_closed()

    def test_exact_inactive_old_container_is_allowed_without_mutating_it(self):
        self.io.old_present = True
        result = self.repair()
        self.assertEqual(result["status"], "completed")
        self.assertIn("a" * 64, self.io.inspect_ids)
        self.assert_closed()

    def test_raw_binding_hash_mismatch_blocks_before_source_or_owner_action(self):
        for name in self.pins:
            with self.subTest(name=name):
                pins = dict(self.pins, **{name: "0" * 64})
                with self.assertRaises(B.BindingError) as error:
                    B.AppBinding.repair(self.request, self.observed, self.runtime, self.lifecycle, pins, io=self.io)
                self.assertEqual(error.exception.receipt["code"], "repair_binding_hash_mismatch")
                self.assertEqual(self.io.dependency_calls, [])
                self.assertEqual(self.io.inspect_ids, [])
                self.assertEqual(self.io.pauses, 0)
                self.assertFalse(list(self.prod.glob("app-binding-*")))

    def test_noncanonical_operation_or_previous_identity_is_rejected(self):
        for updates in ({"operation_id": "a" * 32}, {"operation_id": "A0F62503-A78D-47D2-8C95-977E69EDB586"},
                        {"operation_id": "a0f62503-a78d-17d2-8c95-977e69edb586"},
                        {"previous_container_id": "c" * 64}, {"previous_image_id": "sha256:" + "c" * 64}):
            with self.subTest(updates=updates):
                request = dict(self.request, **updates)
                with self.assertRaises(B.BindingError):
                    B.AppBinding.repair(request, self.observed, self.runtime, self.lifecycle, self.pins, io=self.io)
                self.assertEqual(self.io.pauses, 0)
                self.assertFalse(list(self.prod.glob("app-binding-*")))

    def test_inventory_error_is_never_interpreted_as_old_container_absence(self):
        self.io.inventory_error = True
        with self.assertRaises(B.BindingError) as error:
            self.repair()
        self.assertEqual(error.exception.receipt["code"], "native_binding_command_failed")
        self.assertEqual(self.io.pauses, 0)
        self.assertEqual(self.io.inspect_ids, [])
        self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())

    def test_present_running_or_foreign_old_container_blocks(self):
        self.io.old_present = True
        for attribute in ("old_running", "old_foreign"):
            with self.subTest(attribute=attribute):
                setattr(self.io, attribute, True)
                with self.assertRaises(B.BindingError):
                    self.repair()
                setattr(self.io, attribute, False)
                self.assertEqual(self.io.pauses, 0)
                self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())

    def test_stopped_unknown_new_intent_or_pending_is_never_cleared(self):
        key = B.resource_keys(self.config["services"]["app"])[0]
        path = self.locks / (B.sha(key.encode()) + ".state.json")
        original = path.read_bytes()
        for state in ("stopped", "unknown"):
            with self.subTest(state=state):
                F.store(path, {"schema_version": 1, "resource_key": key, "desired_state": state})
                before = path.read_bytes()
                with self.assertRaises(B.BindingError):
                    self.repair()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.io.pauses, 0)
        F.store(path, original)
        new = self.locks / (B.sha(("docker:fixture-engine:" + "b" * 64).encode()) + ".state.json")
        F.store(new, {"resource_key": "foreign", "desired_state": "unknown"})
        before = new.read_bytes()
        with self.assertRaises(B.BindingError):
            self.repair()
        self.assertEqual(new.read_bytes(), before)
        self.assertEqual(self.io.pauses, 0)
        new.unlink()
        pending = self.locks / (B.sha(key.encode()) + ".pending")
        F.store(pending, b"foreign-unknown\n")
        with self.assertRaises(B.BindingError):
            self.repair()
        self.assertEqual(pending.read_bytes(), b"foreign-unknown\n")
        self.assertEqual(self.io.pauses, 0)

    def test_unqualified_observation_blocks_before_owner_pause(self):
        self.observed["image_id"] = "sha256:" + "0" * 64
        with self.assertRaises(B.BindingError):
            self.repair()
        self.assertEqual(self.io.pauses, 0)

    def test_unknown_commit_preserves_barriers_history_and_closes_only_owned_fds(self):
        self.io.fault = "committed-owner.json"
        with self.assertRaises(B.BindingError) as error:
            self.repair()
        self.assertEqual(error.exception.receipt["status"], "pending-preserved")
        self.assertEqual(error.exception.receipt["phase"], "committed-owner.json")
        self.assertNotIn("secret-fixture", json.dumps(error.exception.receipt))
        self.assertTrue((self.prod / "owner-upgrade.pending.json").exists())
        self.assertTrue(list(self.locks.glob("*.pending")))
        self.assertTrue(list(self.prod.glob("app-binding-*/operations-after-pause.jsonl")))
        self.assertEqual(self.io.resumes, 0)
        self.assertFalse(list((self.locks / "consumer-jobs").iterdir()))
        self.assert_closed()
        with self.assertRaises(B.BindingError):
            self.repair()
        self.assertEqual(self.io.pauses, 1)

    def test_resume_failure_keeps_startup_and_resource_fences_without_rollback(self):
        self.io.fault = "resume"
        before_journal = (self.prod / "operations.jsonl").read_bytes()
        with self.assertRaises(B.BindingError) as error:
            self.repair()
        self.assertEqual(error.exception.receipt["code"], "fixture_resume_failed")
        self.assertEqual(error.exception.receipt["owner_state"], "paused")
        self.assertFalse(error.exception.receipt["automatic_retry"])
        self.assertFalse(error.exception.receipt["automatic_rollback"])
        self.assertTrue((self.prod / "owner-upgrade.pending.json").exists())
        self.assertTrue(list(self.locks.glob("*.pending")))
        self.assertEqual(B.document(self.prod / "owner.json")["services"]["app"]["docker"]["targets"][0]["id"], "b" * 64)
        self.assertEqual((self.prod / "operations.jsonl").read_bytes(), before_journal)
        self.assertEqual(self.io.resumes, 0)
        self.assert_closed()

    def test_busy_consumer_shared_lease_blocks_before_owner_pause(self):
        key = B.resource_keys(self.config["services"]["app"])[0]
        path = self.locks / (B.sha(key.encode()) + ".lock")
        F.store(path, b"")
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaises(B.BindingError):
                self.repair()
            self.assertEqual(self.io.pauses, 0)
            self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())
        finally:
            os.close(fd)

    def test_binding_pins_are_rechecked_on_both_sides_of_owner_pause(self):
        for fault, pauses in (("pins-before-pause", 0), ("pins-after-pause", 1)):
            with self.subTest(fault=fault):
                fixture = RepairTests("test_missing_old_container_repairs_only_binding_and_keeps_history")
                fixture.setUp()
                try:
                    fixture.io.fault = fault
                    with self.assertRaises(B.BindingError) as error:
                        fixture.repair()
                    self.assertEqual(error.exception.receipt["code"], "repair_binding_hash_mismatch")
                    self.assertEqual(fixture.io.pauses, pauses)
                    self.assertTrue((fixture.prod / "owner-upgrade.pending.json").exists())
                    fixture.assert_closed()
                finally:
                    fixture.doCleanups()

    def test_foreign_journal_race_remains_a_barrier_before_and_after_pause(self):
        for fault, pauses in (("race-before-pause", 0), ("race-after-pause", 1)):
            with self.subTest(fault=fault):
                fixture = RepairTests("test_missing_old_container_repairs_only_binding_and_keeps_history")
                fixture.setUp()
                try:
                    fixture.io.fault = fault
                    with self.assertRaises(B.BindingError):
                        fixture.repair()
                    self.assertEqual(fixture.io.pauses, pauses)
                    self.assertTrue((fixture.prod / "owner-upgrade.pending.json").exists())
                    fixture.assert_closed()
                finally:
                    fixture.doCleanups()

    def test_signed_runner_and_real_helper_commit_one_maintenance_without_deployment(self):
        self.composed_case()

    def test_late_completed_evidence_failure_reports_released_admission_and_cannot_replay(self):
        self.composed_case(late_error=True)

    def composed_case(self, late_error=False):
        runner_spec = importlib.util.spec_from_file_location(
            "composed_binding_repair_runner", Path(__file__).with_name("release-binding-repair.py"))
        R = importlib.util.module_from_spec(runner_spec)
        runner_spec.loader.exec_module(R)
        key = b"LOCAL-composed-fixture-only-HMAC-key-at-least-32-bytes"
        now = dt.datetime.now(dt.timezone.utc)
        issued = now - dt.timedelta(hours=2)
        library = "f" * 40
        original = {name: self.request[name] for name in ("commit", "version", "digest", "image")}
        original.update(schema_version=2, kind="deployment", product=R.gate.PRODUCT,
                        prod_job=R.gate.PRODUCT + "/prod", prod_build=213,
                        decision={"decision": "PASS"}, created_at=issued.isoformat(),
                        expires_at=(issued + dt.timedelta(minutes=15)).isoformat(),
                        evidence_sha256="e" * 64, library_revision="1" * 40,
                        deployment_script_revision=self.request["commit"])
        original["promotion"] = R.control.sign({"kind": "promotion", "status": "MERGED", "product": R.gate.PRODUCT,
            "source_commit": "a" * 40, "merge_commit": original["commit"], "version": original["version"],
            "develop_job": R.gate.PRODUCT + "/develop", "develop_build": 362}, key)
        original["finalization"] = R.control.sign({"kind": "finalization", "status": "SUCCESS", "product": R.gate.PRODUCT,
            "commit": original["commit"], "digest": original["digest"], "version": original["version"],
            "evidence_sha256": "e" * 64}, key)
        signed_original = R.control.sign(original, key)
        prior = {"schema_version": 1, "kind": "runtime-deployment", "product": R.gate.PRODUCT,
                 "status": "SUCCESS", "automatic_rollback": False, "request_sha256": R.sha(signed_original),
                 **{name: original[name] for name in ("commit", "version", "digest", "deployment_script_revision", "library_revision")},
                 "previous_container_id": self.request["previous_container_id"],
                 "previous_image_id": self.request["previous_image_id"], "observed": copy.deepcopy(self.observed),
                 "started_at": (issued + dt.timedelta(minutes=1)).isoformat(),
                 "finished_at": (issued + dt.timedelta(minutes=3)).isoformat()}
        prior_signed = R.control.sign(prior, key)
        payload = {"schema_version": 1, "kind": "app-binding-repair", "product": R.gate.PRODUCT,
                   "library_revision": library, "created_at": now.isoformat(),
                   "expires_at": (now + dt.timedelta(minutes=15)).isoformat(),
                   "operation_id": self.request["operation_id"], "authorized_by": "fixture-user",
                   "coordinator_build": 201, "binding_hashes": self.pins,
                   "original": {"coordinator_build": 200, "owner_build": 92,
                                "request": signed_original, "receipt": prior_signed}}
        signed = R.control.sign(payload, key)
        state_root = self.base / "maintenance-state"
        state_root.mkdir(mode=0o700)
        state = R.control.State(state_root)
        state.write(original["commit"], prior_signed)
        deployment_before = state.path(original["commit"]).read_bytes()
        # The runner acquires its own lifecycle EX. Its descriptor, and every
        # resource descriptor inside the real helper, must be real fixture locks.
        fcntl.flock(self.lifecycle, fcntl.LOCK_UN)
        real_loader = R.module
        def load_with_fixture_native_io(name, filename):
            module = real_loader(name, filename)
            self.assertEqual(filename, "release-app-binding.py")
            module.NativeIO = lambda: self.io
            if late_error:
                real_create = module.create
                def fail_only_completed(path, data):
                    if path.name == "completed.json":
                        raise OSError("private-fixture-receipt-write-failed")
                    return real_create(path, data)
                module.create = mock.Mock(side_effect=fail_only_completed)
            return module
        output = self.base / "signed-maintenance.json"
        with mock.patch.object(R, "module", side_effect=load_with_fixture_native_io), \
             mock.patch.object(R.sys, "platform", "darwin"), \
             mock.patch.object(R.deploy, "run", return_value=self.io.engine_id) as command, \
             mock.patch.object(R.deploy, "containers", return_value=[]), \
             mock.patch.object(R.deploy, "target_container", return_value={"Id": self.observed["container_id"],
                                                                          "Image": self.observed["image_id"]}), \
             mock.patch.object(R.deploy, "image_identity", return_value=self.observed["image_id"]):
            if late_error:
                with self.assertRaises(ValueError):
                    R.execute(signed, key, self.runtime, state, self.io.engine_id, now, library, output)
                signed_result = json.loads(output.read_bytes())
            else:
                signed_result = R.execute(signed, key, self.runtime, state, self.io.engine_id, now, library, output)
            command.assert_called_once_with(["docker", "info", "--format", "{{.ID}}"])
            result = R.control.verify(signed_result, key)
            self.assertFalse(result["app_deployed"])
            if late_error:
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["app_binding"]["status"], "outcome-unconfirmed")
                self.assertTrue(result["app_binding"]["binding_committed"])
                self.assertTrue(result["app_binding"]["admission_released"])
                self.assertFalse(result["app_binding"]["automatic_retry"])
                self.assertNotIn("private-fixture", json.dumps(result))
            else:
                self.assertEqual(result["status"], "SUCCESS")
                self.assertEqual(result["app_binding"]["mode"], "ready-repair")
            self.assertEqual(R.control.verify(json.loads(output.read_bytes()), key), result)
            self.assertEqual(state.read(R.state_key(payload)), signed_result)
            self.assertEqual(state.path(original["commit"]).read_bytes(), deployment_before)
            with self.assertRaisesRegex(ValueError, "already claimed"):
                R.execute(signed, key, self.runtime, state, self.io.engine_id, now, library, self.base / "retry.json")
            retry_payload = dict(payload, operation_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            with self.assertRaisesRegex(ValueError, "already claimed"):
                R.execute(R.control.sign(retry_payload, key), key, self.runtime, state, self.io.engine_id, now,
                          library, self.base / "retry-new-operation.json")
            command.assert_called_once_with(["docker", "info", "--format", "{{.ID}}"])
        self.assertEqual((self.io.pauses, self.io.resumes), (1, 1))
        self.assertNotIn("deploy-admitted", self.io.phases)
        for name in self.pins:
            path = self.root / name if name == "consumer-admission.json" else self.prod / name
            self.assertNotEqual(B.sha(path.read_bytes()), self.pins[name])
        self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())
        self.assertFalse(list(self.locks.glob("*.pending")))
        self.assert_closed()


class InventoryTests(unittest.TestCase):
    def test_inventory_requires_successful_full_exact_id_output(self):
        io = B.NativeIO()
        with mock.patch.object(io, "command", return_value=(("a" * 64) + "\n" + ("b" * 64) + "\n").encode()) as command:
            self.assertEqual(io.inventory_ids(None), {"a" * 64, "b" * 64})
            command.assert_called_once_with(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"])
        for raw in (b"short-id\n", (("a" * 64) + "\n" + ("a" * 64) + "\n").encode(), b" \n"):
            with self.subTest(raw=raw), mock.patch.object(io, "command", return_value=raw):
                with self.assertRaises(B.BindingError):
                    io.inventory_ids(None)
        with mock.patch.object(io, "command", side_effect=B.BindingError("native_binding_command_failed")):
            with self.assertRaises(B.BindingError):
                io.inventory_ids(None)


if __name__ == "__main__":
    unittest.main()
