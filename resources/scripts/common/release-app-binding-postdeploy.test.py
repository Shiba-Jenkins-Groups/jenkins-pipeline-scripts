#!/usr/bin/env python3
"""Regression tests for the manual owner lane, without real runtime access."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("postdeploy", Path(__file__).with_name("release-app-binding-postdeploy.py"))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class PostdeployRun(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.backup = self.root / "original"
        self.backup.mkdir(mode=0o700)
        self.owner = self.root / "owner.json"
        self.pending = self.root / "owner-upgrade.pending.json"
        self.old = b'{"executable":"/owner","revision":1}'
        self.new = b'{"executable":"/owner","revision":2}'
        self.key = b"x" * 32
        self.request = dict(commit="a" * 40, version="1.1.25", digest="sha256:" + "b" * 64)
        self.state = self.root / (self.request["commit"] + ".json")
        self.failed = {"status": "FAILED"}
        self.failed_raw = recovery.B.encoded(recovery.D.control.sign(self.failed, self.key))
        recovery.B.create(self.state, self.failed_raw)
        recovery.B.create(self.pending, b"original pending")
        recovery.B.create(self.owner, self.old)
        self.tx = SimpleNamespace(backup=self.backup, config={"executable": "/owner"}, owner_directory=self.root,
                                  pending=self.pending, pending_raw=b"original pending")
        self.io = Mock()
        self.io.engine.return_value = "exact-engine"
        self.io.unload_failed_registration.return_value = True
        self.io.resume.return_value = {"pid": 412}
        self.omlx = {"mode": 0o775}
        self.after_mode = {"mode": 0o755}
        self.checkpoint = Mock(transaction=self.tx, configs={self.owner: self.old}, observed={"version": "1.1.25"},
                               released_fences=[])
        self.checkpoint.release_fences.return_value = ["/owned-fence"]

        @contextmanager
        def locked():
            yield self.checkpoint

        self.checkpoint.locked = locked
        self.checkpoint.release_journal.side_effect = lambda: self.assertFalse(self.pending.exists())
        self.args = SimpleNamespace(action="plan", runtime_root=self.root, state_directory=self.root,
                                    docker_engine_id="exact-engine", library_revision="c" * 40, build_number=1,
                                    plan_sha256="", output=self.root / "plan.json")
        for obj, name, value in (
            (recovery.S, "Checkpoint", Mock(return_value=self.checkpoint)),
            (recovery.O, "plan", Mock(return_value=({self.owner: self.new}, self.omlx))),
            (recovery.O, "apply_mode", Mock(return_value=self.after_mode)),
            (recovery, "check_config", Mock()), (recovery, "readiness", Mock(return_value={"recognition": "ready"})),
        ):
            mocker = patch.object(obj, name, value)
            mocker.start()
            self.addCleanup(mocker.stop)

    def _run(self):
        recovery.execute(self.args, self.request, self.failed, self.failed_raw, self.key, self.io)

    def _apply_reviewed(self):
        self._run()
        plan = self._receipt()
        self.assertEqual(plan["status"], "QUALIFIED")
        self.assertEqual(self.owner.read_bytes(), self.old)
        self.assertTrue(self.pending.exists())
        self.io.resume.assert_not_called()
        self.args.action = "apply"
        self.args.plan_sha256 = plan["plan_sha256"]
        self.args.output = self.root / "receipt.json"

    def _receipt(self):
        return recovery.D.control.verify(json.loads(self.args.output.read_bytes()), self.key)

    def test_exact_once_resume_and_separate_signed_receipt(self):
        self._apply_reviewed()
        self._run()
        self.io.unload_failed_registration.assert_called_once()
        self.io.resume.assert_called_once()
        self.checkpoint.release_fences.assert_called_once()
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.state.read_bytes(), self.failed_raw)
        self.assertEqual(self._receipt()["status"], "SUCCESS")
        self.assertEqual(self._receipt()["services"], {"recognition": "ready"})
        self.args.output = self.root / "replay.json"
        with self.assertRaises(FileExistsError):
            self._run()
        self.io.resume.assert_called_once()

    def test_plan_drift_cannot_mutate(self):
        self._apply_reviewed()
        self.args.plan_sha256 = "1" * 64
        with self.assertRaisesRegex(recovery.B.BindingError, "reviewed_recovery_plan_changed"):
            self._run()
        self.io.unload_failed_registration.assert_not_called()
        self.assertTrue(self.pending.is_file())

    def test_unconfirmed_owner_retains_admission_and_restores_startup_fence(self):
        self._apply_reviewed()
        self.io.resume.side_effect = recovery.B.BindingError("owner_resume_unconfirmed")
        with self.assertRaisesRegex(recovery.B.BindingError, "owner_resume_unconfirmed"):
            self._run()
        self.assertEqual(self.pending.read_bytes(), b"original pending")
        self.checkpoint.release_fences.assert_not_called()
        self.assertEqual(self._receipt()["phase"], "managed-owner-resume")
        self.assertEqual(self._receipt()["status"], "FAILED")

    def _precommit_attempt(self):
        previous = self.backup / recovery.CLAIM
        previous.mkdir(mode=0o700)
        original_plan = {"plan_sha256": "b" * 64, "before": recovery.pins({self.owner: self.old}),
                         "after": recovery.pins({self.owner: self.new}),
                         "omlx": {"program": {"mode": 0o775, "inode": 42, "sha256": "c" * 64}}}
        failure_raw = b"original signed failure bytes"
        claim = {"status": "CLAIMED", "build_number": 2, "transaction_id": recovery.TX,
                 "library_revision": recovery.PRIOR_LIBRARY, "plan_sha256": original_plan["plan_sha256"]}
        recovery.B.create(previous / "claim.json", recovery.B.encoded(recovery.D.control.sign(claim, self.key)))
        recovery.B.create(previous / "before-owner.json", self.old)
        recovery.B.create(previous / "failure.json", failure_raw)
        self.io.owner.return_value = None
        self.prehardened = {"program": {"mode": 0o755, "inode": 42, "sha256": "c" * 64}}
        recovery.O.plan.return_value = {self.owner: self.new}, self.prehardened
        return previous, (original_plan, failure_raw)

    def test_signed_precommit_continuation_uses_only_already_hardened_inode(self):
        previous, prior = self._precommit_attempt()
        recovery.execute(self.args, self.request, self.failed, self.failed_raw, self.key, self.io, prior)
        plan = self._receipt()
        self.assertEqual(plan["continued_from_build"], 2)
        self.assertEqual(plan["omlx"]["program"]["mode"], 0o755)
        self.args.action = "apply"
        self.args.plan_sha256 = plan["plan_sha256"]
        self.args.output = self.root / "continued-receipt.json"
        recovery.execute(self.args, self.request, self.failed, self.failed_raw, self.key, self.io, prior)
        self.io.unload_failed_registration.assert_not_called()
        recovery.O.apply_mode.assert_not_called()
        self.io.resume.assert_called_once()
        self.assertEqual((previous / "failure.json").read_bytes(), prior[1])
        self.assertTrue((previous / recovery.CONTINUATION / "completed.json").is_file())
        self.assertEqual(self._receipt()["status"], "SUCCESS")

    def test_continuation_refuses_an_unreviewed_inode_or_physical_progress(self):
        previous, prior = self._precommit_attempt()
        self.prehardened["program"]["inode"] = 72
        with self.assertRaisesRegex(recovery.B.BindingError, "post_app_management_identity_or_config_changed"):
            recovery.execute(self.args, self.request, self.failed, self.failed_raw, self.key, self.io, prior)
        recovery.B.create(previous / "commit-intent.json", b"unexpected progress")
        with self.assertRaisesRegex(recovery.B.BindingError, "previous_recovery_has_additional_progress"):
            recovery.execute(self.args, self.request, self.failed, self.failed_raw, self.key, self.io, prior)
        self.io.resume.assert_not_called()
        self.assertTrue(self.pending.exists())

    def test_readiness_failure_does_not_become_a_successful_recovery(self):
        self._apply_reviewed()
        recovery.readiness.side_effect = recovery.B.BindingError("recognition_readiness_deadline_exceeded")
        with self.assertRaisesRegex(recovery.B.BindingError, "recognition_readiness_deadline_exceeded"):
            self._run()
        self.assertTrue(self._receipt()["admission_released"])
        self.assertEqual(self._receipt()["phase"], "recognition-readiness")
        self.assertEqual(self._receipt()["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
