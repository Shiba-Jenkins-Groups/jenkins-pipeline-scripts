#!/usr/bin/env python3
"""Fixture tests without Docker, live SQLite, or owner state."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "owner115_runtime", Path(__file__).with_name("release-app-binding-115-runtime.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


class RecoveryRuntime(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=".owner115-runtime-test-", dir=Path.home())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "data/backups/prod-predeploy"
        self.directory.mkdir(parents=True)
        self.journal = self.root / "data/logs/prod-deploy-journal.log"
        self.journal.parent.mkdir()
        self.backup = self.directory / (R.MIGRATION + ".db")
        self.verify = self.directory / (R.MIGRATION + "-verify.json")
        self.apply = self.directory / (R.MIGRATION + "-apply.json")
        self.env = self.root / ".env.prod"
        self.old_assignment = f"APP_IMAGE={R.PREVIOUS_IMAGE}\n".encode()
        self.new_assignment = f"APP_IMAGE={R.IMAGE}\n".encode()
        self.old_env = b"REFERENCE=" + self.old_assignment + self.old_assignment + b"OTHER=opaque\n"
        self.write(self.env, self.old_env)
        self.journal_original = (
            f"2026-11-25T11:00:11+0800\tDEPLOY-BEGIN prev={R.PREVIOUS_IMAGE} new={R.IMAGE}\n"
            f"2026-11-25T11:00:24+0800\tMIGRATION-OK commit={R.COMMIT} backup={self.backup} evidence={self.verify}\n"
        ).encode()
        self.write(self.journal, self.journal_original, mode=0o644)
        self.request = {"commit": R.COMMIT, "version": R.VERSION, "digest": R.DIGEST, "image": R.IMAGE}

    @staticmethod
    def write(path, raw, mode=0o600):
        path.write_bytes(raw)
        path.chmod(mode)

    def test_existing_migration_evidence_and_inactive_backup_are_pinned(self):
        backup = b"opaque closed backup"
        apply = json.dumps({**R.MIGRATION_FIELDS, "applied": True, "backup_path": "/backup/" + self.backup.name,
                            "backup_sha256": R.B.sha(backup)}).encode()
        verify = json.dumps({**R.MIGRATION_FIELDS, "applied": False}).encode()
        for path, raw in ((self.backup, backup), (self.apply, apply), (self.verify, verify)):
            self.write(path, raw)
        failure = {**self.request, "kind": "runtime-deployment", "status": "FAILED",
                   "previous_container_id": R.PREVIOUS_CONTAINER_ID, "previous_image_id": R.PREVIOUS_IMAGE_ID,
                   "mutation_may_have_started": True, "backup_directory": str(self.directory),
                   "deploy_journal": str(self.journal),
                   "new_backup_and_migration_files": list(map(str, (self.backup, self.apply, self.verify)))}
        with patch.multiple(R, MIGRATION_BACKUP_SIZE=len(backup), MIGRATION_BACKUP_SHA=R.B.sha(backup),
                            MIGRATION_APPLY_SHA=R.B.sha(apply), MIGRATION_VERIFY_SHA=R.B.sha(verify)):
            receipt = R.verify_migration_evidence(self.root, failure)
            self.assertFalse(receipt["migration_rerun"])
            self.assertEqual(receipt["backup_sha256"], R.B.sha(backup))
            self.write(self.backup, b"-" * len(backup))
            with self.assertRaisesRegex(R.B.BindingError, "backup_changed"):
                R.verify_migration_evidence(self.root, failure)
            self.write(self.backup, backup)
            self.write(self.verify, verify + b"\n")
            with self.assertRaisesRegex(R.B.BindingError, "receipt_bytes_changed"):
                R.verify_migration_evidence(self.root, failure)

    def test_final_identity_must_be_the_existing_exact_container(self):
        observed = {**self.request, "container_id": R.CONTAINER_ID, "image_id": R.IMAGE_ID,
                    "heartbeat": 200, "compose_project": R.PROJECT}
        deploy = SimpleNamespace(run=lambda cmd: R.ENGINE_ID, no_host_writer=lambda root: None,
                                 image_identity=lambda request: R.IMAGE_ID,
                                 final_identity=lambda request, image, runtime: observed)
        self.assertIs(R.observe_app(deploy, self.request, self.root), observed)
        observed["container_id"] = R.PREVIOUS_CONTAINER_ID
        with self.assertRaisesRegex(R.B.BindingError, "running_app_identity_changed"):
            R.observe_app(deploy, self.request, self.root)

    def test_persistence_replaces_only_app_image_and_records_deploy_completion(self):
        R.verify_activation_inputs(self.root, self.request, R.PREVIOUS_IMAGE)
        result = R.persist_completed_activation(self.root, self.request, R.PREVIOUS_IMAGE, R.TRANSACTION_ID)
        self.assertEqual(self.env.read_bytes(), b"REFERENCE=" + self.old_assignment
                         + self.new_assignment + b"OTHER=opaque\n")
        journal = self.journal.read_bytes()
        self.assertTrue(journal.startswith(self.journal_original))
        self.assertIn(f"DEPLOY-RECOVERED owner_build=115 transaction={R.TRANSACTION_ID}".encode(), journal)
        self.assertTrue(journal.endswith(f"DEPLOY-OK   {R.IMAGE}\n".encode()))
        self.assertTrue(result["app_deploy_journal_completed"])
        self.assertEqual(result["deploy_journal_sha256"], R.B.sha(journal))

    def test_image_persistence_failure_leaves_original_deploy_journal(self):
        with patch.object(R.B, "replace", side_effect=OSError("fixture write failed")):
            with self.assertRaisesRegex(OSError, "fixture write failed"):
                R.persist_completed_activation(self.root, self.request, R.PREVIOUS_IMAGE, R.TRANSACTION_ID)
        self.assertEqual(self.env.read_bytes(), self.old_env)
        self.assertEqual(self.journal.read_bytes(), self.journal_original)

    def test_duplicate_assignment_or_changed_journal_stop_preflight(self):
        self.write(self.env, self.old_env + b"export APP_IMAGE =other\n")
        with self.assertRaisesRegex(R.B.BindingError, "persisted_image_predecessor_changed"):
            R.verify_activation_inputs(self.root, self.request, R.PREVIOUS_IMAGE)
        self.write(self.env, self.old_env)
        self.write(self.journal, self.journal_original + b"changed\n", mode=0o644)
        with self.assertRaisesRegex(R.B.BindingError, "deploy_journal_tail_changed"):
            R.verify_activation_inputs(self.root, self.request, R.PREVIOUS_IMAGE)


if __name__ == "__main__":
    unittest.main()
