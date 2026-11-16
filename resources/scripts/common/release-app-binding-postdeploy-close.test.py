#!/usr/bin/env python3
"""Exercise additive closure with active unrelated native admission."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = load("postdeploy_close", "release-app-binding-postdeploy-close.py")
T = load("postdeploy_state_test", "release-app-binding-postdeploy-state.test.py")
S, F = T.S, T.F


class VerifyClosedTransactionTests(unittest.TestCase):
    def setUp(self):
        state = self.state = T.CommittedCheckpointTests()
        state.setUp()
        self.addCleanup(state.doCleanups)
        fixture = self.fixture = state.fixture
        with state.checkpoint().locked() as checkpoint:
            tx = checkpoint.transaction
            self.addCleanup(patch.stopall)
            patch.object(C.S, "TRANSACTION", tx.id).start()
            self.parent = tx.backup / C.CLAIM
            self.attempt = self.parent / C.CONTINUATION
            self.parent.mkdir(mode=0o700)
            self.attempt.mkdir(mode=0o700)
            owner, registry, _, installation = [checkpoint._config_path(name) for name in S.CONFIGS]
            documents = {path: F.B.document(path) for path in checkpoint.configs}
            self.before = dict(checkpoint.configs)
            for path in (owner, registry):
                omlx = documents[path]["services"]["omlx"]
                omlx["revision"] = 3
                omlx["mac"].update(program_sha256=S.OMLX_PROGRAM, owner_sha256=S.OMLX_OWNER)
            documents[registry]["registry_revision"] = 16
            documents[registry]["evidence"]["omlx"] = {"verified": True, "checked_at": F.B.stamp(), "note": "fixture"}
            documents[installation]["owner_config_sha256"] = F.B.sha(F.B.encoded(documents[owner]) + b"\n")
            documents[installation]["identity"]["registry_digest"] = F.B.sha(F.B.encoded(documents[registry]))
            for path, raw in self.before.items():
                for folder in (self.parent, self.attempt):
                    F.store(folder / ("before-" + path.name), raw)
            for path in (owner, registry, installation):
                F.store(path, documents[path])
            self.after = {path: path.read_bytes() for path in checkpoint.configs}
            F.store(self.attempt / "candidate-owner.json", self.after[owner])
            checkpoint.check_after(self.after)
            tx.pending.rename(self.attempt / "startup-pending.archived.json")
            checkpoint.release_journal()
            fixture.io.paused = False
            fixture.io.resumes += 1
            expected = fixture.io.owner(tx)
            released = checkpoint.release_fences(expected)
        self.plan = {"transaction_id": tx.id, "app": checkpoint.observed,
                     "before": C._config_hashes(self.before), "after": C._config_hashes(self.after)}
        self.applied = {"transaction_id": tx.id, "owner": expected, "app": checkpoint.observed,
                        "released_fences": released, "commit": S.COMMIT, "version": S.VERSION, "digest": S.DIGEST}
        self.generation = expected
        self.fixture.io.prove_running = lambda _, expect: C.need(self.generation == expect, "fixture_generation_changed")
        # A real owner may be in the middle of writing an active recovery
        # operation after #101's own fences have been removed.
        with tx.journal.open("ab") as journal:
            journal.write(b'{"type":"operation","operation":{"id":"aa","state":"running"}}\n')
        self.live_fence = Path(released[0])
        F.store(self.live_fence, b"9" * 32 + b"\n")

    def verify(self):
        return C.verify(self.fixture.runtime, self.state.request, self.state.failed,
                        self.plan, self.applied, self.fixture.io)

    def test_valid_foreign_marker_and_active_journal_pass_without_mutating_them(self):
        tx = self.verify()
        self.assertEqual(tx.id, self.plan["transaction_id"])
        self.assertEqual(tx.config["services"]["omlx"]["revision"], 3)
        self.assertEqual(self.live_fence.read_bytes(), b"9" * 32 + b"\n")
        self.assertIn(b'"state":"running"', tx.journal.read_bytes())

    def test_original_fence_and_job_remain_blockers(self):
        F.store(self.live_fence, (self.plan["transaction_id"] + "\n").encode())
        with self.assertRaisesRegex(C.B.BindingError, "original_resource_fence_present"):
            self.verify()
        F.store(self.live_fence, b"b" * 32 + b"\n")
        job = self.fixture.locks / "consumer-jobs" / (self.plan["transaction_id"] + ".json")
        F.store(job, b"fixture")
        with self.assertRaisesRegex(C.B.BindingError, "original_admission_present"):
            self.verify()

    def test_configuration_journal_and_owner_drift_block_closure(self):
        path = self.fixture.prod / "owner.json"
        original = path.read_bytes()
        F.store(path, original + b"\n")
        with self.assertRaisesRegex(C.B.BindingError, "configuration_evidence_changed"):
            self.verify()
        F.store(path, original)
        self.generation = dict(self.generation, pid=self.generation["pid"] + 1)
        with self.assertRaisesRegex(C.B.BindingError, "generation_changed"):
            self.verify()
        self.generation = self.applied["owner"]
        F.store(self.fixture.prod / "operations.jsonl", b'{"type":"nonce"}\n')
        with self.assertRaisesRegex(C.B.BindingError, "historical_journal_changed"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
