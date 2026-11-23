"""Offline test of fixed Jenkins archive handoff and exact owner dispatch."""

from contextlib import contextmanager
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("committed_job_test_entry", Path(__file__).with_name("release-app-binding-committed-job.py"))
job = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = job
spec.loader.exec_module(job)
entry = job.entry
fixture = job.load("committed_job_owner_fixture", "release-app-binding-committed.test.py")
fixture.entry = entry
KEY, NOW, FAILED, ORIGINAL = fixture.KEY, fixture.NOW, fixture.FAILED, fixture.ORIGINAL
APPROVER = "synthetic-review-owner"
ADMISSION = entry.Admission("a" * 40, "b" * 64)
BASE_URL = "http://fixture.invalid"


def completed(number, result, upstream=None, queue=None):
    causes = [{"_class": "hudson.model.Cause$UpstreamCause", "upstreamProject": entry.COORDINATOR,
               "upstreamBuild": upstream}] if upstream is not None else []
    return {"number": number, "building": False, "result": result, "queueId": queue,
            "actions": [{"causes": causes}]}


class CommittedJobTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="app-binding-105-job-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state_path = self.root / "state"
        self.state_path.mkdir(mode=0o700)
        (self.state_path / "promotion.lock").touch(mode=0o600)
        self.state = entry.control.State(self.state_path)
        self.state.write(entry.FAILED_COMMIT, json.loads(FAILED))
        self.state.write(entry.ORIGINAL_COMMIT, json.loads(ORIGINAL))
        self.native = fixture.FixturePort()
        self.native.admission = ADMISSION
        self.api = {}
        for c, o, outcome in ((238, 105, "FAILURE"), (231, 103, "SUCCESS")):
            self.api_build(entry.COORDINATOR, c, completed(c, outcome))
            self.api_build(entry.OWNER, o, completed(o, outcome, c, 15298 if o == 105 else None))
        self.api_file(entry.OWNER, 105, "runtime-receipt.json", FAILED)
        self.api_file(entry.OWNER, 103, "runtime-receipt.json", ORIGINAL)
        for change in (patch.multiple(entry, FAILED_SHA=entry.digest(FAILED), ORIGINAL_SHA=entry.digest(ORIGINAL)),
                       patch.object(job, "clock", return_value=NOW),
                       patch.object(job.repair.adapter, "jenkins_get", side_effect=self.get)):
            change.start()
            self.addCleanup(change.stop)
        self.policy = self.root / "policy.json"
        job.repair.write(self.policy, {"library_revision": ADMISSION.library_revision,
                                      "product": entry.gate.PRODUCT, "approvers": [APPROVER]})

    def get(self, base, suffix):
        self.assertEqual(base, BASE_URL)
        return self.api[suffix]

    def api_build(self, name, number, value):
        self.api[job.repair.prefix(name, number) + "api/json"] = entry.gate.canonical(value)

    def api_file(self, name, number, filename, raw):
        self.api[job.repair.prefix(name, number) + "artifact/" + filename] = raw

    def collect(self, build, value):
        review = self.root / f"review-{build}.json"
        request = self.root / f"request-{build}.json"
        history = self.root / f"history-{build}.json"
        job.repair.write(review, value)
        args = SimpleNamespace(job=entry.COORDINATOR, current_build=build, author=APPROVER, policy=self.policy,
                               jenkins_url=BASE_URL, review=review, output=request, history_output=history)
        with patch.dict(os.environ, {"JOB_NAME": entry.COORDINATOR, "BUILD_NUMBER": str(build)}, clear=True):
            job.coordinator_collect(args, ADMISSION, KEY)
        return job.repair.read(request), job.decode_history(job.repair.read(history), KEY)

    def dispatch(self, owner, coordinator, request, history):
        args = SimpleNamespace(job=entry.OWNER, owner_build=owner, upstream_job=entry.COORDINATOR,
                               upstream_build=coordinator, docker_engine_id=self.native.scope()["engine"],
                               state_directory=self.state_path, runtime_root=self.root,
                               output=self.root / f"receipt-{owner}.json")

        @contextmanager
        def retained_fixture(runtime, state):
            self.assertEqual((runtime, state.root), (self.root, self.state_path))
            yield 30

        def driver(entry_module, runtime, fd, admission, *, engine_id):
            self.assertEqual((entry_module, runtime, fd, admission, engine_id),
                             (entry, self.root, 30, ADMISSION, "fixture-engine"))
            return self.native

        grant = entry.request_identity(request, admitted=ADMISSION, approvers=[APPROVER],
                                       upstream_build=coordinator, key=KEY, now=NOW)
        with patch.dict(os.environ, {"BUILD_NUMBER": str(owner)}, clear=True),              patch.object(job, "retained_lifecycle", retained_fixture),              patch.object(job, "load", return_value=SimpleNamespace(NativeContinuation=driver)):
            job.owner_dispatch(args, request, grant, [APPROVER], history, ADMISSION, KEY)
        return job.repair.read(args.output)

    def archived_observation(self):
        request, history = self.collect(239, {"intent": "observe"})
        receipt = self.dispatch(106, 239, request, history)
        self.api_build(entry.COORDINATOR, 239, completed(239, "SUCCESS"))
        self.api_build(entry.OWNER, 106, completed(106, "SUCCESS", 239))
        self.api_file(entry.COORDINATOR, 239, "deployment-request.json", entry.gate.canonical(request))
        self.api_file(entry.OWNER, 106, "runtime-receipt.json", entry.gate.canonical(receipt))
        return receipt

    def test_observation_has_no_claim_and_fixed_archived_scope_can_finalize(self):
        observation = self.archived_observation()
        self.assertEqual(observation["payload"]["status"], "OBSERVED")
        self.assertFalse(self.state.exists(entry.claim_key()))
        self.assertEqual(self.native.events, [])
        request, history = self.collect(240, observation)
        result = self.dispatch(107, 240, request, history)
        verified = job.verify_result(request, result, admission=ADMISSION, api_approvers=[APPROVER],
                                     upstream_build=240, owner_build=107, key=KEY)
        self.assertEqual(verified["status"], "SUCCESS")
        self.assertEqual([action[0] for action in self.native.events], ["resume", "release"])
        self.assertTrue(self.state.exists(entry.claim_key()))

    def test_wrong_or_expired_scope_cannot_be_reviewed_on_signed_finish_path(self):
        receipt = self.archived_observation()
        with self.assertRaisesRegex(ValueError, "expired"):
            job.reviewed_scope(BASE_URL, receipt, ADMISSION, APPROVER, KEY, NOW + timedelta(minutes=15))
        wrong_owner_job = completed(106, "SUCCESS", 240)
        self.api_build(entry.OWNER, 106, wrong_owner_job)
        with self.assertRaisesRegex(ValueError, "upstream mismatch"):
            job.reviewed_scope(BASE_URL, receipt, ADMISSION, APPROVER, KEY, NOW)
        self.assertFalse(self.state.exists(entry.claim_key()))
        self.assertEqual(self.native.events, [])

    def test_observation_requires_exact_persistent_old_receipt(self):
        request, history = self.collect(239, {"intent": "observe"})
        self.state.write(entry.FAILED_COMMIT, json.loads(ORIGINAL))
        with self.assertRaisesRegex(ValueError, "persistent #105 or #103"):
            self.dispatch(106, 239, request, history)
        self.assertFalse(self.state.exists(entry.claim_key()))
        self.assertEqual(self.native.events, [])

    def test_inspect_verifies_wrapper_identity_before_loading_native_adapter(self):
        self.collect(239, {"intent": "observe"})
        approvers = self.root / "approvers.json"
        job.repair.write(approvers, [APPROVER])
        args = SimpleNamespace(command="inspect", approvers=approvers, request=self.root / "request-239.json",
                               upstream_build=239, history=self.root / "history-239.json",
                               output=self.root / "identity.json")
        with patch.dict(os.environ, {"JOB_NAME": entry.OWNER}, clear=True), patch.object(job, "load") as loader:
            job.owner_command(args, ADMISSION, KEY)
        loader.assert_not_called()
        identity = job.repair.read(args.output)
        self.assertEqual((identity["kind"], identity["library_revision"], identity["driver_sha256"]),
                         (entry.OBSERVE, ADMISSION.library_revision, ADMISSION.driver_sha256))
        self.assertFalse(self.state.exists(entry.claim_key()))

    def test_failed_mutation_archives_exact_failure_without_releasing_or_retrying(self):
        receipt = self.archived_observation()
        request, history = self.collect(240, receipt)
        self.native.lost_on_resume = True
        with self.assertRaisesRegex(ValueError, "retained owner locks changed"):
            self.dispatch(107, 240, request, history)
        archived = job.repair.read(self.root / "receipt-107.json")
        self.assertEqual(archived, self.state.read(entry.claim_key()))
        self.assertEqual(archived["payload"]["status"], "FAILED")
        self.assertEqual([action[0] for action in self.native.events], ["resume"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
