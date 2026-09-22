#!/usr/bin/env python3
"""Offline recovery bundle tests: no Jenkins writes, Git push, Docker or Nexus."""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import types
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('recovery_tests', Path(__file__).with_name('release-recovery.py'))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
gate, control = recovery.gate, recovery.control
deploy_spec = importlib.util.spec_from_file_location('recovery_deploy_contract', Path(__file__).with_name('release-deploy.py'))
deploy = importlib.util.module_from_spec(deploy_spec)
deploy_spec.loader.exec_module(deploy)


class RecoveryBundle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'bundle'
        self.source = self.base / 'source'
        self.source.mkdir()
        self.archive = {}
        self.key = b'offline-recovery-receipt-key-000000000000'
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.source_commit, self.merge, self.previous = 'a' * 40, 'b' * 40, 'c' * 40
        self.digest = 'sha256:' + 'd' * 64
        self.version = '1.0.52'
        self.source_build, self.prod_build = 243, 127
        self.coordinator_build, self.owner_build = 52, 19
        self.nexus = 'http://nexus.invalid'
        self.fixture_index = 0
        self.policy = {
            'schema_version': 1, 'product': gate.PRODUCT,
            'jobs': {'promotion': gate.PRODUCT + '/develop', 'deployment': gate.PRODUCT + '/prod'},
            'required_stages': {'promotion': recovery.adapter.LEAN_STAGES,
                                'deployment': ['Build', 'Scan', 'Reports']},
            'required_scanners': ['trivy', 'govulncheck', 'harbor'],
            'candidate_mode': True, 'develop_mode': 'lean-success-v1',
            'waivable_stages': sorted(gate.WAIVABLE_STAGES),
            'max_evidence_age_seconds': 3600, 'max_exception_seconds': 600,
            'approvers': ['test-approver'], 'revoked_approval_ids': [],
            'library_revision': 'e' * 40,
        }
        self.make_fixture()

    def save(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = value if isinstance(value, bytes) else gate.canonical(value)
        path.write_bytes(raw)
        return {'path': name, 'sha256': hashlib.sha256(raw).hexdigest()}

    def evidence(self, branch, age, scanner_age=None):
        commit = self.source_commit if branch == 'develop' else self.merge
        build = self.source_build if branch == 'develop' else self.prod_build
        root = self.base / f'original-{self.fixture_index}' / branch / 'evidence'
        root.mkdir(parents=True)
        completed = (self.now - dt.timedelta(minutes=age)).isoformat()
        if branch == 'develop':
            result = {'schema_version': 1, 'product': gate.PRODUCT,
                      'mode': 'lean-develop-success-v1', 'gate': 'promotion',
                      'branch': branch, 'event': 'branch', 'trusted': True,
                      'job': gate.PRODUCT + '/' + branch, 'build': build, 'commit': commit,
                      'version': self.version, 'building': False, 'post_complete': True,
                      'result': 'SUCCESS', 'completed_at': completed,
                      'stages': [{'name': name, 'result': 'SUCCESS'}
                                 for name in recovery.adapter.LEAN_STAGES],
                      'reports': []}
            self.save(root, 'evidence.json', result)
            return result, root
        scanner_completed = (self.now - dt.timedelta(minutes=scanner_age if scanner_age is not None else age)).isoformat()
        digest = self.digest
        artifact_name = f'{gate.PRODUCT}-{branch}-{self.version}'
        artifact = self.save(root, 'binary/app', b'offline-artifact-for-' + branch.encode())
        reports = []
        native = {
            'trivy': {'SchemaVersion': 2, 'Metadata': {'RepoDigests': ['registry/app@' + digest]},
                      'Results': [{'Target': 'app', 'Vulnerabilities': []}]},
            'govulncheck': {'messages': [{'config': {'scanner_version': 'offline'}}]},
            'harbor': {'artifact': {'digest': digest}, 'report': {'vulnerabilities': []}},
        }
        for scanner, payload in native.items():
            native_ref = self.save(root, f'reports/{scanner}-native.json', payload)
            envelope = {'schema_version': 1, 'scanner': scanner, 'complete': True,
                        'unfiltered': True, 'scanner_version': 'offline', 'database_revision': 'offline-db',
                        'commit': commit, 'image_digest': digest, 'severities': sorted(gate.SEVERITIES),
                        'completed_at': scanner_completed, 'native_report': native_ref}
            reports.append({'scanner': scanner, **self.save(root, f'reports/{scanner}.json', envelope)})
        stage_checks = []
        for stage in sorted(gate.CANDIDATE_STAGES):
            slug = stage.lower().replace(' ', '-').replace('—', '-')
            if stage == 'Test':
                body = [b'offline-test-log', b'<testsuite><testcase name="clean"/></testsuite>']
            elif stage == 'Dependency Scan':
                body = [b'{"config":{"scanner_version":"offline"}}\n']
            elif stage == 'Image Scan':
                body = [gate.canonical(native['trivy'])]
            else:
                body = [b'offline-clean-stage-log']
            refs = [self.save(root, f'stages/{slug}-{n}.txt', raw) for n, raw in enumerate(body)]
            check = {'schema_version': 1, 'stage': stage, 'complete': True,
                     'commit': commit, 'job': gate.PRODUCT + '/' + branch, 'build': build,
                     'exit_code': 0, 'reports': refs, 'finding_count': 0, 'outcome': 'PASS'}
            stage_checks.append(self.save(root, f'stages/{slug}.json', check))
        names = sorted(set(self.policy['required_stages']['deployment']) | gate.CANDIDATE_STAGES)
        image_ref = f'localhost:9290/{gate.PRODUCT}/{branch}/{self.version}:{build}'
        result = {'schema_version': 1, 'product': gate.PRODUCT,
                  'gate': 'promotion' if branch == 'develop' else 'deployment',
                  'branch': branch, 'event': 'branch', 'trusted': True,
                  'job': gate.PRODUCT + '/' + branch, 'build': build, 'commit': commit,
                  'image_ref': image_ref, 'image_digest': digest,
                  'immutable_image': image_ref.rsplit(':', 1)[0] + '@' + digest,
                  'version': self.version, 'building': False, 'post_complete': True,
                  'result': 'SUCCESS', 'completed_at': completed,
                  'stages': [{'name': name, 'result': 'SUCCESS'} for name in names],
                  'reports': reports, 'mode': 'controlled-candidate-v1',
                  'stage_checks': stage_checks, 'artifact': artifact, 'artifact_name': artifact_name}
        self.save(root, 'evidence.json', result)
        return result, root

    def make_fixture(self, develop_age=10, prod_age=10, scanner_age=None):
        self.fixture_index += 1
        self.evidences = {}
        self.origins = {}
        for branch, age in [('develop', develop_age), ('prod', prod_age)]:
            evidence, root = self.evidence(branch, age, scanner_age)
            self.evidences[branch], self.origins[branch] = evidence, root
            self.assertEqual(gate.evaluate(evidence, self.policy, root, self.now - dt.timedelta(minutes=5))['decision'], 'PASS')
        develop, prod = self.evidences['develop'], self.evidences['prod']
        self.promotion = control.sign({
            'schema_version': 1, 'kind': 'promotion', 'status': 'MERGED', 'product': gate.PRODUCT,
            'source_commit': self.source_commit, 'develop_build': self.source_build,
            'develop_job': gate.PRODUCT + '/develop', 'merge_commit': self.merge,
            'previous_prod_commit': self.previous, 'version': self.version,
            'evidence_sha256': recovery.sha(develop), 'policy_sha256': recovery.sha(self.policy),
        }, self.key)
        self.finalization = control.sign({
            'schema_version': 1, 'kind': 'finalization', 'status': 'SUCCESS', 'product': gate.PRODUCT,
            'commit': self.merge, 'version': self.version, 'digest': self.digest,
            'evidence_sha256': recovery.sha(prod), 'artifact_sha256': prod['artifact']['sha256'],
            'manifest': {'RELEASE_TAG': 'v' + self.version, 'GIT_COMMIT': self.merge,
                         'IMAGE_REF': prod['image_ref'], 'IMAGE_DIGEST': self.digest,
                         'NEXUS_ARTIFACT_URL': self.artifact_url(prod)},
        }, self.key)
        self.original = control.deploy_handoff(self.promotion, self.key, prod, self.policy,
                                               self.origins['prod'], self.now - dt.timedelta(minutes=5),
                                               finalization=self.finalization)
        request = control.verify(self.original, self.key)
        self.failure = control.sign({
            'schema_version': 1, 'kind': 'runtime-deployment', 'status': 'FAILED',
            'product': gate.PRODUCT, 'request_sha256': recovery.sha(self.original),
            'new_backup_and_migration_files': [], 'automatic_rollback': False,
            'commit': self.merge, 'version': self.version,
            'digest': self.digest, 'library_revision': request['library_revision'],
            'deployment_script_revision': request['deployment_script_revision'],
            'previous_container_id': 'f' * 64, 'previous_image_id': 'sha256:' + 'a' * 64,
            'started_at': (self.now - dt.timedelta(minutes=4)).isoformat(),
            'finished_at': (self.now - dt.timedelta(minutes=3)).isoformat(),
        }, self.key)
        self.archive.clear()
        cp, op = recovery.prefix(recovery.COORDINATOR, self.coordinator_build), recovery.prefix(recovery.OWNER, self.owner_build)
        self.archive.update({
            cp + 'api/json': gate.canonical(self.build(self.coordinator_build, gate.PRODUCT + '/develop', self.source_build)),
            op + 'api/json': gate.canonical(self.build(self.owner_build, recovery.COORDINATOR, self.coordinator_build)),
            recovery.prefix(gate.PRODUCT + '/prod', self.prod_build) + 'api/json':
                gate.canonical(self.build(self.prod_build, recovery.COORDINATOR, self.coordinator_build, result='SUCCESS')),
            cp + 'artifact/promotion.json': gate.canonical(self.promotion),
            cp + 'artifact/finalization.json': gate.canonical(self.finalization),
            cp + 'artifact/deployment-request.json': gate.canonical(self.original),
            cp + 'artifact/control/policy.json': gate.canonical(self.policy),
            op + 'artifact/runtime-receipt.json': gate.canonical(self.failure),
            op + 'artifact/request.json': gate.canonical(self.original),
        })
        for branch, root in self.origins.items():
            for path in root.rglob('*'):
                if path.is_file():
                    self.archive[cp + 'artifact/' + branch + '/evidence/' + str(path.relative_to(root))] = path.read_bytes()

    def artifact_url(self, evidence):
        return (f'{self.nexus}/repository/raw-artifacts/{gate.PRODUCT}/prod/'
                f'{self.version}-{self.prod_build}-{self.merge[:7]}/{evidence["artifact_name"]}')

    @staticmethod
    def build(number, upstream_job, upstream_build, result='FAILURE', cause_class=None):
        if cause_class is None:
            cause_class = ('hudson.model.Cause$UpstreamCause' if upstream_job == gate.PRODUCT + '/develop'
                           else 'org.jenkinsci.plugins.workflow.support.steps.build.BuildUpstreamCause')
        return {'number': number, 'building': False, 'result': result,
                'actions': [{'causes': [{'_class': cause_class, 'upstreamProject': upstream_job,
                                         'upstreamBuild': upstream_build}]}]}

    def collect(self):
        def get(_base, path):
            return self.archive[path]
        def inspect(_base, branch, number, candidate_root=None, lean=False):
            self.assertEqual(number, self.source_build if branch == 'develop' else self.prod_build)
            self.assertEqual(lean, branch == 'develop')
            self.assertEqual(candidate_root is None, branch == 'develop')
            # Native completed_build cannot have the coordinator's archived
            # normalized scanner reports; those are a separate signed archive.
            native = copy.deepcopy(self.evidences[branch])
            native['reports'] = []
            if lean:
                native.pop('version')
            return native
        with patch.object(recovery.adapter, 'jenkins_get', side_effect=get) as get_call, \
             patch.object(recovery.adapter, 'inspect', side_effect=inspect):
            value = recovery.collect('http://jenkins.invalid', self.coordinator_build, self.owner_build,
                                     self.source_build, self.source_commit, self.root, self.policy, self.key, self.now)
        self.assertTrue(all(call.args[1].startswith('/') for call in get_call.call_args_list))
        return value

    def operator_coordinator(self):
        return {'number': self.coordinator_build, 'building': False, 'result': 'FAILURE',
                'actions': [{'causes': [{'_class': 'hudson.model.Cause$UserIdCause',
                                         'userId': 'test-approver'}]},
                            {'parameters': [{'name': 'SOURCE_BUILD', 'value': str(self.source_build)},
                                            {'name': 'EXPECTED_COMMIT', 'value': self.source_commit}]}]}

    def test_collect_operator_retry_still_verifies_signed_bundle(self):
        cp = recovery.prefix(recovery.COORDINATOR, self.coordinator_build)
        self.archive[cp + 'api/json'] = gate.canonical(self.operator_coordinator())
        self.assertEqual(self.collect()['commit'], self.merge)
        forged = copy.deepcopy(self.promotion)
        forged['payload']['source_commit'] = '0' * 40
        self.archive[cp + 'artifact/promotion.json'] = gate.canonical(forged)
        self.root.rename(self.base / 'valid-operator-bundle')
        with self.assertRaisesRegex(ValueError, 'signature'):
            self.collect()

    def test_operator_retry_rejects_wrong_identity_or_source(self):
        for kind in ['operator', 'build', 'commit', 'missing', 'duplicate', 'unknown',
                     'recursive', 'rebuild', 'replay', 'mixed', 'numeric']:
            with self.subTest(kind=kind):
                build = self.operator_coordinator()
                cause, params = build['actions'][0]['causes'][0], build['actions'][1]['parameters']
                if kind == 'operator': cause['userId'] = 'not-an-approver'
                elif kind == 'build': params[0]['value'] = str(self.source_build + 1)
                elif kind == 'commit': params[1]['value'] = '0' * 40
                elif kind == 'missing': params.pop()
                elif kind == 'duplicate': params.append(dict(params[0]))
                elif kind == 'unknown': params.append({'name': 'BYPASS', 'value': ''})
                elif kind == 'recursive': params.append({'name': 'RECOVER_OWNER_BUILD', 'value': '1'})
                elif kind == 'rebuild': params.append({'name': 'REBUILD_PROD_COMMIT', 'value': self.merge})
                elif kind == 'replay': cause['_class'] = 'org.jenkinsci.plugins.workflow.cps.replay.ReplayCause'
                elif kind == 'mixed': build['actions'][0]['causes'].append(dict(cause))
                elif kind == 'numeric': params[0]['value'] = self.source_build
                with self.assertRaises(ValueError):
                    recovery.coordinator_source(build, self.source_build, self.source_commit, self.policy)

    def live(self, *, git_override=None, image_override=None, published=None, heads=None):
        evidence = self.evidences['prod']
        image = {'RepoDigests': [evidence['immutable_image']], 'Config': {'Labels': {
            'org.opencontainers.image.revision': self.merge,
            'app.artifact.sha256': evidence['artifact']['sha256'],
            'app.version': self.version, 'app.branch': 'prod', 'app.name': gate.PRODUCT}}}
        if image_override:
            image_override(image)
        def git(_source, *args):
            if git_override is not None:
                changed = git_override(args)
                if changed is not None:
                    return changed
            if args[:3] == ('remote', 'get-url', 'origin'): return control.REMOTE
            if args == ('rev-parse', 'HEAD'): return self.merge
            if args == ('status', '--porcelain'): return ''
            if args[:4] == ('rev-list', '--parents', '-n', '1'): return ' '.join([self.merge, self.previous, self.source_commit])
            if args[:3] == ('ls-remote', '--tags', 'origin'):
                tag = 'refs/tags/v' + self.version + '^{}'
                return self.merge + '\t' + tag
            raise AssertionError('unexpected Git operation: ' + str(args))
        def docker(command, **_kwargs):
            self.assertEqual(command[:3], ['docker', 'image', 'inspect'])
            return types.SimpleNamespace(returncode=0, stdout=json.dumps([image]))
        patches = [patch.object(recovery.control, 'git', side_effect=git),
                   patch.object(recovery.control, 'heads', return_value=heads or (self.source_commit, self.merge)),
                   patch.object(recovery.subprocess, 'run', side_effect=docker),
                   patch.object(recovery.finalizer, 'published_hash', return_value=published or evidence['artifact']['sha256']),
                   patch.dict(os.environ, {'NEXUS_BASE_URL': self.nexus})]
        for item in patches:
            item.start()
        return patches

    def test_complete_bundle_and_fresh_retry_without_new_build(self):
        identity = self.collect()
        self.assertEqual(identity['source_commit'], self.source_commit)
        self.assertEqual((self.root / 'original-request.json').read_bytes(), gate.canonical(self.original))
        for branch in ('develop', 'prod'):
            self.assertEqual((self.root / branch / 'evidence/evidence.json').read_bytes(),
                             (self.origins[branch] / 'evidence.json').read_bytes())
            self.assertEqual(len(list((self.root / branch / 'evidence').rglob('*'))),
                             len(list(self.origins[branch].rglob('*'))))
        for item in self.live():
            self.addCleanup(item.stop)
        signed_retry = recovery.handoff(self.root, self.source, self.policy, self.key, self.now, 'test-approver')
        retry = deploy.request_identity(signed_retry, self.key, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(retry['recovery']['kind'], 'failed-deployment-retry')
        self.assertEqual(retry['recovery']['coordinator_build'], self.coordinator_build)
        self.assertEqual(retry['recovery']['owner_build'], self.owner_build)
        self.assertEqual(retry['recovery']['failed_receipt'], self.failure)
        self.assertEqual(retry['prod_build'], self.prod_build)
        self.assertEqual((self.root / 'original-request.json').read_bytes(), gate.canonical(self.original))
        self.assertEqual((self.root / 'failed-receipt.json').read_bytes(), gate.canonical(self.failure))
        for branch in ('develop', 'prod'):
            for path in self.origins[branch].rglob('*'):
                if path.is_file():
                    archived = self.root / branch / 'evidence' / path.relative_to(self.origins[branch])
                    self.assertEqual(archived.read_bytes(), path.read_bytes())

    def test_collect_rejects_forged_or_wrong_failure_provenance(self):
        cp, op = recovery.prefix(recovery.COORDINATOR, self.coordinator_build), recovery.prefix(recovery.OWNER, self.owner_build)
        original = copy.deepcopy(self.archive)
        variants = [
            (cp + 'api/json', self.build(self.coordinator_build, 'wrong/develop', self.source_build), 'upstream chain mismatch'),
            (cp + 'api/json', self.build(self.coordinator_build, gate.PRODUCT + '/develop',
                                        self.source_build, cause_class='hudson.model.Cause$UserIdCause'), 'unauthorized original recovery operator'),
            (op + 'api/json', self.build(self.owner_build, recovery.COORDINATOR, self.coordinator_build + 1), 'upstream chain mismatch'),
            (op + 'api/json', self.build(self.owner_build, recovery.COORDINATOR, self.coordinator_build, result='SUCCESS'), 'completed failed coordinator and owner'),
            (recovery.prefix(gate.PRODUCT + '/prod', self.prod_build) + 'api/json',
             self.build(self.prod_build, 'wrong/coordinator', self.coordinator_build, result='SUCCESS'), 'upstream chain mismatch'),
        ]
        for path, value, expected_error in variants:
            with self.subTest(path=path, value=value):
                self.archive = dict(original, **{path: gate.canonical(value)})
                with self.assertRaisesRegex(ValueError, expected_error):
                    self.collect()
                if self.root.exists():
                    self.root.rename(self.base / ('failed-' + str(len(list(self.base.glob('failed-*'))))))
        self.archive = original

    def test_collect_rejects_tampered_signature_wrong_source_and_policy(self):
        cp = recovery.prefix(recovery.COORDINATOR, self.coordinator_build)
        forged = copy.deepcopy(self.promotion)
        forged['payload']['source_commit'] = '0' * 40
        self.archive[cp + 'artifact/promotion.json'] = gate.canonical(forged)
        with self.assertRaisesRegex(ValueError, 'invalid receipt signature'): self.collect()
        self.root.rename(self.base / 'failed-forged')
        self.archive[cp + 'artifact/promotion.json'] = gate.canonical(self.promotion)
        with patch.object(recovery.adapter, 'jenkins_get', side_effect=lambda _base, path: self.archive[path]), \
             patch.object(recovery.adapter, 'inspect', side_effect=lambda _base, branch, _number, _root=None, lean=False: self.evidences[branch]), \
             self.assertRaisesRegex(ValueError, 'original promotion binding mismatch'):
            recovery.collect('http://jenkins.invalid', self.coordinator_build, self.owner_build,
                             self.source_build, '0' * 40, self.root, self.policy, self.key, self.now)
        self.root.rename(self.base / 'failed-source')
        changed = dict(self.policy, max_evidence_age_seconds=7200)
        with patch.object(recovery.adapter, 'jenkins_get', side_effect=lambda _base, path: self.archive[path]), \
             patch.object(recovery.adapter, 'inspect', side_effect=lambda _base, branch, _number, _root=None, lean=False: self.evidences[branch]), \
             self.assertRaisesRegex(ValueError, 'recovery policy changed'):
            recovery.collect('http://jenkins.invalid', self.coordinator_build, self.owner_build,
                             self.source_build, self.source_commit, self.root, changed, self.key, self.now)

    def test_collect_rejects_missing_or_changed_nested_report(self):
        cp = recovery.prefix(recovery.COORDINATOR, self.coordinator_build)
        target = cp + 'artifact/prod/evidence/reports/trivy-native.json'
        original = self.archive[target]
        del self.archive[target]
        with self.assertRaisesRegex(KeyError, re.escape(target)): self.collect()
        self.root.rename(self.base / 'failed-missing')
        self.archive[target] = original + b'changed'
        with self.assertRaisesRegex(ValueError, 'archived report checksum mismatch'): self.collect()

    def test_collect_rejects_native_timestamp_or_stage_mismatch(self):
        for field in ('completed_at', 'stages'):
            with self.subTest(field=field):
                def inspect(_base, branch, _number, _root=None, lean=False):
                    native = copy.deepcopy(self.evidences[branch])
                    native['reports'] = []
                    if lean:
                        native.pop('version')
                    if branch == 'prod':
                        if field == 'completed_at':
                            native[field] = (self.now - dt.timedelta(minutes=9)).isoformat()
                        else:
                            native[field][0]['result'] = 'FAILURE'
                    return native
                with patch.object(recovery.adapter, 'jenkins_get', side_effect=lambda _base, path: self.archive[path]), \
                     patch.object(recovery.adapter, 'inspect', side_effect=inspect), \
                     self.assertRaisesRegex(ValueError, 'archived evidence differs from native build'):
                    recovery.collect('http://jenkins.invalid', self.coordinator_build, self.owner_build,
                                     self.source_build, self.source_commit, self.root, self.policy, self.key, self.now)
                self.root.rename(self.base / ('failed-native-' + field))

    def test_collect_rejects_signed_ineligible_failed_receipt_and_request_mismatch(self):
        cp, op = recovery.prefix(recovery.COORDINATOR, self.coordinator_build), recovery.prefix(recovery.OWNER, self.owner_build)
        original = copy.deepcopy(self.archive)
        for changed in ({'status': 'SUCCESS'}, {'new_backup_and_migration_files': ['backup.sqlite']},
                        {'automatic_rollback': True}, {'request_sha256': '0' * 64}):
            with self.subTest(changed=changed):
                payload = dict(control.verify(self.failure, self.key), **changed)
                self.archive = dict(original, **{op + 'artifact/runtime-receipt.json': gate.canonical(control.sign(payload, self.key))})
                with self.assertRaisesRegex(ValueError, 'failed receipt is not eligible'): self.collect()
                self.root.rename(self.base / ('failed-receipt-' + str(len(list(self.base.glob('failed-receipt-*'))))))
        self.archive = dict(original, **{op + 'artifact/request.json': gate.canonical({'different': True})})
        with self.assertRaisesRegex(ValueError, 'owner request differs from coordinator handoff'): self.collect()

    def test_collect_rechecks_both_original_evidence_at_actual_now(self):
        for branch in ('develop', 'prod'):
            with self.subTest(branch=branch):
                self.make_fixture(develop_age=61 if branch == 'develop' else 10,
                                  prod_age=61 if branch == 'prod' else 10)
                original_timestamp = self.evidences[branch]['completed_at']
                with self.assertRaisesRegex(ValueError, 'stale or future evidence'): self.collect()
                self.assertEqual(self.evidences[branch]['completed_at'], original_timestamp)
                if self.root.exists():
                    self.root.rename(self.base / ('failed-stale-' + branch))

    def test_handoff_rejects_unauthorized_changed_source_tag_image_and_hash(self):
        self.collect()
        with self.assertRaisesRegex(ValueError, 'unauthorized recovery approver'):
            recovery.handoff(self.root, self.source, self.policy, self.key, self.now, 'outsider')
        mutations = [
            ({'git_override': lambda args: '0' * 40 if args == ('rev-parse', 'HEAD') else None}, 'recovery checkout mismatch'),
            ({'git_override': lambda args: 'https://wrong.invalid/repo.git' if args[:3] == ('remote', 'get-url', 'origin') else None}, 'recovery checkout mismatch'),
            ({'git_override': lambda args: ' M file.go' if args == ('status', '--porcelain') else None}, 'recovery checkout mismatch'),
            ({'git_override': lambda args: '0' * 40 + '\trefs/tags/v1.0.52^{}' if args[:3] == ('ls-remote', '--tags', 'origin') else None}, 'finalized release tag changed'),
            ({'heads': (self.source_commit, '0' * 40)}, 'release branches advanced'),
            ({'image_override': lambda image: image['RepoDigests'].clear()}, 'candidate registry digest mismatch'),
            ({'published': '0' * 64}, 'published artifact bytes changed'),
        ]
        for index, (kwargs, expected_error) in enumerate(mutations):
            with self.subTest(index=index):
                patches = self.live(**kwargs)
                try:
                    with self.assertRaisesRegex(ValueError, expected_error):
                        recovery.handoff(self.root, self.source, self.policy, self.key, self.now, 'test-approver')
                finally:
                    for item in reversed(patches):
                        item.stop()

    def test_handoff_caps_fresh_request_to_original_evidence_and_scan_lifetimes(self):
        self.make_fixture(develop_age=10, prod_age=10, scanner_age=58)
        self.collect()
        for item in self.live():
            self.addCleanup(item.stop)
        retry = control.verify(recovery.handoff(self.root, self.source, self.policy, self.key, self.now, 'test-approver'), self.key)
        expires = gate.timestamp(retry['expires_at'])
        latest = min(gate.timestamp(gate.verified_report(self.origins[branch], report)['completed_at'])
                     + dt.timedelta(hours=1)
                     for branch in ('develop', 'prod') for report in self.evidences[branch]['reports'])
        self.assertLessEqual(expires, latest)
        self.assertGreater(expires, self.now)


if __name__ == '__main__':
    unittest.main()
