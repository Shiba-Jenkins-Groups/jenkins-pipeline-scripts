#!/usr/bin/env python3
"""Synthetic pre-deploy producer/recovery fixtures: no live platform or APIs."""
import copy
import contextlib
import fcntl
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("incident_fixtures", HERE / "release-app-binding.test.py")
F = importlib.util.module_from_spec(spec); spec.loader.exec_module(F)
spec = importlib.util.spec_from_file_location("incident_under_test", HERE / "release-app-binding-incident.py")
I = importlib.util.module_from_spec(spec); spec.loader.exec_module(I)
B = I.B


class FailedPause(F.FakeIO):
    def pause(self, transaction):
        assert self.owner(transaction) == transaction.owner_before
        self.pauses += 1; self.paused = True
        raise F.B.BindingError("owner_kernel_path_unavailable")


class NativeReplay(I.IncidentIO):
    """Run the actual NativeIO.resume through a strict synthetic transport."""
    def __init__(self, fixture):
        self.fixture, self.root = fixture, fixture.root
        self.loaded = False; self.bootstraps = 0; self.journal_lease = None
        self.fault = None; self.append = "nonce"; self.before = fixture.authorized["owner_before"]

    def owner(self, transaction, **kwargs):
        if self.fault == "owner-unknown": raise B.BindingError("owner_launchd_observation_unknown")
        if not self.loaded: return None
        value = dict(self.before, pid=self.before["pid"] + 1, uid_start=str(os.getuid()) + " fixture-new-start")
        if self.fault == "foreign-pid": value["pid"] = self.before["pid"]
        if self.fault == "foreign-hash": value["binary_sha256"] = "0" * 64
        return value

    def engine(self, transaction): return "fixture-engine"

    def app_inventory(self, transaction):
        return {transaction.old_target["id"]} | ({"b" * 64} if self.fault == "replacement" else set())

    def app(self, transaction, cid):
        temporary = transaction.request
        transaction.request = self.fixture.request
        try: raw = F.FakeIO(self.root).app(transaction, cid)
        finally: transaction.request = temporary
        raw["State"].update(StartedAt="2026-10-29T00:00:00Z", Dead=False, OOMKilled=False, Paused=False)
        raw["RestartCount"] = 0
        if self.fault == "app-unhealthy": raw["State"]["Health"]["Status"] = "unhealthy"
        return raw

    def command(self, args):
        expected = ["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()), str(self.root / (B.LABEL + ".plist"))]
        assert args == expected, "only one managed bootstrap is permitted"
        self.bootstraps += 1
        assert self.bootstraps == 1
        assert not (self.root / "prod/owner-upgrade.pending.json").exists()
        for key in self.fixture.authorized["state_pins"]:
            pending = Path(key.replace(".state.json", ".pending"))
            assert pending.read_bytes() == (self.fixture.tx + "\n").encode()
            fd = os.open(pending.with_suffix(".lock"), os.O_RDWR)
            try:
                try: fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError: pass
                else: raise AssertionError("resource EX released before resume verification")
            finally: os.close(fd)
        if self.fault == "resume": raise B.BindingError("fixture_resume_failed")
        if self.fault == "timeout": raise subprocess.TimeoutExpired("fixture-only", 20)
        if self.fault == "interrupt": raise KeyboardInterrupt()
        self.journal_lease = os.open(self.root / "prod/operations.jsonl", os.O_RDWR)
        fcntl.flock(self.journal_lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (self.root / "prod/operations.jsonl").open("ab") as output:
            if self.append == "nonce": value = {"type": "nonce", "nonce": "fixture-normal-startup"}
            elif self.append == "recovery": value = {"type": "recovery", "recovery": {"service": "ocrsvc", "outcome": "failed", "stop_requested": False}}
            else: value = {"type": "operation", "operation": {"id": "f" * 32, "state": self.append}}
            output.write(B.encoded(value) + b"\n")
        self.loaded = True
        return b""

    def lsof(self, args, **kwargs):
        assert args[:2] == ["/usr/sbin/lsof", "-nP"]
        return subprocess.CompletedProcess(args, 0, stdout=str(self.before["pid"] + 1).encode(), stderr=b"")

    def close(self):
        if self.journal_lease is not None: os.close(self.journal_lease); self.journal_lease = None


class IncidentTests(unittest.TestCase):
    def setUp(self):
        F.BindingTests.setUp(self)
        producer = FailedPause(self.root)
        with self.assertRaises(F.B.BindingError) as failure:
            F.B.AppBinding.begin(self.request, self.runtime, self.lifecycle, io=producer)
        self.failure = failure.exception.receipt
        self.tx = self.failure["transaction_id"]
        self.backup = Path(self.failure["backup"])
        self.authorized = F.B.document(self.backup / "authorized.json")
        self.assertEqual((producer.pauses, producer.resumes), (1, 0))
        self.assertEqual(self.failure["phase"], "authorized")
        self.assertFalse((self.backup / "owner-paused.json").exists())
        fcntl.flock(self.lifecycle, fcntl.LOCK_UN)
        self.metadata = self.base / "incident-metadata.json"
        document = {"transaction_id": self.tx, "backup": str(self.backup), "authorized": self.authorized,
                    "failure": self.failure, "live_probe_performed": False, "credentials_exported": False,
                    "journal_records_exported": False, "backup_files": {name: {
                        "sha256": B.sha((self.backup / name).read_bytes()), "bytes": (self.backup / name).stat().st_size}
                        for name in I.ORIGINAL_FILES}}
        F.store(self.metadata, document)
        self.meta_sha = B.sha(self.metadata.read_bytes())
        self.io = NativeReplay(self); self.addCleanup(self.io.close)
        self.revision = "9" * 40
        self.source = lambda revision: {"revision": revision, "files": {name: B.sha((HERE / name).read_bytes()) for name in I.SOURCE_FILES}}
        self.collection = self.base / "collection.json"
        self.plan_path = self.base / "plan.json"
        self.claim = self.backup / I.CLAIM
        self.original_configs = {name: (self.root / name if name == "consumer-admission.json" else self.prod / name).read_bytes() for name in I.CONFIGS}
        self.original_journal = (self.prod / "operations.jsonl").read_bytes()

    def collect(self):
        return I.collect(self.metadata, self.meta_sha, self.runtime, self.tx, self.revision, io=self.io, verify_source=self.source)

    def plan(self):
        F.store(self.collection, self.collect())
        value = I.plan(self.collection, B.sha(self.collection.read_bytes()), io=self.io, verify_source=self.source, now=1000)
        F.store(self.plan_path, value)
        return value

    def apply(self):
        with mock.patch.object(B.subprocess, "run", side_effect=self.io.lsof):
            return I.apply(self.plan_path, B.sha(self.plan_path.read_bytes()), io=self.io, verify_source=self.source, now=1001)

    def markers(self): return sorted(self.locks.glob("*.pending"))

    def test_true_failed_producer_collect_plan_do_not_mutate_managed_installation(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.plan()
        self.assertEqual({str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        self.assertEqual(self.io.bootstraps, 0)

    def test_actual_native_resume_preserves_configs_and_journal_prefix_then_releases_only_own_fences(self):
        self.plan(); original_pending = (self.prod / "owner-upgrade.pending.json").read_bytes()
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.io.bootstraps, 1)
        self.assertFalse(self.markers())
        self.assertEqual((self.claim / "owner-upgrade.pending.archived.json").read_bytes(), original_pending)
        self.assertEqual((self.prod / "operations.jsonl").read_bytes()[:len(self.original_journal)], self.original_journal)
        self.assertGreater((self.prod / "operations.jsonl").stat().st_size, len(self.original_journal))
        for name, raw in self.original_configs.items():
            self.assertEqual((self.root / name if name == "consumer-admission.json" else self.prod / name).read_bytes(), raw)
        self.assertTrue((self.claim / "release-intent.json").exists())
        self.assertTrue((self.claim / "completed.json").exists())
        self.assertEqual(self.io.owner(None)["pid"], self.authorized["owner_before"]["pid"] + 1)

    def test_normal_terminal_recovery_startup_append_is_allowed(self):
        self.io.append = "recovery"; self.plan()
        self.assertEqual(self.apply()["status"], "completed")

    def test_active_or_unknown_poststartup_journal_is_rejected_without_waiting_it_terminal(self):
        self.io.append = "running"; self.plan()
        with mock.patch.object(I.time, "sleep", side_effect=AssertionError("must not wait active journal")):
            result = self.apply()
        self.assertEqual(result["status"], "outcome-unconfirmed")
        self.assertEqual(result["code"], "owner_operation_or_fence_active")
        self.assertTrue(self.markers())
        self.assertEqual(result["resource_fences_released"], [])

    def test_resume_failure_keeps_resource_barriers_but_never_recreates_archived_startup_marker(self):
        self.plan(); self.io.fault = "resume"
        result = self.apply()
        self.assertEqual(result["status"], "outcome-unconfirmed")
        self.assertEqual(result["resource_fences_released"], [])
        self.assertTrue(self.markers())
        self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())
        self.assertTrue((self.claim / "failure.json").exists())
        again = self.apply()
        self.assertEqual(again["code"], "incident_once_claim_exists_no_retry")
        self.assertEqual(self.io.bootstraps, 1)

    def test_timeout_and_interrupt_after_claim_persist_uncertain_evidence(self):
        self.plan(); self.io.fault = "timeout"
        result = self.apply()
        self.assertEqual(result["code"], "TimeoutExpired")
        self.assertTrue((self.claim / "failure.json").exists())
        self.assertTrue(self.markers())

    def test_keyboard_interrupt_persists_before_propagating(self):
        self.plan(); self.io.fault = "interrupt"
        with self.assertRaises(KeyboardInterrupt): self.apply()
        result = B.document(self.claim / "failure.json")
        self.assertTrue(result["resume_submitted"])
        self.assertEqual(result["code"], "KeyboardInterrupt")
        self.assertTrue(self.markers())

    def test_unknown_or_loaded_owner_is_never_treated_as_unloaded(self):
        for fault, loaded in (("owner-unknown", False), (None, True)):
            self.io.fault, self.io.loaded = fault, loaded
            with self.assertRaises(B.BindingError): self.collect()
        self.assertEqual(self.io.bootstraps, 0)
        self.assertFalse(self.claim.exists())

    def test_replacement_or_unhealthy_old_app_blocks_before_claim(self):
        for fault in ("replacement", "app-unhealthy"):
            self.io.fault = fault
            with self.assertRaises(B.BindingError): self.collect()
        self.assertFalse(self.claim.exists())

    def test_foreign_jobs_pending_and_postdeploy_markers_are_preserved(self):
        paths = [self.locks / ("f" * 64 + ".pending"), self.locks / "consumer-jobs/foreign.json",
                 self.backup / "owner-paused.json", self.backup / "deploy-admitted.json", self.backup / "new-intent-written.json",
                 self.backup / "committed-owner.json.json"]
        for path in paths:
            F.store(path, b"foreign\n")
            with self.assertRaises(B.BindingError): self.collect()
            self.assertEqual(path.read_bytes(), b"foreign\n"); path.unlink()
        self.assertEqual(self.io.bootstraps, 0)

    def test_changed_active_intent_and_configs_cannot_be_adopted(self):
        for path in (self.prod / "owner.json", next(self.locks.glob("*.state.json"))):
            raw = path.read_bytes(); F.store(path, raw + b" ")
            with self.assertRaises(B.BindingError): self.collect()
            F.store(path, raw)
        self.assertFalse(self.claim.exists())

    def test_historical_extra_stopped_or_unknown_state_is_preserved_and_fully_pinned(self):
        key = "docker:fixture-engine:" + "c" * 64
        path = self.locks / (B.sha(key.encode()) + ".state.json")
        F.store(path, {"schema_version":1,"resource_key":key,"desired_state":"unknown"})
        before = path.read_bytes(); self.plan()
        self.assertEqual(self.apply()["status"], "completed")
        self.assertEqual(path.read_bytes(), before)

    def test_new_state_or_same_bytes_replaced_pending_after_plan_blocks(self):
        self.plan()
        marker = self.markers()[0]
        replacement = self.base / "pending-replacement"
        F.store(replacement, marker.read_bytes()); os.replace(replacement, marker)
        result = self.apply()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.io.bootstraps, 0)
        self.assertFalse(self.claim.exists())

    def test_plan_expiry_raw_hash_and_source_drift_never_claim(self):
        self.plan()
        with self.assertRaises(B.BindingError):
            I.apply(self.plan_path, "0" * 64, io=self.io, verify_source=self.source, now=1001)
        with self.assertRaises(B.BindingError):
            I.apply(self.plan_path, B.sha(self.plan_path.read_bytes()), io=self.io, verify_source=self.source, now=1900)
        with self.assertRaises(B.BindingError):
            I.apply(self.plan_path, B.sha(self.plan_path.read_bytes()), io=self.io,
                    verify_source=lambda _: {"revision":"8" * 40}, now=1001)
        self.assertFalse(self.claim.exists())

    def test_release_interruption_records_exact_removed_and_remaining_without_rollback(self):
        self.plan(); markers = [str(p) for p in self.markers()]
        original = Path.unlink
        def fail(path, *args, **kwargs):
            if str(path) == markers[1]: raise OSError("private-fixture-error-must-not-leak")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", fail): result = self.apply()
        self.assertEqual(result["status"], "outcome-unconfirmed")
        self.assertEqual(result["resource_fences_released"], markers[:1])
        self.assertEqual(result["resource_fences_present"], markers[1:])
        self.assertEqual(result["resource_fences_unknown"], [])
        self.assertNotIn("private-fixture-error", json.dumps(result))
        self.assertFalse((self.prod / "owner-upgrade.pending.json").exists())

    def test_receipt_write_failure_does_not_falsely_claim_fences_still_present(self):
        self.plan(); original = B.create
        def fail(path, raw):
            if path.name == "completed.json": raise OSError("fixture receipt failure")
            return original(path, raw)
        with mock.patch.object(B, "create", side_effect=fail): result = self.apply()
        self.assertEqual(result["status"], "outcome-unconfirmed")
        self.assertFalse(result["resource_fences_present"])
        self.assertFalse(self.markers())
        self.assertEqual(self.io.bootstraps, 1)

    def test_claim_binds_lock_inode_and_never_resumes_under_a_replaced_lock(self):
        self.plan(); original = B.create
        def swap(path, raw):
            original(path, raw)
            if path.name == "claim.json":
                replacement = self.base / "replacement-lock"; F.store(replacement, b"")
                os.replace(replacement, self.prod / "owner-upgrade.lock")
        with mock.patch.object(B, "create", side_effect=swap): result = self.apply()
        self.assertEqual(result["code"], "incident_lock_identity_changed")
        self.assertEqual(self.io.bootstraps, 0)
        self.assertTrue(self.markers())

    def test_same_bytes_foreign_marker_replacement_before_release_is_preserved(self):
        self.plan(); original = B.create; marker = self.markers()[0]
        def swap(path, raw):
            original(path, raw)
            if path.name == "release-intent.json":
                replacement = self.base / "replacement-marker"; F.store(replacement, marker.read_bytes())
                os.replace(replacement, marker)
        with mock.patch.object(B, "create", side_effect=swap): result = self.apply()
        self.assertEqual(result["status"], "outcome-unconfirmed")
        self.assertEqual(result["resource_fences_released"], [])
        self.assertIn(str(marker), result["resource_fences_unknown"])
        self.assertTrue(marker.exists())

    def test_new_state_file_after_review_is_not_adopted(self):
        self.plan(); key = "docker:fixture-engine:" + "f" * 64
        F.store(self.locks / (B.sha(key.encode()) + ".state.json"),
                {"schema_version":1,"resource_key":key,"desired_state":"running"})
        self.assertEqual(self.apply()["status"], "blocked")
        self.assertFalse(self.claim.exists())
        self.assertEqual(self.io.bootstraps, 0)

    def test_partial_tail_only_may_be_read_again_without_replacing_checked_prefix(self):
        self.plan(); original = I.pin; partial_reads = []
        def partial(path, mode=0o600):
            raw, identity = original(path, mode)
            if self.io.loaded and Path(path) == self.prod / "operations.jsonl" and not partial_reads:
                partial_reads.append(True); return raw + b'{"type":"nonce"', identity
            return raw, identity
        with mock.patch.object(I, "pin", side_effect=partial), mock.patch.object(I.time, "sleep") as sleep:
            result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sleep.call_count, 1)

    def test_wrong_resumed_binary_identity_never_releases_fences(self):
        self.plan(); self.io.fault = "foreign-hash"
        result = self.apply()
        self.assertEqual(result["code"], "owner_resume_identity_mismatch")
        self.assertEqual(result["resource_fences_released"], [])
        self.assertTrue(self.markers())

    def test_review_expiring_during_preflight_does_not_claim_or_resume(self):
        self.plan(); elapsed = [0.0]; original = I.Incident.open
        def late(incident):
            value = original(incident); elapsed[0] = 900.0; return value
        with mock.patch.object(I.time, "monotonic", side_effect=lambda: elapsed[0]), \
             mock.patch.object(I.Incident, "open", late): result = self.apply()
        self.assertEqual(result["code"], "incident_plan_expired_or_invalid")
        self.assertFalse(self.claim.exists()); self.assertEqual(self.io.bootstraps, 0)

    def test_review_expiring_after_claim_keeps_original_startup_marker(self):
        self.plan(); elapsed = [0.0]; original = B.create
        def late(path, raw):
            original(path, raw)
            if path.name == "claim.json": elapsed[0] = 900.0
        with mock.patch.object(I.time, "monotonic", side_effect=lambda: elapsed[0]), \
             mock.patch.object(B, "create", side_effect=late): result = self.apply()
        self.assertEqual(result["code"], "incident_plan_expired_or_invalid")
        self.assertTrue(self.claim.exists()); self.assertTrue((self.prod / "owner-upgrade.pending.json").exists())
        self.assertEqual(self.io.bootstraps, 0)

    def test_review_deadline_is_checked_at_the_actual_bootstrap_command(self):
        self.plan(); elapsed = [0.0]; original = self.io.owner
        def late(transaction):
            value = original(transaction)
            if self.claim.exists() and not (self.prod / "owner-upgrade.pending.json").exists(): elapsed[0] = 900.0
            return value
        with mock.patch.object(I.time, "monotonic", side_effect=lambda: elapsed[0]), \
             mock.patch.object(self.io, "owner", side_effect=late): result = self.apply()
        self.assertEqual(result["code"], "incident_plan_expired_or_invalid")
        self.assertTrue(result["installation_pending_archived"])
        self.assertFalse(result["resume_submitted"]); self.assertEqual(self.io.bootstraps, 0)
        self.assertTrue(self.markers())

    def test_cli_output_publication_failure_reports_completed_runtime_not_blocked(self):
        self.plan(); output = self.base / "external-result.json"; original_create = B.create; original_apply = I.apply
        def publication(path, raw):
            if path == output: raise OSError("private publication fixture")
            original_create(path, raw)
        def actual_apply(path, digest):
            return original_apply(path, digest, io=self.io, verify_source=self.source, now=1001)
        stream = io.StringIO()
        with mock.patch.object(B, "create", side_effect=publication), mock.patch.object(I, "apply", side_effect=actual_apply), \
             mock.patch.object(B.subprocess, "run", side_effect=self.io.lsof), contextlib.redirect_stdout(stream):
            status = I.main(["apply", "--plan", str(self.plan_path), "--expected-plan-sha256", B.sha(self.plan_path.read_bytes()),
                             "--approved-predeploy-incident-owner-resume-once", "--output", str(output)])
        value = json.loads(stream.getvalue())
        self.assertEqual(status, 1); self.assertEqual(value["status"], "outcome-unconfirmed")
        self.assertEqual(value["code"], "incident_output_publication_failed")
        self.assertTrue(value["owner_verified"]); self.assertTrue(value["resource_fences_released"])
        self.assertTrue((self.claim / "completed.json").exists()); self.assertEqual(self.io.bootstraps, 1)

    def test_cli_output_cannot_write_inside_the_managed_installation(self):
        stream = io.StringIO()
        with mock.patch.object(I, "collect", side_effect=AssertionError("must reject before collection")), contextlib.redirect_stdout(stream):
            status = I.main(["collect", "--metadata", str(self.metadata), "--expected-metadata-sha256", self.meta_sha,
                             "--runtime", str(self.runtime), "--transaction-id", self.tx, "--source-revision", self.revision,
                             "--approved-read-only-collection", "--output", str(self.prod / "new-output.json")])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stream.getvalue())["code"], "incident_output_must_be_outside_managed_roots")
        self.assertFalse((self.prod / "new-output.json").exists())


if __name__ == "__main__": unittest.main()
