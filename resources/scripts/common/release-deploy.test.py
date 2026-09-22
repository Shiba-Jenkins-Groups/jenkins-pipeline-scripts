#!/usr/bin/env python3
"""Offline deployment contracts; Docker, network and source execution are fakes."""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
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
        for rc, out, err in [(0, 'p123\ncsqlite3\n', ''), (1, '', 'permission denied'), (2, '', '')]:
            with patch.object(runner.subprocess, 'run', return_value=types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)):
                with self.assertRaises(ValueError):
                    runner.no_host_writer(self.root)
        with patch.object(runner.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1, stdout='', stderr='')):
            runner.no_host_writer(self.root)

    def test_docker_vm_file_descriptors_are_the_validated_owner_domain(self):
        allowed = '/Applications/Docker.app/Contents/MacOS/com.docker.backend services\n'
        def process(command, **kwargs):
            if command[0] == 'lsof':
                return types.SimpleNamespace(returncode=0, stdout='p123\nccom.docke\n', stderr='')
            return types.SimpleNamespace(returncode=0, stdout=allowed, stderr='')
        with patch.object(runner.subprocess, 'run', side_effect=process):
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
            patch.object(runner, 'no_host_writer'), patch.object(runner, 'image_identity', return_value=self.image['Id']),
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

    def recovery_fixture(self):
        self.original_request = self.signed()
        self.container['Id'] = '1' * 64
        self.container['Image'] = 'sha256:' + '2' * 64
        self.image['Id'] = 'sha256:' + '3' * 64
        failed = {'schema_version': 1, 'kind': 'runtime-deployment', 'product': self.product,
            'status': 'FAILED', 'request_sha256': hashlib.sha256(runner.gate.canonical(self.original_request)).hexdigest(),
            'commit': self.commit, 'version': self.request['version'], 'digest': self.digest,
            'previous_container_id': self.container['Id'], 'previous_image_id': self.container['Image'],
            'automatic_rollback': False, 'mutation_may_have_started': True,
            'new_backup_and_migration_files': []}
        signed = runner.control.sign(failed, self.key)
        self.request['recovery'] = {'kind': 'failed-deployment-retry', 'attempt_id': '12345678-1234-4abc-8abc-123456789abc', 'coordinator_build': 54,
            'owner_build': 18, 'authorized_by': 'offline-explicit-owner-authorization', 'failed_receipt': signed}
        runner.control.State(self.root / 'state').write(self.commit, signed)
        return signed

    def archive_path(self, signed):
        digest = hashlib.sha256(runner.gate.canonical(signed)).hexdigest()
        return self.root / 'state/attempts' / f'{self.commit}-{digest}.json'

    def test_recovery_preserves_original_before_claim_and_runs_exactly_once_under_both_locks(self):
        failed = self.recovery_fixture()
        process = self.execution_patches()
        archive = self.archive_path(failed)
        state = runner.control.State(self.root / 'state')
        def deploy(*args, **kwargs):
            self.assertEqual(archive.read_bytes(), runner.gate.canonical(failed))
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
            claim = runner.control.verify(state.read(self.commit), self.key)
            self.assertEqual(claim['status'], 'CLAIMED')
            self.assertEqual(claim['recovery'], self.request['recovery'])
            self.assertEqual(claim['failed_receipt_sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(claim['request_sha256'], hashlib.sha256(runner.gate.canonical(self.signed())).hexdigest())
            self.assertNotEqual(claim['request_sha256'], failed['payload']['request_sha256'])
            for path in (self.root / 'data/locks/prod.lock', self.root / 'state/promotion.lock'):
                fd = os.open(path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        runner.fcntl.flock(fd, runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            return types.SimpleNamespace(returncode=0)
        process.side_effect = deploy
        receipt = self.execute()
        result = runner.control.verify(receipt, self.key)
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['recovery']['failed_receipt'], failed)
        self.assertEqual(state.read(self.commit), receipt)
        self.assertEqual(archive.read_bytes(), runner.gate.canonical(failed))
        with self.assertRaisesRegex(ValueError, 'state changed'):
            self.execute('replay.json')
        self.assertEqual(process.call_count, 1)
        self.request.pop('recovery')
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.execute('original-replay.json')
        self.assertEqual(process.call_count, 1)

    def test_recovery_new_failure_preserves_original_and_blocks_all_automatic_replay(self):
        failed = self.recovery_fixture()
        process = self.execution_patches(rc=1)
        with self.assertRaisesRegex(ValueError, 'no automatic rollback'):
            self.execute()
        new = runner.control.State(self.root / 'state').read(self.commit)
        payload = runner.control.verify(new, self.key)
        self.assertEqual(payload['status'], 'FAILED')
        self.assertEqual(payload['recovery']['failed_receipt'], failed)
        self.assertEqual(self.archive_path(failed).read_bytes(), runner.gate.canonical(failed))
        with self.assertRaisesRegex(ValueError, 'state changed'):
            self.execute('repeat.json')
        self.request['recovery']['failed_receipt'] = new
        with self.assertRaisesRegex(ValueError, 'recursive'):
            self.execute('recursive.json')
        self.assertEqual(process.call_count, 1)

    def test_recovery_contract_rejects_invalid_authorization_coordinates_and_envelope(self):
        self.recovery_fixture()
        good = copy.deepcopy(self.request['recovery'])
        cases = [('kind', 'retry'), ('attempt_id', ''), ('attempt_id', '12345678-1234-4ABC-8ABC-123456789ABC'),
                 ('attempt_id', '12345678-1234-1abc-8abc-123456789abc'), ('attempt_id', None),
                 ('coordinator_build', 0), ('coordinator_build', True),
                 ('owner_build', -1), ('owner_build', '18'), ('authorized_by', ''),
                 ('authorized_by', ' '), ('authorized_by', 'owner\n'), ('authorized_by', '\x00owner'),
                 ('failed_receipt', {}), ('failed_receipt', None)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.request['recovery'] = dict(good, **{field: value})
                with self.assertRaises(ValueError):
                    runner.request_identity(self.signed(), self.key, self.now)
        for value in (None, {}, dict(good, extra='not-authorized')):
            self.request['recovery'] = value
            with self.assertRaises(ValueError):
                runner.request_identity(self.signed(), self.key, self.now)

    def test_recovery_failed_receipt_tamper_or_unsafe_identity_never_changes_state(self):
        original = self.recovery_fixture()
        process = self.execution_patches()
        original_bytes = runner.gate.canonical(original)
        cases = [('schema_version', True), ('status', 'SUCCESS'), ('status', 'CLAIMED'), ('kind', 'promotion'),
                 ('product', 'other'), ('commit', 'f' * 40), ('version', '1.0.99'),
                 ('digest', 'sha256:' + 'f' * 64), ('request_sha256', 'short'),
                 ('previous_container_id', 'short'), ('previous_image_id', 'app:latest'),
                 ('new_backup_and_migration_files', ['/backup/evidence']),
                 ('new_backup_and_migration_files', None), ('automatic_rollback', True),
                 ('recovery', None), ('failed_receipt_sha256', 'a' * 64)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                bad = runner.control.sign(dict(original['payload'], **{field: value}), self.key)
                self.request['recovery']['failed_receipt'] = bad
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertEqual((self.root / 'state' / (self.commit + '.json')).read_bytes(), original_bytes)
                self.assertFalse((self.root / 'state/attempts').exists())
        tampered = copy.deepcopy(original)
        tampered['payload']['previous_container_id'] = 'f' * 64
        self.request['recovery']['failed_receipt'] = tampered
        with self.assertRaisesRegex(ValueError, 'signature'):
            self.execute()
        process.assert_not_called()

    def test_recovery_missing_changed_or_ambiguous_claim_never_mutates(self):
        failed = self.recovery_fixture()
        process = self.execution_patches()
        state = runner.control.State(self.root / 'state')
        state.path(self.commit).unlink()
        with self.assertRaisesRegex(ValueError, 'state missing'):
            self.execute()
        for status in ('CLAIMED', 'SUCCESS', 'FAILED'):
            with self.subTest(status=status):
                different = runner.control.sign(dict(failed['payload'], status=status, request_sha256='f' * 64), self.key)
                state.write(self.commit, different)
                before = state.path(self.commit).read_bytes()
                with self.assertRaisesRegex(ValueError, 'state changed'):
                    self.execute()
                self.assertEqual(state.path(self.commit).read_bytes(), before)
        self.assertFalse((self.root / 'state/attempts').exists())
        process.assert_not_called()

    def test_recovery_changed_unhealthy_or_already_active_runtime_never_mutates(self):
        failed = self.recovery_fixture()
        original = copy.deepcopy(self.container)
        process = self.execution_patches()
        for field, value in [('Id', '4' * 64), ('Image', 'sha256:' + '4' * 64),
                             ('State', {'Running': True, 'Health': {'Status': 'starting'}}),
                             ('State', {'Running': False, 'Health': {'Status': 'healthy'}})]:
            with self.subTest(field=field, value=value):
                self.container.clear()
                self.container.update(copy.deepcopy(original))
                self.container[field] = value
                with self.assertRaises(ValueError):
                    self.execute()
        self.container.clear()
        self.container.update(original)
        with patch.object(runner, 'image_identity', return_value=self.container['Image']):
            with self.assertRaisesRegex(ValueError, 'already active'):
                self.execute()
        self.assertEqual(runner.control.State(self.root / 'state').read(self.commit), failed)
        self.assertFalse((self.root / 'state/attempts').exists())
        process.assert_not_called()

    def test_recovery_advanced_develop_or_prod_never_mutates(self):
        failed = self.recovery_fixture()
        process = self.execution_patches()
        for heads in (('f' * 40, self.commit), ('a' * 40, 'f' * 40)):
            with patch.object(runner.control, 'heads', return_value=heads), self.assertRaisesRegex(ValueError, 'advanced'):
                self.execute()
        self.assertEqual(runner.control.State(self.root / 'state').read(self.commit), failed)
        self.assertFalse((self.root / 'state/attempts').exists())
        process.assert_not_called()

    def test_recovery_archive_conflict_even_identical_or_symlink_never_overwrites(self):
        failed = self.recovery_fixture()
        process = self.execution_patches()
        archive = self.archive_path(failed)
        archive.parent.mkdir(mode=0o700)
        for data in (b'conflicting previous artifact', runner.gate.canonical(failed)):
            archive.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'archive already exists'):
                self.execute()
            self.assertEqual(archive.read_bytes(), data)
            self.assertEqual(runner.control.State(self.root / 'state').read(self.commit), failed)
        archive.unlink()
        target = self.root / 'immutable-target'
        target.write_bytes(b'untouched')
        archive.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'archive already exists'):
            self.execute()
        self.assertEqual(target.read_bytes(), b'untouched')
        process.assert_not_called()

    def test_recovery_archive_fsync_failure_never_replaces_failed_claim(self):
        failed = self.recovery_fixture()
        process = self.execution_patches()
        with patch.object(runner.os, 'fsync', side_effect=OSError('synthetic sync failure')):
            with self.assertRaises(OSError):
                self.execute()
        self.assertEqual(runner.control.State(self.root / 'state').read(self.commit), failed)
        self.assertEqual(self.archive_path(failed).read_bytes(), runner.gate.canonical(failed))
        process.assert_not_called()

    def test_recovery_expired_while_waiting_for_owner_checks_never_claims(self):
        failed = self.recovery_fixture()
        process = self.execution_patches()
        late = self.now + dt.timedelta(minutes=15)
        class LateClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return late
        with patch.object(runner.dt, 'datetime', LateClock), self.assertRaisesRegex(ValueError, 'expired'):
            self.execute()
        self.assertEqual(runner.control.State(self.root / 'state').read(self.commit), failed)
        self.assertFalse((self.root / 'state/attempts').exists())
        process.assert_not_called()

    def test_recovery_untrusted_archive_directory_never_follows_or_claims(self):
        failed = self.recovery_fixture()
        process = self.execution_patches()
        directory = self.root / 'state/attempts'
        external = self.root / 'outside'
        external.mkdir(mode=0o700)
        directory.symlink_to(external, target_is_directory=True)
        with self.assertRaises(OSError):
            self.execute()
        self.assertEqual(list(external.iterdir()), [])
        directory.unlink()
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'not private'):
            self.execute()
        self.assertEqual(runner.control.State(self.root / 'state').read(self.commit), failed)
        self.assertEqual(list(directory.iterdir()), [])
        process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
