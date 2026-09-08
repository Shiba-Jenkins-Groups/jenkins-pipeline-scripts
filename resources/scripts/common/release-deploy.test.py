#!/usr/bin/env python3
"""Offline deployment contracts; Docker, network and source execution are fakes."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("deploy_runner", Path(__file__).with_name("release-deploy.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Deployment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / '.env.prod').touch()
        (self.root / 'data/db/app').mkdir(parents=True)
        (self.root / 'state').mkdir()
        self.key = b'offline-only-receipt-test-key-00000'
        self.now = dt.datetime.now(dt.timezone.utc)
        self.commit, self.digest = 'b' * 40, 'sha256:' + 'c' * 64
        self.product = runner.gate.PRODUCT
        promotion = {'kind': 'promotion', 'status': 'MERGED', 'product': self.product,
                     'source_commit': 'a' * 40, 'merge_commit': self.commit, 'version': '1.0.32',
                     'develop_job': self.product + '/develop'}
        self.request = {'schema_version': 2, 'kind': 'deployment', 'product': self.product,
                        'commit': self.commit, 'version': '1.0.32', 'digest': self.digest,
                        'image': f'localhost:9290/{self.product}/prod/1.0.32@{self.digest}',
                        'prod_job': self.product + '/prod', 'prod_build': 103,
                        'decision': {'decision': 'PASS'}, 'created_at': self.now.isoformat(),
                        'expires_at': (self.now + dt.timedelta(minutes=15)).isoformat(),
                        'promotion': runner.control.sign(promotion, self.key)}
        self.request['evidence_sha256'] = 'e' * 64
        self.request['finalization'] = runner.control.sign({'kind': 'finalization', 'status': 'SUCCESS', 'product': self.product,
            'commit': self.commit, 'digest': self.digest, 'version': '1.0.32', 'evidence_sha256': 'e' * 64}, self.key)
        self.container = {'Id': 'fake-container', 'Image': 'sha256:fake-image-id',
            'Config': {'Labels': {'com.docker.compose.project': runner.PROJECT, 'com.docker.compose.service': 'app'},
                       'Env': ['APP_ENV=prod', 'STORAGE_BUCKET=shiba-prod', 'APP_VERSION=1.0.32', 'BRANCH=prod']},
            'State': {'Running': True, 'Health': {'Status': 'healthy'}},
            'Mounts': [{'Type': 'bind', 'RW': True, 'Destination': runner.DB_DESTINATION,
                        'Source': str(self.root / 'data/db/app')}],
            'NetworkSettings': {'Ports': {'8090/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8090'}]}}}
        self.image = {'Id': self.container['Image'], 'RepoDigests': [self.request['image']],
            'Config': {'Labels': {'org.opencontainers.image.revision': self.commit, 'app.version': '1.0.32',
                                 'app.branch': 'prod', 'app.name': self.product}}}

    def signed(self):
        return runner.control.sign(self.request, self.key)

    def test_valid_request_requires_matching_nested_promotion(self):
        self.assertEqual(runner.request_identity(self.signed(), self.key, self.now)['commit'], self.commit)
        self.request['promotion']['payload']['merge_commit'] = 'f' * 40
        with self.assertRaises(ValueError):
            runner.request_identity(self.signed(), self.key, self.now)

    def test_tampered_request_blocks(self):
        signed = self.signed()
        signed['payload']['version'] = '1.0.33'
        with self.assertRaises(ValueError):
            runner.request_identity(signed, self.key, self.now)

    def test_missing_failed_or_mismatched_finalization_blocks(self):
        for field, value in [('status', 'FAILED'), ('commit', 'f' * 40), ('evidence_sha256', '0' * 64)]:
            final = dict(self.request['finalization']['payload'], **{field: value})
            signed = runner.control.sign(dict(self.request, finalization=runner.control.sign(final, self.key)), self.key)
            with self.subTest(field=field), self.assertRaises(ValueError):
                runner.request_identity(signed, self.key, self.now)
        old = runner.control.sign(dict(self.request, schema_version=1), self.key)
        with self.assertRaises(ValueError): runner.request_identity(old, self.key, self.now)

    def test_expired_future_and_overlong_handoff_block(self):
        for now in [self.now - dt.timedelta(seconds=1), self.now + dt.timedelta(minutes=15)]:
            with self.subTest(now=now), self.assertRaises(ValueError):
                runner.request_identity(self.signed(), self.key, now)
        self.request['expires_at'] = (self.now + dt.timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError):
            runner.request_identity(self.signed(), self.key, self.now)

    def test_wrong_repo_tag_only_dev_job_and_missing_decision_block(self):
        original = copy.deepcopy(self.request)
        for field, value in [('image', 'registry.invalid/app@' + self.digest), ('image', 'app:latest'),
                             ('prod_job', self.product + '/develop'), ('prod_build', True),
                             ('decision', {'decision': 'NEEDS_APPROVAL'})]:
            self.request = dict(original, **{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                runner.request_identity(self.signed(), self.key, self.now)

    def test_owner_classification_rejects_missing_ambiguous_stopped_wrong_mount(self):
        self.assertEqual(runner.target_container([self.container], self.root)['Id'], 'fake-container')
        for containers in [[], [self.container, self.container]]:
            with self.assertRaises(ValueError):
                runner.target_container(containers, self.root)
        self.container['State']['Running'] = False
        with self.assertRaises(ValueError):
            runner.target_container([self.container], self.root)
        self.container['State']['Running'] = True
        self.container['Mounts'][0]['Source'] = str(self.root / 'data/db/dev')
        with self.assertRaises(ValueError):
            runner.target_container([self.container], self.root)

    def test_other_active_container_mounting_parent_blocks(self):
        other = copy.deepcopy(self.container)
        other['Id'] = 'other'
        other['Config']['Labels'] = {}
        other['Mounts'][0]['Source'] = str(self.root / 'data')
        with self.assertRaises(ValueError):
            runner.target_container([self.container, other], self.root)

    def test_host_open_fds_and_inventory_errors_fail_closed(self):
        for rc, out, err in [(0, '123\n', ''), (1, '', 'permission denied'), (2, '', '')]:
            with patch.object(runner.subprocess, 'run', return_value=types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)):
                with self.assertRaises(ValueError):
                    runner.no_host_writer(self.root)
        with patch.object(runner.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1, stdout='', stderr='')):
            runner.no_host_writer(self.root)

    def test_image_commit_version_branch_and_digest_are_required(self):
        with patch.object(runner, 'docker_json', return_value=[self.image]):
            self.assertEqual(runner.image_identity(self.request), self.container['Image'])
        for field in ['org.opencontainers.image.revision', 'app.version', 'app.branch', 'app.name']:
            image = copy.deepcopy(self.image)
            image['Config']['Labels'][field] = 'wrong'
            with patch.object(runner, 'docker_json', return_value=[image]), self.assertRaises(ValueError):
                runner.image_identity(self.request)
        self.image['RepoDigests'] = []
        with patch.object(runner, 'docker_json', return_value=[self.image]), self.assertRaises(ValueError):
            runner.image_identity(self.request)

    def test_final_verification_needs_actual_image_version_health_and_port(self):
        for key, value in [('Image', 'old'), ('State', {'Running': True, 'Health': {'Status': 'starting'}}),
                           ('NetworkSettings', {'Ports': {'8090/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '8090'}]}})]:
            container = dict(self.container, **{key: value})
            with patch.object(runner, 'containers', return_value=[container]), \
                 patch.object(runner, 'image_identity', return_value=self.container['Image']), self.assertRaises(ValueError):
                runner.final_identity(self.request, self.container['Image'], self.root)

    def execution_patches(self, rc=0, head=None):
        def git(source, *args):
            if args[0] == 'remote':
                return runner.control.REMOTE
            if args[0] == 'rev-parse':
                return self.commit
            return ''
        patches = [patch.object(runner.sys, 'platform', 'darwin'), patch.object(runner.control, 'git', side_effect=git),
            patch.object(runner.control, 'heads', return_value=('a' * 40, head or self.commit)),
            patch.object(runner, 'run', return_value='fake-engine'), patch.object(runner, 'containers', return_value=[self.container]),
            patch.object(runner, 'no_host_writer'), patch.object(runner, 'image_identity', return_value=self.container['Image']),
            patch.object(runner, 'final_identity', return_value={'commit': self.commit, 'digest': self.digest}),
            patch.object(runner.subprocess, 'run', return_value=types.SimpleNamespace(returncode=rc))]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        return mocks[-1]

    def execute(self, output='receipt.json'):
        return runner.execute(self.signed(), self.key, self.root, self.root, runner.control.State(self.root / 'state'),
                              'fake-engine', self.now, self.root / output)

    def test_success_receipt_and_duplicate_never_redeploys(self):
        process = self.execution_patches()
        receipt = self.execute()
        self.assertEqual(runner.control.verify(receipt, self.key)['status'], 'SUCCESS')
        kwargs = process.call_args.kwargs
        self.assertTrue(kwargs['pass_fds'])
        self.assertEqual(kwargs['env']['SHIBA_EXPECTED_COMMIT'], self.commit)
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.execute('again.json')
        self.assertEqual(process.call_count, 1)

    def test_failed_deploy_leaves_signed_failed_state_without_rollback(self):
        process = self.execution_patches(rc=1)
        with self.assertRaisesRegex(ValueError, 'no automatic rollback'):
            self.execute()
        receipt = json.loads((self.root / 'receipt.json').read_bytes())
        payload = runner.control.verify(receipt, self.key)
        self.assertEqual(payload['status'], 'FAILED')
        self.assertFalse(payload['automatic_rollback'])
        self.assertEqual(process.call_count, 1)
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.execute('again.json')

    def test_advanced_prod_or_wrong_engine_never_starts_mutation(self):
        process = self.execution_patches(head='d' * 40)
        with self.assertRaisesRegex(ValueError, 'advanced'):
            self.execute()
        process.assert_not_called()
        self.assertFalse(list((self.root / 'state').glob('*.json')))


if __name__ == '__main__':
    unittest.main()
