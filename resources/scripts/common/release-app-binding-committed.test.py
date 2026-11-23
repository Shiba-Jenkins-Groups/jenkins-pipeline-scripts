"""Targeted synthetic cases for the owner #105 continuation contract."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("binding_committed_fixture", Path(__file__).with_name("release-app-binding-committed.py"))
entry = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = entry
spec.loader.exec_module(entry)

KEY = b"synthetic isolated continuation fixture key"
NOW = datetime(2026, 11, 23, 15, tzinfo=timezone.utc)
ADMISSION = entry.Admission("a" * 40, "b" * 64)
APPROVERS = {"synthetic-review-owner"}
RUN = entry.OwnerRun(entry.OWNER, 106, entry.COORDINATOR, 239)


def archive(status, commit, version, digest, **details):
    return entry.gate.canonical(entry.control.sign(
        {"kind": "runtime-deployment", "product": entry.gate.PRODUCT, "status": status, "commit": commit,
         "version": version, "digest": digest, **details}, KEY,
    ))


FAILED = archive("FAILED", entry.FAILED_COMMIT, "1.1.29", entry.FAILED_DIGEST,
                 mutation_may_have_started=True, automatic_rollback=False)
ORIGINAL = archive("SUCCESS", entry.ORIGINAL_COMMIT, "1.1.27", entry.APP_DIGEST)


class FixturePort:
    def __init__(self):
        self.events = []
        self.lease_value = entry.Lease("fixture-native-lock", entry.LOCKS)
        self.marker = entry.PENDING_SHA
        self.scope_value = {
            "transaction": entry.TRANSACTION, "phase": "committed-installation.json", "engine": "fixture-engine",
            "runtime_sha256": "c" * 64, "app_container": entry.APP_ID, "app_digest": entry.APP_DIGEST,
            "app_image_id": entry.APP_IMAGE_ID, "app_healthy": True, "files": dict(entry.FILES),
            "configs": dict(entry.CONFIGS), "fences": {"d" * 64: "e" * 64, "f" * 64: "1" * 64},
        }
        self.fences = dict(self.scope_value["fences"])
        self.proof = entry.OwnerProof(72000, "fixture-process-start", 501, entry.OWNER_PATH,
                                    entry.OWNER_BINARY, 72000, True)
        self.admission = ADMISSION
        self.lost_on_resume = False

    def registration(self):
        return self.admission

    def lifecycle(self):
        return "fixture-prod-lock"

    @contextmanager
    def hold_remaining(self):
        yield

    def lease(self):
        return self.lease_value

    def scope(self):
        return deepcopy(self.scope_value)

    def pending(self):
        return self.marker

    def active_fences(self):
        return dict(self.fences)

    def ready_owner(self):
        return self.proof

    def resume_committed(self, transaction, expected_hash, deadline):
        self.events.append(("resume", transaction, expected_hash))
        self.marker = None
        if self.lost_on_resume:
            self.lease_value = replace(self.lease_value, identity="changed-fixture-lock")

    def release_owned_fences(self, transaction, fences, proof, deadline):
        self.events.append(("release", transaction, proof.uid))
        self.fences.clear()


class CommittedBindingTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory(prefix="app-binding-committed-fixture-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name).resolve()
        (self.root / "promotion.lock").touch(mode=0o600)
        self.state = entry.control.State(self.root)
        self.state.write(entry.FAILED_COMMIT, json.loads(FAILED))
        self.state.write(entry.ORIGINAL_COMMIT, json.loads(ORIGINAL))
        self.native = FixturePort()
        pins = patch.multiple(entry, FAILED_SHA=entry.digest(FAILED), ORIGINAL_SHA=entry.digest(ORIGINAL))
        pins.start()
        self.addCleanup(pins.stop)
        self.request = self.issue()

    def issue(self, **changes):
        inputs = dict(job=entry.COORDINATOR, build=239, author="synthetic-review-owner", approvers=APPROVERS,
                      admitted=ADMISSION, scope=self.native.scope(), failed_raw=FAILED, original_raw=ORIGINAL,
                      key=KEY, now=NOW)
        return entry.coordinator_request(**(inputs | changes))

    def run_owner(self, **changes):
        inputs = dict(run=RUN, admitted=ADMISSION, approvers=APPROVERS, native=self.native, state=self.state,
                      failed_raw=FAILED, original_raw=ORIGINAL, key=KEY, clock=lambda: NOW)
        return entry.owner_finish(self.request, **(inputs | changes))

    def records(self):
        return [entry.signed_payload(json.loads(p.read_bytes()), KEY)
                for p in self.root.glob("*.json") if p.stem not in {entry.FAILED_COMMIT, entry.ORIGINAL_COMMIT}]

    def test_exact_native_readiness_finalizes_and_new_coordinator_build_cannot_replay(self):
        result = entry.signed_payload(self.run_owner(), KEY)
        self.assertEqual((result["status"], result["app_container"], result["owner_pid"]),
                         ("SUCCESS", entry.APP_ID, self.native.proof.pid))
        self.assertEqual(self.native.events, [("resume", entry.TRANSACTION, entry.PENDING_SHA),
                                              ("release", entry.TRANSACTION, 501)])
        self.assertEqual(self.records(), [result])
        self.request = self.issue(build=240)
        with self.assertRaisesRegex(ValueError, "already consumed"):
            self.run_owner(run=replace(RUN, upstream_build=240, build=107))
        self.assertEqual(len(self.native.events), 2)

    def test_uninstalled_driver_missing_scope_and_unsigned_job_do_not_consume(self):
        for settings in ({"native": None}, {"run": replace(RUN, upstream_build=240)}, {"key": b"z" * 32}):
            with self.subTest(settings=tuple(settings)), self.assertRaises(ValueError):
                self.run_owner(**settings)
        self.native.admission = entry.Admission("9" * 40, ADMISSION.driver_sha256)
        with self.assertRaisesRegex(ValueError, "not installed"):
            self.run_owner()
        with self.assertRaisesRegex(ValueError, "authorized incident operator"):
            self.issue(author="other-fixture")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.native.events, [])

    def test_under_lock_drift_old_receipt_or_expiry_rejects_before_resume(self):
        with self.assertRaisesRegex(ValueError, "exact archived"):
            self.run_owner(failed_raw=FAILED + b" ")
        with self.assertRaisesRegex(ValueError, "expired fixed continuation grant"):
            self.run_owner(clock=lambda: NOW + timedelta(minutes=5))
        self.native.marker = "0" * 64
        with self.assertRaisesRegex(ValueError, "original committed pending"):
            self.run_owner()
        self.native.marker = entry.PENDING_SHA
        self.native.scope_value["runtime_sha256"] = "2" * 64
        with self.assertRaisesRegex(ValueError, "drifted"):
            self.run_owner()
        self.assertEqual(self.records(), [])
        self.assertEqual(self.native.events, [])

    def test_missing_release_state_lock_is_not_created_by_continuation(self):
        lock = self.root / "promotion.lock"
        lock.unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_owner()
        self.assertFalse(lock.exists())
        self.assertEqual(self.records(), [])
        self.assertEqual(self.native.events, [])

    def test_root_launcher_cannot_release_fences_after_consumption(self):
        self.native.proof = replace(self.native.proof, uid=0, executable="/usr/libexec/xpcproxy")
        with self.assertRaisesRegex(ValueError, "exact admitted native owner generation"):
            self.run_owner()
        self.assertEqual(self.native.events, [("resume", entry.TRANSACTION, entry.PENDING_SHA)])
        self.assertEqual(self.native.fences, self.native.scope_value["fences"])
        self.assertEqual([record["status"] for record in self.records()], ["FAILED"])
        with self.assertRaisesRegex(ValueError, "already consumed"):
            self.run_owner()

    def test_lost_native_lock_after_resume_keeps_fences_and_failure_claim(self):
        self.native.lost_on_resume = True
        with self.assertRaisesRegex(ValueError, "retained owner locks changed"):
            self.run_owner()
        self.assertEqual(len(self.native.events), 1)
        self.assertEqual([record["status"] for record in self.records()], ["FAILED"])
        self.assertEqual(self.native.fences, self.native.scope_value["fences"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
