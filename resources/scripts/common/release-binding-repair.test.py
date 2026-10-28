#!/usr/bin/env python3
"""Offline authenticated maintenance contracts; no network, Docker or services."""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('repair_runner', Path(__file__).with_name('release-binding-repair.py'))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.key = b'LOCAL-only-binding-repair-fixture-key'
        self.now = dt.datetime.now(dt.timezone.utc)
        self.library = 'f' * 40
        self.commit, self.digest = 'b' * 40, 'sha256:' + 'c' * 64
        issued = self.now - dt.timedelta(hours=2)
        promotion = {'kind': 'promotion', 'status': 'MERGED', 'product': R.gate.PRODUCT,
                     'source_commit': 'a' * 40, 'merge_commit': self.commit, 'version': '1.1.17',
                     'develop_job': R.gate.PRODUCT + '/develop', 'develop_build': 362}
        original = {'schema_version': 2, 'kind': 'deployment', 'product': R.gate.PRODUCT,
                    'commit': self.commit, 'version': '1.1.17', 'digest': self.digest,
                    'image': 'localhost:9290/' + R.gate.PRODUCT + '/prod/1.1.17@' + self.digest,
                    'prod_job': R.gate.PRODUCT + '/prod', 'prod_build': 213,
                    'decision': {'decision': 'PASS'}, 'created_at': issued.isoformat(),
                    'expires_at': (issued + dt.timedelta(minutes=15)).isoformat(),
                    'promotion': R.control.sign(promotion, self.key), 'evidence_sha256': 'e' * 64,
                    'library_revision': '1' * 40, 'deployment_script_revision': self.commit}
        original['finalization'] = R.control.sign({'kind': 'finalization', 'status': 'SUCCESS',
            'product': R.gate.PRODUCT, 'commit': self.commit, 'digest': self.digest,
            'version': '1.1.17', 'evidence_sha256': 'e' * 64}, self.key)
        signed_original = R.control.sign(original, self.key)
        self.observed = {'container_id': 'd' * 64, 'image_id': 'sha256:' + 'e' * 64,
                         'commit': self.commit, 'version': '1.1.17', 'digest': self.digest,
                         'compose_project': R.deploy.PROJECT, 'heartbeat': 200}
        prior = {'schema_version': 1, 'kind': 'runtime-deployment', 'product': R.gate.PRODUCT,
                 'status': 'SUCCESS', 'automatic_rollback': False, 'request_sha256': R.sha(signed_original),
                 'commit': self.commit, 'version': '1.1.17', 'digest': self.digest,
                 'deployment_script_revision': self.commit, 'library_revision': '1' * 40,
                 'previous_container_id': '9' * 64, 'previous_image_id': 'sha256:' + '8' * 64,
                 'observed': copy.deepcopy(self.observed),
                 'started_at': (issued + dt.timedelta(minutes=1)).isoformat(),
                 'finished_at': (issued + dt.timedelta(minutes=3)).isoformat()}
        self.payload = {'schema_version': 1, 'kind': 'app-binding-repair', 'product': R.gate.PRODUCT,
                        'library_revision': self.library, 'created_at': self.now.isoformat(),
                        'expires_at': (self.now + dt.timedelta(minutes=15)).isoformat(),
                        'operation_id': '12345678-1234-4123-8123-123456789abc', 'authorized_by': 'fixture-user',
                        'coordinator_build': 201, 'binding_hashes': {n: 'a' * 64 for n in R.PINS},
                        'original': {'coordinator_build': 200, 'owner_build': 92,
                                     'request': signed_original, 'receipt': R.control.sign(prior, self.key)}}

    def signed(self):
        return R.control.sign(self.payload, self.key)

    def validate(self):
        return R.request_identity(self.signed(), self.key, self.now, self.library)

    def test_fresh_maintenance_does_not_renew_expired_deployment_permission(self):
        self.assertEqual(self.validate()['kind'], 'app-binding-repair')
        with self.assertRaisesRegex(ValueError, 'expired'):
            R.deploy.request_identity(self.payload['original']['request'], self.key, self.now)

    def test_signature_nested_tampering_and_library_mismatch_block(self):
        signed = self.signed()
        signed['payload']['binding_hashes']['owner.json'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'signature'):
            R.request_identity(signed, self.key, self.now, self.library)
        self.payload['original']['receipt']['payload']['observed']['container_id'] = '7' * 64
        with self.assertRaisesRegex(ValueError, 'signature'):
            self.validate()
        self.setUp()
        with self.assertRaisesRegex(ValueError, 'library'):
            R.request_identity(self.signed(), self.key, self.now, '0' * 40)

    def test_expired_future_and_overlong_maintenance_block(self):
        for now in (self.now - dt.timedelta(seconds=1), self.now + dt.timedelta(minutes=15)):
            with self.subTest(now=now), self.assertRaisesRegex(ValueError, 'expired'):
                R.request_identity(self.signed(), self.key, now, self.library)
        self.payload['expires_at'] = (self.now + dt.timedelta(minutes=16)).isoformat()
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.validate()

    def test_exact_hash_set_canonical_uuid_and_no_extra_fields(self):
        original = copy.deepcopy(self.payload)
        for update in ({'binding_hashes': {}}, {'operation_id': '../unsafe'},
                       {'operation_id': '12345678-1234-1123-8123-123456789abc'},
                       {'coordinator_build': True}, {'authorized_by': ''}, {'deploy': True}):
            self.payload = dict(copy.deepcopy(original), **update)
            with self.subTest(update=update), self.assertRaises((ValueError, TypeError, AttributeError)):
                self.validate()

    def test_original_failed_bound_recursive_wrong_runtime_or_digest_block(self):
        original = copy.deepcopy(self.payload)
        for changes in ({'status': 'FAILED'}, {'app_binding': {'status': 'completed'}},
                        {'mode': 'restore'}, {'recovery': {}}, {'request_sha256': '0' * 64},
                        {'digest': 'sha256:' + '7' * 64},
                        {'previous_container_id': self.observed['container_id']},
                        {'observed': dict(self.observed, heartbeat=500)}):
            self.payload = copy.deepcopy(original)
            prior = dict(self.payload['original']['receipt']['payload'], **changes)
            self.payload['original']['receipt'] = R.control.sign(prior, self.key)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate()

    def test_claim_fingerprint_ignores_new_authorization_uuid(self):
        first = R.state_key(self.payload)
        self.payload['operation_id'] = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        self.assertEqual(first, R.state_key(self.payload))
        self.payload['binding_hashes']['owner.json'] = 'b' * 64
        self.assertNotEqual(first, R.state_key(self.payload))

    def test_collect_requires_exact_completed_owner_upstream_and_author(self):
        upstream = {'actions': [{'causes': [{'_class': 'hudson.model.Cause$UpstreamCause',
                    'upstreamProject': R.COORDINATOR, 'upstreamBuild': 200}]}]}
        responses = [{'number': 200, 'building': False, 'result': 'SUCCESS'},
                     dict(upstream, number=92, building=False, result='SUCCESS'),
                     self.payload['original']['request'], self.payload['original']['receipt']]
        def collect():
            return R.collect('https://fixture.invalid', 200, 92, self.payload['binding_hashes'],
                             {'approvers': ['fixture-user'], 'library_revision': self.library},
                             self.key, self.now, 'fixture-user', 201)
        with patch.object(R.adapter, 'jenkins_get', side_effect=[json.dumps(v).encode() for v in responses]) as fetch:
            self.assertEqual(R.control.verify(collect(), self.key)['original']['owner_build'], 92)
            self.assertEqual(fetch.call_count, 4)
        responses[1]['actions'][0]['causes'][0]['upstreamBuild'] = 199
        with patch.object(R.adapter, 'jenkins_get', side_effect=[json.dumps(v).encode() for v in responses]), self.assertRaises(ValueError):
            collect()
        with patch.object(R.adapter, 'jenkins_get') as fetch, self.assertRaisesRegex(ValueError, 'author'):
            R.collect('https://fixture.invalid', 200, 92, self.payload['binding_hashes'],
                      {'approvers': [], 'library_revision': self.library}, self.key, self.now, 'fixture-user', 201)
        fetch.assert_not_called()

    def execution_fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = Path(temp.name).resolve()
        (runtime / 'data/locks').mkdir(parents=True)
        (runtime / 'state').mkdir(mode=0o700)
        lock = runtime / 'data/locks/prod.lock'
        lock.touch(mode=0o600)
        state = R.control.State(runtime / 'state')
        state.write(self.commit, self.payload['original']['receipt'])
        helper = types.SimpleNamespace(NativeIO=lambda: object(), directory=lambda p: p,
            AppBinding=types.SimpleNamespace(repair=Mock(return_value={'status': 'completed', 'mode': 'ready-repair'})))
        for target, name, value in [(R.sys, 'platform', 'darwin'), (R, 'module', helper),
            (R.deploy, 'run', 'fixture-engine'), (R.deploy, 'containers', []),
            (R.deploy, 'target_container', {'Id': self.observed['container_id'], 'Image': self.observed['image_id']}),
            (R.deploy, 'image_identity', self.observed['image_id'])]:
            p = patch.object(target, name, return_value=value) if name != 'platform' else patch.object(target, name, value)
            p.start(); self.addCleanup(p.stop)
        return runtime, state, helper

    def execute(self, runtime, state, filename='repair.json'):
        return R.execute(self.signed(), self.key, runtime, state, 'fixture-engine', self.now,
                         self.library, runtime / filename)

    def test_owner_uses_original_state_and_never_overwrites_deployment_receipt(self):
        runtime, state, helper = self.execution_fixture()
        before = state.path(self.commit).read_bytes()
        result = self.execute(runtime, state)
        self.assertEqual(R.control.verify(result, self.key)['kind'], 'app-binding-maintenance')
        self.assertFalse(result['payload']['app_deployed'])
        self.assertEqual(state.path(self.commit).read_bytes(), before)
        verified, observed = helper.AppBinding.repair.call_args.args[:2]
        self.assertEqual(verified['previous_container_id'], '9' * 64)
        self.assertEqual(observed, self.observed)
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.execute(runtime, state, 'again.json')
        self.assertEqual(helper.AppBinding.repair.call_count, 1)

    def test_original_state_or_current_image_drift_cannot_call_native_repair(self):
        runtime, state, helper = self.execution_fixture()
        changed = copy.deepcopy(self.payload['original']['receipt'])
        changed['payload']['status'] = 'FAILED'
        state.write(self.commit, changed)
        with self.assertRaisesRegex(ValueError, 'owner state'):
            self.execute(runtime, state)
        helper.AppBinding.repair.assert_not_called()
        state.write(self.commit, self.payload['original']['receipt'])
        R.deploy.image_identity.return_value = 'sha256:' + '7' * 64
        with self.assertRaisesRegex(ValueError, 'current App'):
            self.execute(runtime, state)
        helper.AppBinding.repair.assert_not_called()
        self.assertEqual(state.read(R.state_key(self.payload))['payload']['status'], 'FAILED')

    def test_uncertain_native_failure_preserves_failed_claim_and_never_retries(self):
        runtime, state, helper = self.execution_fixture()
        error = ValueError('fixture pending preserved')
        error.receipt = {'status': 'pending-preserved', 'owner_paused': True}
        helper.AppBinding.repair.side_effect = error
        with self.assertRaises(ValueError):
            self.execute(runtime, state)
        failed = R.control.verify(state.read(R.state_key(self.payload)), self.key)
        self.assertEqual(failed['status'], 'FAILED')
        self.assertTrue(failed['app_binding']['owner_paused'])
        self.assertFalse(failed['automatic_retry'])
        self.payload['operation_id'] = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.execute(runtime, state, 'again.json')
        self.assertEqual(helper.AppBinding.repair.call_count, 1)


if __name__ == '__main__':
    unittest.main()
