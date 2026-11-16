#!/usr/bin/env python3
"""Exercise a committed postdeploy checkpoint using the existing fake release."""

import copy
import fcntl
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = load("postdeploy_state", "release-app-binding-postdeploy-state.py")
F = load("native_binding_fixtures", "release-app-binding.test.py")


class CommittedCheckpointTests(unittest.TestCase):
    def setUp(self):
        fixture = self.fixture = F.BindingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # The original release has eight pre-existing resource keys, including
        # one additional fixed target in the existing notification service.
        target = copy.deepcopy(fixture.config["services"]["notify"]["docker"]["targets"][0])
        target["id"] = "8" * 64
        for config in (fixture.config, fixture.registry):
            config["services"]["notify"]["docker"]["targets"].append(copy.deepcopy(target))
        key = F.B.resource_keys(fixture.config["services"]["notify"])[1]
        F.store(fixture.locks / (F.B.sha(key.encode()) + ".state.json"),
                {"schema_version": 1, "resource_key": key, "desired_state": "running", "updated_at": F.B.stamp()})
        old_model_key = F.B.resource_keys(fixture.config["services"]["omlx"])[0]
        native = {"kind": "gui-app", "uid": os.getuid(), "owner_executable": "/fixture/omlx", "program": "/fixture/python",
                  "program_sha256": "a" * 64, "owner_sha256": "b" * 64}
        for config in (fixture.config, fixture.registry):
            spec = config["services"]["omlx"]
            spec.pop("docker")
            spec.update(platform="mac", mac=copy.deepcopy(native), revision=2)
        new_model_key = F.B.resource_keys(fixture.config["services"]["omlx"])[0]
        (fixture.locks / (F.B.sha(old_model_key.encode()) + ".state.json")).unlink()
        F.store(fixture.locks / (F.B.sha(new_model_key.encode()) + ".state.json"),
                {"schema_version": 1, "resource_key": new_model_key, "desired_state": "running", "updated_at": F.B.stamp()})
        for entry in fixture.consumers["consumers"].values():
            entry["resource_keys"] = [new_model_key if item == old_model_key else item for item in entry["resource_keys"]]
        fixture.registry["registry_revision"] = 14
        fixture.installation["identity"]["consumer_config_digest"] = F.B.sha(F.B.encoded(fixture.consumers))
        F.store(fixture.root / "consumer-admission.json", fixture.consumers)
        fixture.installation["owner_config_sha256"] = F.B.sha(F.B.encoded(fixture.config) + b"\n")
        fixture.installation["identity"]["registry_digest"] = F.B.sha(F.B.encoded(fixture.registry))
        F.store(fixture.prod / "owner.json", fixture.config)
        F.store(fixture.prod / "registry.json", fixture.registry)
        F.store(fixture.prod / "installation.json", fixture.installation)

        self.request = {"schema_version": 2, "kind": "deployment", "product": S.PRODUCT,
                        "commit": S.COMMIT, "version": S.VERSION, "digest": S.DIGEST,
                        "image": f"localhost:9290/{S.PRODUCT}/prod/{S.VERSION}@{S.DIGEST}"}
        fixture.request = self.request
        fixture.observed.update(commit=S.COMMIT, version=S.VERSION, digest=S.DIGEST)
        started = F.B.stamp()
        tx = fixture.begin()
        self.addCleanup(patch.stopall)
        patch.object(S, "TRANSACTION", tx.id).start()

        def resume_failed(_):
            raise F.B.BindingError("owner_resume_unconfirmed")

        fixture.io.resume = resume_failed
        with self.assertRaises(F.B.BindingError) as captured:
            tx.finish(fixture.observed)
        tx.close()
        fcntl.flock(fixture.lifecycle, fcntl.LOCK_UN)
        self.failed = {"schema_version": 1, "kind": "runtime-deployment", "product": S.PRODUCT,
                       "commit": S.COMMIT, "version": S.VERSION, "digest": S.DIGEST,
                       "status": "FAILED", "automatic_rollback": False, "mutation_may_have_started": True,
                       "new_backup_and_migration_files": ["fixture-migration-evidence"],
                       "previous_container_id": tx.old_target["id"], "previous_image_id": tx.old_target["image_id"],
                       "started_at": started, "finished_at": F.B.stamp(), "app_binding": captured.exception.receipt}
        self.state = fixture.base / "deployment-state"
        self.state.mkdir(mode=0o700)
        F.store(self.state / "promotion.lock", b"")
        F.store(self.state / (S.COMMIT + ".json"), {"payload": self.failed, "signature": "c" * 64})

        original_owner = fixture.io.owner
        fixture.io.owner = lambda transaction, **_: original_owner(transaction)
        fixture.io._resume_ready = lambda *args: not fixture.io.paused

        def command(argv):
            self.assertEqual(argv[:2], ["docker", "ps"])
            return (fixture.observed["container_id"] + "\n").encode()

        fixture.io.command = command

    def checkpoint(self):
        return S.Checkpoint(self.fixture.runtime, self.state, self.request, self.failed, self.fixture.io)

    def test_committed_checkpoint_releases_only_its_nine_fences_after_extended_journal(self):
        with self.checkpoint().locked() as checkpoint:
            tx = checkpoint.transaction
            self.assertEqual(checkpoint.observed, self.fixture.observed)
            self.assertEqual(len(list(tx.resources.glob("*.pending"))), 9)
            self.assertTrue(tx.pending.exists())
            with self.assertRaisesRegex(S.B.BindingError, "marker_archive_required"):
                checkpoint.release_journal()
            tx.pending.rename(tx.backup / "test-recovery-marker")
            checkpoint.release_journal()
            self.fixture.io.paused = False
            self.fixture.io.resumes += 1
            with tx.journal.open("ab") as journal:
                journal.write(b'{"type":"nonce"}\n')
            owner = self.fixture.io.owner(tx)
            self.assertEqual(len(checkpoint.planned_fences), 9)
            with self.assertRaisesRegex(S.B.BindingError, "fresh_owner_required"):
                checkpoint.release_fences(dict(owner, pid=owner["pid"] + 1))
            self.assertTrue(tx.job_path.exists())
            self.assertEqual(len(list(tx.resources.glob("*.pending"))), 9)
            self.assertEqual(checkpoint.release_fences(owner), [str(path) for path in checkpoint.planned_fences])
            self.assertFalse(list(tx.resources.glob("*.pending")))
            self.assertFalse(tx.job_path.exists())
            self.assertTrue((tx.backup / "owner-upgrade.pending.committed.json").is_file())
            self.assertTrue((self.state / (S.COMMIT + ".json")).is_file())

    def test_changed_checkpoint_or_markers_fail_before_releasing_anything(self):
        with self.checkpoint().locked() as checkpoint:
            tx = checkpoint.transaction
            marker = next(tx.resources.glob("*.pending"))
            F.store(marker, b"foreign transaction\n")
            with self.assertRaisesRegex(S.B.BindingError, "pinned_file_changed"):
                checkpoint.check_after(checkpoint.configs)
            self.assertTrue(tx.job_path.exists())
            self.assertEqual(len(list(tx.resources.glob("*.pending"))), 9)
            self.assertEqual(checkpoint.released_fences, [])

    def test_checkpoint_holds_existing_private_locks_without_taking_a_second_owner_lifetime_lock(self):
        with self.checkpoint().locked() as checkpoint:
            handles = [os.open(path, os.O_RDWR) for path in (
                self.fixture.runtime / "data/locks/prod.lock",
                self.state / "promotion.lock",
                self.fixture.prod / "owner-upgrade.lock",
                self.fixture.root / "consumer-helper-upgrade.lock",
                checkpoint.transaction.journal,
            )]
            try:
                for descriptor in handles:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                checkpoint.transaction.pending.rename(checkpoint.transaction.backup / "test-recovery-marker")
                checkpoint.release_journal()
                fcntl.flock(handles[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(S.B.BindingError, "fresh_owner_required"):
                    checkpoint.release_fences(None)
                self.assertEqual(len(list(checkpoint.transaction.resources.glob("*.pending"))), 9)
                self.assertTrue(checkpoint.transaction.job_path.exists())
            finally:
                for descriptor in handles:
                    os.close(descriptor)

    def test_only_the_exact_omlx_repin_is_accepted(self):
        with self.checkpoint().locked() as checkpoint:
            owner, registry, _, installation = (checkpoint._config_path(name) for name in S.CONFIGS)
            documents = {path: F.B.document(path) for path in checkpoint.configs}
            for path in (owner, registry):
                omlx = documents[path]["services"]["omlx"]
                omlx["revision"] = 3
                omlx["mac"].update(program_sha256=S.OMLX_PROGRAM, owner_sha256=S.OMLX_OWNER)
            documents[registry]["registry_revision"] = 16
            documents[registry]["evidence"]["omlx"] = {"verified": True, "checked_at": F.B.stamp(), "note": "fixture"}
            documents[installation]["owner_config_sha256"] = F.B.sha(F.B.encoded(documents[owner]) + b"\n")
            documents[installation]["identity"]["registry_digest"] = F.B.sha(F.B.encoded(documents[registry]))
            for path in (owner, registry, installation):
                F.store(path, documents[path])
            checkpoint.check_after({path: path.read_bytes() for path in checkpoint.configs})

            documents[owner]["principals"]["fixture"]["grants"].append("unreviewed")
            documents[installation]["owner_config_sha256"] = F.B.sha(F.B.encoded(documents[owner]) + b"\n")
            F.store(owner, documents[owner])
            F.store(installation, documents[installation])
            with self.assertRaisesRegex(S.B.BindingError, "repin_scope_exceeded"):
                checkpoint.check_after({path: path.read_bytes() for path in checkpoint.configs})
            self.assertEqual(len(list(checkpoint.transaction.resources.glob("*.pending"))), 9)

    def test_modified_state_payload_cannot_enter_recovery(self):
        F.store(self.state / (S.COMMIT + ".json"),
                {"payload": {**self.failed, "status": "SUCCESS"}, "signature": "c" * 64})
        with self.assertRaisesRegex(S.B.BindingError, "failed_state_changed"):
            with self.checkpoint().locked():
                self.fail("changed owner evidence was accepted")


if __name__ == "__main__":
    unittest.main()
