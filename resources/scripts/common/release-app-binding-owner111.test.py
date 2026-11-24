#!/usr/bin/env python3
"""Offline evidence and native protocol regression for admitted #111 recovery."""
import datetime as dt
import fcntl
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("owner111_under_test", HERE / "release-app-binding-owner111.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
fixtures = R.load("release-app-binding-incident.test.py", "owner111_fixtures")
F, I, B = fixtures.F, R.I, R.B


class SuccessfulPause(F.FakeIO):
    def pause(self, transaction):
        return F.B.NativeIO.pause(self, transaction)

    def _pause_observation(self, *args, **kwargs):
        return F.B.NativeIO._pause_observation(self, *args, **kwargs)

    def owner(self, transaction, **kwargs):
        return super().owner(transaction)

    def command(self, argv):
        assert argv == ["/bin/launchctl", "bootout", "gui/" + str(self.root.stat().st_uid) + "/" + B.LABEL]
        self.pauses += 1
        self.paused = True


class NativeReplay(fixtures.NativeReplay):
    def app(self, transaction, container):
        raw = super().app(transaction, container)
        raw["State"]["StartedAt"] = self.fixture.reviewed_app_start
        raw["RestartCount"] = 1
        return raw

    def command(self, argv):
        # Keep the interrupted deploy's checkpoint until resume has been
        # verified. Every resource lock/fence is still held by the recovery.
        assert self.fixture.job.is_file()
        return super().command(argv)


class Owner111Tests(unittest.TestCase):
    def setUp(self):
        F.BindingTests.setUp(self)
        start = dt.datetime.now(dt.timezone.utc)
        # The same old App restarted during a dependency change after #110
        # succeeded and before #111 began.
        previous_start = start - dt.timedelta(seconds=40)
        self.reviewed_app_start = previous_start.strftime("%Y-%m-%dT%H:%M:%S.%f") + "301Z"
        production_fields = dict(
            kind="deployment", schema_version=2, product=R.PRODUCT,
            commit=R.FAILED_COMMIT, version="1.1.35", digest=R.FAILED_DIGEST,
            library_revision=R.ORIGINAL_LIBRARY, deployment_script_revision=R.FAILED_COMMIT)
        self.key = b"synthetic-only never actual Jenkins key"
        self.request = dict(production_fields, prod_job=R.PRODUCT + "/prod", prod_build=234,
                            image="localhost:9290/" + R.PRODUCT + "/prod/1.1.35@" + R.FAILED_DIGEST,
                            promotion=R.control.sign({"develop_build": 400, "source_commit": R.FAILED_SOURCE,
                                                      "merge_commit": R.FAILED_COMMIT}, self.key),
                            created_at=(start - dt.timedelta(seconds=30)).isoformat(),
                            expires_at=(start + dt.timedelta(seconds=60)).isoformat())
        request_raw = self.signed(self.request)
        producer = SuccessfulPause(self.root)
        try:
            transaction = F.B.AppBinding.begin(self.request, self.runtime, self.lifecycle, io=producer)
        except F.B.BindingError as error:
            self.fail(error.receipt["code"])
        self.tx, self.backup, self.job = transaction.id, transaction.backup, transaction.job_path
        old = transaction.old_target
        old_container, old_image, old_key = old["id"], old["image_id"], transaction.old_key
        transaction.close()
        fcntl.flock(self.lifecycle, fcntl.LOCK_UN)
        self.authorized = B.document(self.backup / "authorized.json")
        self.claim = self.backup / R.Owner111Incident.claim_name
        self.failed = dict(production_fields, kind="runtime-deployment", schema_version=1,
                           status="FAILED", request_sha256=B.sha(request_raw), new_backup_and_migration_files=[],
                           automatic_rollback=False, mutation_may_have_started=True,
                           previous_container_id=old_container, previous_image_id=old_image,
                           started_at=start.isoformat(), finished_at=(start + dt.timedelta(seconds=30)).isoformat())
        self.good = dict(production_fields, kind="runtime-deployment", schema_version=1,
                         commit=R.GOOD_COMMIT, deployment_script_revision=R.GOOD_COMMIT,
                         version="1.1.34", digest=R.GOOD_DIGEST, status="SUCCESS",
                         finished_at=(start - dt.timedelta(seconds=45)).isoformat(),
                         app_binding={"status": "completed", "new_app_key": old_key},
                         observed={"commit": R.GOOD_COMMIT, "version": "1.1.34", "digest": R.GOOD_DIGEST,
                                   "compose_project": "shiba-goditch-prod", "container_id": old_container, "image_id": old_image})
        originals = {"failed_request": request_raw, "failed_receipt": self.signed(self.failed),
                     "last_good_receipt": self.signed(self.good)}
        self.pins = {name: B.sha(raw) for name, raw in originals.items()}
        settings = mock.patch.multiple(R, TX=self.tx, OLD_CONTAINER=old_container, OLD_IMAGE=old_image,
                                       OLD_STARTED_AT=self.reviewed_app_start, HASHES=self.pins)
        settings.start()
        self.addCleanup(settings.stop)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        F.store(self.state / "promotion.lock", b"")
        self.failed_state = self.state / (R.FAILED_COMMIT + ".json")
        F.store(self.failed_state, originals["failed_receipt"])
        F.store(self.state / (R.GOOD_COMMIT + ".json"), originals["last_good_receipt"])
        self.artifacts = {name: self.base / (name + ".json") for name in originals}
        for name, raw in originals.items():
            F.store(self.artifacts[name], raw)
        self.io = NativeReplay(self)
        self.addCleanup(self.io.close)
        self.metadata, self.collection, self.review = (self.base / (name + ".json")
                                                        for name in ("metadata", "collection", "plan"))
        self.revision = "0" * 40
        self.source = lambda revision: {"revision": revision, "files": {name: "0" * 64 for name in R.SOURCE_FILES}}
        self.journal = self.prod / "operations.jsonl"

    def signed(self, data):
        return B.encoded(R.control.sign(data, self.key))

    def plan(self):
        value = R.prepare_plan(self.runtime, self.state, self.artifacts, self.key, self.revision,
                               self.metadata, self.collection, io=self.io, verify_source=self.source)
        F.store(self.review, value)
        return value

    def apply(self, key=None):
        with mock.patch.object(B.subprocess, "run", side_effect=self.io.lsof):
            return I.apply(self.review, B.sha(self.review.read_bytes()), io=self.io, verify_source=self.source,
                           incident_factory=R.incident_factory(key or self.key))

    def markers(self):
        return sorted(self.locks.glob("*.pending"))

    def test_plan_is_read_only_apply_archives_exact_objects_and_replays_no_deployment(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.plan()
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(self.io.bootstraps, 0)
        pending = self.prod / "owner-upgrade.pending.json"
        original_job = self.job.stat().st_ino
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["deployment_performed"])
        self.assertEqual(self.io.bootstraps, 1)
        self.assertFalse(self.markers())
        self.assertFalse(self.job.exists())
        self.assertFalse(pending.exists())
        self.assertEqual((self.claim / "consumer-admission.archived.json").read_bytes(), before[self.job])
        self.assertEqual((self.claim / "consumer-admission.archived.json").stat().st_ino, original_job)
        self.assertEqual((self.claim / "owner-upgrade.pending.archived.json").read_bytes(), before[pending])
        self.assertEqual({path: path.read_bytes() for path in before if path not in
                          {*map(Path, result["resource_fences_released"]), self.job, pending, self.journal}},
                         {path: raw for path, raw in before.items() if path not in
                          {*map(Path, result["resource_fences_released"]), self.job, pending, self.journal}})
        self.assertTrue(self.journal.read_bytes().startswith(before[self.journal]))
        self.assertEqual(self.apply()["code"], "incident_once_claim_exists_no_retry")

    def test_resume_uncertain_keeps_all_resource_fences_and_consumer_checkpoint(self):
        self.plan()
        originals = self.markers()
        self.io.fault = "timeout"
        result = self.apply()
        self.assertEqual(result["status"], "outcome-unconfirmed")
        self.assertTrue(result["resume_submitted"])
        self.assertTrue(result["installation_pending_archived"])
        self.assertTrue(self.job.is_file())
        self.assertEqual(self.markers(), originals)
        self.assertEqual(self.apply()["code"], "incident_once_claim_exists_no_retry")
        self.assertEqual(self.io.bootstraps, 1)

    def test_unsigned_or_substituted_receipt_never_qualifies(self):
        artifact = self.artifacts["failed_receipt"]
        raw = artifact.read_bytes()
        F.store(artifact, raw + b"\n")
        with self.assertRaisesRegex(B.BindingError, "owner111_archive_hash_changed"):
            self.plan()
        F.store(artifact, self.failed)
        self.pins["failed_receipt"] = B.sha(artifact.read_bytes())
        with self.assertRaisesRegex(B.BindingError, "owner111_invalid_signed_envelope"):
            self.plan()
        self.assertEqual(self.io.bootstraps, 0)

    def test_durable_state_must_match_exact_signed_receipt_in_each_phase(self):
        self.plan()
        F.store(self.failed_state, self.signed(dict(self.failed, status="CLAIMED")))
        result = self.apply()
        self.assertEqual(result["code"], "owner111_live_release_state_changed")
        self.assertFalse(self.claim.exists())
        self.assertTrue(self.job.exists())

    def test_foreign_consumer_checkpoint_blocks_even_with_original_checkpoint(self):
        self.plan()
        other_job = self.job.with_name("foreign.json")
        F.store(other_job, {})
        self.assertEqual(self.apply()["code"], "incident_foreign_fence_or_job")
        self.assertTrue(other_job.is_file())
        self.assertEqual(self.io.bootstraps, 0)

    def test_missing_admitted_checkpoint_or_later_phase_blocks(self):
        self.plan()
        extra = self.backup / "new-intent-written.json"
        F.store(extra, {})
        self.assertEqual(self.apply()["code"], "owner111_backup_phase_changed")
        self.assertTrue(extra.exists())
        extra.unlink()
        self.job.unlink()
        self.assertEqual(self.apply()["status"], "blocked")
        self.assertFalse(self.claim.exists())

    def test_changed_or_replaced_old_app_preserves_fences_and_checkpoint(self):
        self.plan()
        for fault in ("replacement", "app-unhealthy"):
            self.io.fault = fault
            result = self.apply()
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(self.markers())
            self.assertTrue(self.job.exists())
        self.assertEqual(self.io.bootstraps, 0)

    def test_same_container_restart_after_failed_attempt_began_is_rejected(self):
        app = self.io.app

        def late_start(transaction, container):
            raw = app(transaction, container)
            raw["State"]["StartedAt"] = self.failed["started_at"]
            return raw

        self.io.app = late_start
        with self.assertRaisesRegex(B.BindingError, "owner111_healthy_old_app_generation_required"):
            self.plan()
        self.assertFalse(self.claim.exists())

    def test_later_journal_record_blocks_even_if_terminal(self):
        self.plan()
        with self.journal.open("ab") as file:
            file.write(B.encoded({"type": "nonce", "nonce": "fixture-changed-journal"}) + b"\n")
        self.assertEqual(self.apply()["code"], "owner111_journal_changed_after_pause")
        self.assertEqual(self.io.bootstraps, 0)

    def test_hmac_is_reverified_at_apply_even_with_known_artifact_hashes(self):
        self.plan()
        result = self.apply(key=b"a different synthetic test key only")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.claim.exists())
        self.assertEqual(self.io.bootstraps, 0)


if __name__ == "__main__":
    unittest.main()
