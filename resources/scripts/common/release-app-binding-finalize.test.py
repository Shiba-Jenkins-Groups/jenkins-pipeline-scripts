#!/usr/bin/env python3
"""Self-contained two-generation producer fixtures; never use host runtime/Git."""
import contextlib
import copy
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value

P = load("running_fixture_producer", HERE / "release-app-binding-incident.test.py")
F = P.F


class FinalizeTests(unittest.TestCase):
    def git(self, *args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", "-c", "user.name=Offline Fixture",
            "-c", "user.email=fixture@example.invalid", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def setUp(self):
        F.BindingTests.setUp(self)
        # Eight protected resources, using the real AppBinding failed producer.
        target = copy.deepcopy(self.config["services"]["ollama"]["docker"]["targets"][0])
        target["id"] = "8" * 64
        for mapping in (self.config, self.registry): mapping["services"]["ollama"]["docker"]["targets"].append(copy.deepcopy(target))
        key = F.B.resource_keys(self.config["services"]["ollama"])[1]
        F.store(self.locks / (F.B.sha(key.encode()) + ".state.json"), {"schema_version": 1, "resource_key": key, "desired_state": "running"})
        self.installation["owner_config_sha256"] = F.B.sha(F.B.encoded(self.config) + b"\n")
        self.installation["identity"]["registry_digest"] = F.B.sha(F.B.encoded(self.registry))
        F.store(self.prod / "owner.json", self.config); F.store(self.prod / "registry.json", self.registry)
        F.store(self.prod / "installation.json", self.installation)
        producer = P.FailedPause(self.root)
        with self.assertRaises(F.B.BindingError) as failure:
            F.B.AppBinding.begin(self.request, self.runtime, self.lifecycle, io=producer)
        self.failure = failure.exception.receipt
        self.tx, self.backup = self.failure["transaction_id"], Path(self.failure["backup"])
        self.authorized = F.B.document(self.backup / "authorized.json")
        fcntl.flock(self.lifecycle, fcntl.LOCK_UN)
        # Git generations are created here, not borrowed from the parent checkout.
        self.repo = self.base / "git-producer"; self.repo.mkdir(mode=0o700)
        for name in ("release-app-binding.py", "release-app-binding-incident.py"):
            (self.repo / name).write_bytes((HERE / name).read_bytes())
        self.git("init", "-q"); self.git("add", "."); self.git("commit", "-qm", "original producer")
        self.old_revision = self.git("rev-parse", "HEAD")
        self.O = load("running_original_producer_" + self.tx, self.repo / "release-app-binding-incident.py")
        self.B = self.O.B
        self.metadata = self.base / "metadata.json"
        F.store(self.metadata, {"transaction_id": self.tx, "backup": str(self.backup), "authorized": self.authorized,
            "failure": self.failure, "live_probe_performed": False, "credentials_exported": False, "journal_records_exported": False,
            "backup_files": {name: {"sha256": self.B.sha((self.backup / name).read_bytes()),
                "bytes": (self.backup / name).stat().st_size} for name in self.O.ORIGINAL_FILES}})
        fixture, original = self, self.O
        class RunningReplay(P.NativeReplay):
            def owner(self, tx, **kwargs):
                if not self.loaded: return None
                return dict(self.before, pid=21988, uid_start=str(os.getuid()) + " fixture-running-start")
            def resume(self, tx, *, before_bootstrap):
                return original.IncidentIO.resume(self, tx, before_bootstrap=before_bootstrap)
        self.transport = RunningReplay(self); self.addCleanup(self.transport.close)
        self.old_collection, self.old_plan = self.base / "old-collection.json", self.base / "old-plan.json"
        F.store(self.old_collection, self.O.collect(self.metadata, self.B.sha(self.metadata.read_bytes()), self.runtime,
            self.tx, self.old_revision, io=self.transport))
        F.store(self.old_plan, self.O.plan(self.old_collection, self.B.sha(self.old_collection.read_bytes()), io=self.transport, now=1000))
        def failed_resume(guard, transaction):
            self.assertIsNone(guard.owner(transaction))
            guard.command(["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()), str(transaction.plist)])
            raise self.B.BindingError("foreign_owner_process")
        with mock.patch.object(self.B.NativeIO, "resume", failed_resume):
            result = self.O.apply(self.old_plan, self.B.sha(self.old_plan.read_bytes()), io=self.transport, now=1001)
        self.assertEqual(result["status"], "outcome-unconfirmed")
        self.assertEqual(result["phase"], "resume-submitted")
        self.assertEqual(self.transport.bootstraps, 1)
        self.old_receipt = self.base / "old-receipt.json"; F.store(self.old_receipt, result)
        self.original_claim = self.backup / self.O.CLAIM
        self.old_immutable = {p: p.read_bytes() for p in self.original_claim.iterdir()}
        # A second genuine Git commit owns only the new finalizer generation.
        (self.repo / "release-app-binding-finalize.py").write_bytes((HERE / "release-app-binding-finalize.py").read_bytes())
        self.git("add", "release-app-binding-finalize.py"); self.git("commit", "-qm", "running finalizer")
        self.revision = self.git("rev-parse", "HEAD")
        self.M = load("running_finalize_" + self.tx, self.repo / "release-app-binding-finalize.py")
        self.B = self.M.B
        self.observation = self.base / "observation.json"
        owner = self.transport.owner(None)
        F.store(self.observation, {"launchctl_exit": 0, "ps_exit": 0, "listener_exit": 0, "pid": 21988,
            "proc_pidpath_return": 100, "proc_pidpath_errno": 0, "kernel_path": owner["kernel_path"],
            "path_matches_original": True, "binary_matches_original": True, "listener_pids": ["21988"],
            "uid_start_comm": owner["uid_start"] + " " + owner["kernel_path"], "binary_sha256": owner["binary_sha256"],
            "expected_binary_sha256": owner["binary_sha256"], "expected_original_binary": owner["kernel_path"]})
        self.barrier = self.base / "barrier.json"
        F.store(self.barrier, {"transaction_id": self.tx, "original_startup_pending_archived": True,
            "claim_present": True, "completed_receipt_exists": False, "resource_fences_released": [],
            "no_new_bootstrap": True, "no_runtime_mutation": True,
            "resource_fences_still_present_and_unchanged": [{"path": str(p), "sha256": self.B.sha(p.read_bytes()),
                "inode": p.stat().st_ino} for p in sorted(self.locks.glob("*.pending"))]})
        self.export = self.base / "export"; self.export.mkdir(mode=0o700)
        files = {"original-inactive-metadata.json": self.metadata, "original-collection.json": self.old_collection,
            "original-plan.json": self.old_plan, "original-recovery-receipt.json": self.old_receipt,
            "reviewed-running-owner.json": self.observation, "postfailure-barriers.json": self.barrier,
            "original-claim.json": self.original_claim / "claim.json", "original-claim-failure.json": self.original_claim / "failure.json"}
        self.bundle = self.export / "bundle.json"
        for name, path in files.items(): F.store(self.export / name, path.read_bytes())
        F.store(self.bundle, {"base": self.old_revision, "live_probes_this_export": False, "files": {
            name: {"source": str(path), "sha256": self.B.sha(path.read_bytes()), "bytes": path.stat().st_size}
            for name, path in files.items()}})
        module = self.M
        class FinalReplay(module.FinalizeIO):
            root = fixture.root
            def __init__(self): self.fault = None; self.observations = 0; self.unlink_calls = 0
            def owner(self, tx, **kwargs):
                value = fixture.transport.owner(tx)
                if self.fault == "pid": value["pid"] += 1
                if self.fault == "start": value["uid_start"] += " changed"
                if self.fault == "hash": value["binary_sha256"] = "0" * 64
                return value
            def engine(self, tx): return fixture.transport.engine(tx)
            def app_inventory(self, tx):
                values = fixture.transport.app_inventory(tx)
                return values | {"b" * 64} if self.fault == "replacement" else values
            def app(self, tx, cid): return fixture.transport.app(tx, cid)
            def observe(self, args):
                self.observations += 1
                if "-iTCP:8120" in args: return b"21988\n21989\n" if self.fault == "listener" else b"21988\n"
                inode = (fixture.prod / "operations.jsonl").stat()
                return ("p21988\nf%s\ntREG\nD0x%x\ni%d\nn%s\n" % ("mem" if self.fault == "mapping" else "5u", inode.st_dev,
                    inode.st_ino + (1 if self.fault == "handle" else 0), fixture.prod / "operations.jsonl")).encode()
            def command(self, args): raise AssertionError("finalizer must never bootstrap/control a service")
        self.io = FinalReplay()
        self.collection = self.base / "final-collection.json"; self.plan_path = self.base / "final-plan.json"
        self.claim = self.original_claim / self.M.CLAIM

    def collect(self): return self.M.collect(self.bundle, self.B.sha(self.bundle.read_bytes()), self.tx, self.revision, io=self.io)
    def make_plan(self):
        F.store(self.collection, self.collect())
        F.store(self.plan_path, self.M.plan(self.collection, self.B.sha(self.collection.read_bytes()), io=self.io, now=2000))
    def apply(self): return self.M.apply(self.plan_path, self.B.sha(self.plan_path.read_bytes()), io=self.io, now=2001)
    def assert_original(self):
        for path, raw in self.old_immutable.items(): self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.transport.bootstraps, 1)

    def test_two_real_git_generations_and_true_unconfirmed_producer_finalize_without_bootstrap(self):
        self.assertNotEqual(self.old_revision, self.revision)
        self.make_plan(); result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["bootstrap_performed"])
        self.assertTrue(result["original_autorecovery_permitted_after_release"])
        self.assertEqual(len(result["resource_fences_released"]), 8)
        self.assertEqual(result["resource_fences_present"], [])
        self.assertFalse(list(self.locks.glob("*.pending")))
        self.assert_original()

    def test_collect_plan_are_readonly_and_keep_all_eight_fences(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.make_plan()
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        self.assertFalse(self.claim.exists()); self.assert_original()

    def test_fixed_claim_cannot_be_retried_with_a_new_plan(self):
        self.make_plan(); self.assertEqual(self.apply()["status"], "completed")
        again = self.apply()
        self.assertEqual(again["code"], "finalize_once_claim_exists_no_retry")
        self.assert_original()

    def test_wrong_pid_start_hash_listener_or_journal_handle_rejects(self):
        for fault in ("pid", "start", "hash", "listener", "handle", "mapping", "replacement"):
            self.io.fault = fault
            with self.subTest(fault=fault), self.assertRaises(self.B.BindingError): self.collect()
        self.assertFalse(self.claim.exists()); self.assert_original()

    def test_journal_ex_busy_without_same_pid_handle_is_insufficient(self):
        self.io.fault = "handle"
        with self.assertRaisesRegex(self.B.BindingError, "same_pid_journal_handle"): self.collect()
        self.assert_original()

    def test_partial_lsof_stderr_inventory_rejects_before_claim_or_unlink(self):
        self.make_plan(); actual_run = self.M.subprocess.run
        def observed_run(args, **kwargs):
            if args[0] == "/usr/sbin/lsof":
                return subprocess.CompletedProcess(args, 0, stdout=b"21988\n", stderr=b"private-warning-not-complete")
            return actual_run(args, **kwargs)
        with mock.patch.object(self.io, "observe", side_effect=lambda argv: self.M.FinalizeIO.observe(self.io, argv)), mock.patch.object(self.M.subprocess, "run", side_effect=observed_run):
            result = self.apply()
        self.assertEqual(result["code"], "finalize_positive_observation_required")
        self.assertEqual(result["resource_fences_released"], [])
        self.assertEqual(len(result["resource_fences_present"]), 8)
        self.assertFalse(self.claim.exists()); self.assertNotIn("private-warning", json.dumps(result)); self.assert_original()

    def test_journal_open_handle_without_lifetime_lock_is_insufficient(self):
        self.transport.close()
        with self.assertRaisesRegex(self.B.BindingError, "lifetime_journal_lock"): self.collect()
        self.assert_original()

    def test_journal_active_or_unknown_complete_record_is_never_waited_terminal(self):
        path = self.prod / "operations.jsonl"; baseline = path.read_bytes()
        for record in ({"type":"operation","operation":{"id":"f"*32,"state":"running"}}, {"type":"unexpected"}):
            F.store(path, baseline + self.B.encoded(record) + b"\n")
            with mock.patch.object(self.M.time, "sleep", side_effect=AssertionError("no semantic retry")):
                with self.assertRaises((self.B.BindingError, self.M.Old.B.BindingError)): self.collect()
        self.assert_original()

    def test_first_journal_read_partial_tail_is_completed_with_bounded_retry(self):
        path = self.prod / "operations.jsonl"; actual_pin = self.M.pin; injected = []
        def racing_pin(value, mode=0o600):
            if Path(value) == path and not injected:
                injected.append(True)
                with path.open("ab") as output: output.write(b'{"type":"nonce","nonce":"racing"')
            return actual_pin(value, mode)
        def complete_line(delay):
            self.assertEqual(delay, 0.02)
            with path.open("ab") as output: output.write(b'}\n')
        with mock.patch.object(self.M, "pin", side_effect=racing_pin), mock.patch.object(self.M.time, "sleep", side_effect=complete_line) as sleep:
            self.collect()
        self.assertEqual(sleep.call_count, 1); self.assertFalse(self.claim.exists()); self.assert_original()

    def test_first_journal_read_file_changed_retries_without_semantic_fallback(self):
        path = self.prod / "operations.jsonl"; actual_pin = self.M.pin; injected = []
        def racing_pin(value, mode=0o600):
            if Path(value) == path and not injected:
                injected.append(True); raise self.B.BindingError("file_changed")
            return actual_pin(value, mode)
        with mock.patch.object(self.M, "pin", side_effect=racing_pin), mock.patch.object(self.M.time, "sleep") as sleep:
            self.collect()
        self.assertEqual(sleep.call_count, 1); self.assertFalse(self.claim.exists()); self.assert_original()

    def test_reviewed_journal_prefix_allows_nonce_and_terminal_append(self):
        self.make_plan()
        with (self.prod / "operations.jsonl").open("ab") as out:
            out.write(self.B.encoded({"type":"nonce","nonce":"later"}) + b"\n")
        self.assertEqual(self.apply()["status"], "completed"); self.assert_original()

    def test_journal_same_bytes_new_inode_and_prefix_change_reject(self):
        self.make_plan(); path = self.prod / "operations.jsonl"
        replacement = self.base / "replacement-journal"; F.store(replacement, path.read_bytes()); os.replace(replacement, path)
        self.assertEqual(self.apply()["status"], "blocked")
        self.assertFalse(self.claim.exists()); self.assert_original()

    def test_marker_same_bytes_new_inode_rejects_without_claim(self):
        self.make_plan(); path = sorted(self.locks.glob("*.pending"))[0]
        replacement = self.base / "replacement-marker"; F.store(replacement, path.read_bytes()); os.replace(replacement, path)
        self.assertEqual(self.apply()["status"], "blocked")
        self.assertFalse(self.claim.exists()); self.assert_original()

    def test_changed_original_claim_or_current_producer_rejects(self):
        self.make_plan(); path = self.repo / "release-app-binding-incident.py"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(self.B.BindingError, "current_producer_changed"): self.apply()
        self.assertFalse(self.claim.exists()); self.assert_original()

    def test_foreign_pending_jobs_or_progress_rejects_without_removal(self):
        for path in (self.locks / ("f" * 64 + ".pending"), self.locks / "consumer-jobs/foreign.json", self.backup / "deploy-admitted.json"):
            F.store(path, b"foreign\n")
            with self.subTest(path=path.name), self.assertRaises(self.B.BindingError): self.collect()
            self.assertEqual(path.read_bytes(), b"foreign\n"); path.unlink()
        self.assert_original()

    def test_expiry_before_claim_leaves_all_barriers(self):
        self.make_plan()
        with mock.patch.object(self.M.time, "monotonic", side_effect=[0, 0, 1000]): result = self.apply()
        self.assertEqual(result["code"], "finalize_plan_expired_or_invalid")
        self.assertFalse(self.claim.exists()); self.assertEqual(len(result["resource_fences_present"]), 8)

    def test_expiry_after_claim_before_first_unlink_keeps_fixed_claim(self):
        self.make_plan()
        with mock.patch.object(self.M.time, "monotonic", side_effect=[0, 0, 0, 1000]): result = self.apply()
        self.assertEqual(result["code"], "finalize_plan_expired_or_invalid")
        self.assertTrue(self.claim.exists()); self.assertEqual(result["resource_fences_released"], [])
        self.assertEqual(len(result["resource_fences_present"]), 8); self.assert_original()

    def test_partial_release_is_recorded_and_never_recreates_fences(self):
        self.make_plan(); unlink = Path.unlink
        def fail_second(path, *args, **kwargs):
            if path.suffix == ".pending":
                self.io.unlink_calls += 1
                if self.io.unlink_calls == 2: raise OSError("private-error-must-not-log")
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", fail_second): result = self.apply()
        self.assertEqual(result["status"], "outcome-unconfirmed")
        self.assertEqual((len(result["resource_fences_released"]), len(result["resource_fences_present"])), (1,7))
        self.assertEqual(result["resource_fences_unknown"], []); self.assertNotIn("private-error", json.dumps(result))
        self.assertIsNone(result["checked_journal_prefix_preserved"])
        self.assertFalse(result["original_autorecovery_permitted_after_release"])
        self.assertTrue((self.claim / "release-intent.json").exists()); self.assert_original()

    def test_recreated_fence_after_final_recheck_never_publishes_completed(self):
        self.make_plan(); actual_outcome = self.M.RunningFinalization.outcome; recreated = []
        def uncertain(current, status, *args, **kwargs):
            if status == "completed" and not recreated:
                path = Path(current.released[0]); F.store(path, (self.tx + "\n").encode()); recreated.append(path)
            return actual_outcome(current, status, *args, **kwargs)
        with mock.patch.object(self.M.RunningFinalization, "outcome", uncertain): result = self.apply()
        self.assertEqual(result["status"], "outcome-unconfirmed")
        self.assertEqual(result["code"], "finalize_release_outcome_unconfirmed")
        self.assertEqual(len(result["resource_fences_released"]), 7)
        self.assertEqual(result["resource_fences_unknown"], [str(recreated[0])])
        self.assertTrue(recreated[0].exists()); self.assertFalse((self.claim / "completed.json").exists())
        self.assertIsNone(result["checked_journal_prefix_preserved"])
        self.assertFalse(result["original_autorecovery_permitted_after_release"]); self.assert_original()

    def test_changed_own_release_intent_blocks_before_first_unlink(self):
        self.make_plan(); actual_recheck = self.M.RunningFinalization.recheck; changed = []
        def tampered(current):
            record = current.claim / "release-intent.json"
            if record.exists() and not changed:
                changed.append(True); F.store(record, record.read_bytes() + b" ")
            return actual_recheck(current)
        with mock.patch.object(self.M.RunningFinalization, "recheck", tampered): result = self.apply()
        self.assertEqual(result["code"], "finalize_once_record_identity_changed")
        self.assertEqual(result["resource_fences_released"], [])
        self.assertEqual(len(result["resource_fences_present"]), 8); self.assert_original()

    def test_interrupt_persists_failure_and_never_restarts_owner(self):
        self.make_plan(); unlink = Path.unlink
        def interrupt(path, *args, **kwargs):
            if path.suffix == ".pending": raise KeyboardInterrupt()
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", interrupt), self.assertRaises(KeyboardInterrupt): self.apply()
        result = self.B.document(self.claim / "failure.json")
        self.assertEqual(result["code"], "KeyboardInterrupt"); self.assertEqual(result["resource_fences_released"], [])
        self.assert_original()

    def test_cli_publication_failure_preserves_known_completed_outcome(self):
        self.make_plan(); output = self.base / "result.json"; create = self.B.create
        def fail_output(path, data):
            if path == output: raise OSError("fixture output failure")
            return create(path, data)
        result = io.StringIO(); real_apply = self.M.apply
        with mock.patch.object(self.M, "apply", side_effect=lambda *a, **k: real_apply(self.plan_path, self.B.sha(self.plan_path.read_bytes()), io=self.io, now=2001)), mock.patch.object(self.B, "create", fail_output), contextlib.redirect_stdout(result):
            code = self.M.main(["apply", "--plan", str(self.plan_path), "--expected-plan-sha256", self.B.sha(self.plan_path.read_bytes()),
                "--approved-running-owner-fence-finalization-once", "--output", str(output)])
        value = json.loads(result.getvalue())
        self.assertEqual((code, value["status"], value["operation_result_status"]), (1, "outcome-unconfirmed", "completed"))
        self.assertEqual(len(value["resource_fences_released"]), 8); self.assertFalse(value["bootstrap_performed"])
        self.assert_original()


if __name__ == "__main__": unittest.main()
