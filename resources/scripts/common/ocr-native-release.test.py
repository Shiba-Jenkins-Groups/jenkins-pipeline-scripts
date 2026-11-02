#!/usr/bin/env python3
"""Offline source/CI/archive boundary tests; no model, Jenkins or runtime calls."""
import argparse
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_release', Path(__file__).with_name('ocr-native-release.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class NativeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.parent = {'schema': 'recognition-parent-ci-v1', 'status': 'SUCCESS', 'source_commit': 'a' * 40,
                       'image': 'registry/recognition@sha256:' + 'b' * 64, 'job_name': m.PARENT_JOB, 'build_number': 46,
                       'build_url': m.ORIGIN + '/' + m.build_path(m.PARENT_JOB, 46),
                       'artifact_hash_inventory': {name: 'c' * 64 for name in m.PARENT_FILES}}
        self.identity = {'schema_version': 1, 'job_name': m.JOB, 'build_number': '1',
                         'build_url': m.ORIGIN + '/' + m.build_path(m.JOB, 1), 'source_commit': 'd' * 40, 'source_baseline_commit': '8' * 40,
                         'owner_source_commit': 'e' * 40, 'owner_baseline_commit': '9' * 40,
                         'library_revision': 'f' * 40, 'requested_by': 'claude-code-use',
                         'owner_package': './cmd/service-control-owner', 'owner_test_run': '^TestOCRReplacement',
                         'go_version': 'go1.27.2', 'parent_ci': self.parent,
                         'scanner_tools': {'gitleaks': {'module': 'github.com/zricethezav/gitleaks/v8', 'version': 'v8.30.1'},
                                           'govulncheck': {'path': '/private/tool', 'sha256': '1' * 64}}}
        native_bytes, owner_bytes, manifest_bytes = b'native binary', b'owner binary', b'manifest binary'
        self.native = {'revision': 'd' * 40, 'lane': 'prod', 'binary': 'shiba_ocr_prod_' + m.sha(native_bytes)[:12],
                       'binary_sha256': m.sha(native_bytes), 'runtime_identity': {'scheme': 'apple-vision-artifact-v2'},
                       'runtime_artifact_sha256': '2' * 64}
        self.smoke = {'ready': True, 'contract_version': 1, 'provider_id': 'apple-vision', 'platform_lane': 'mac',
                      'runtime_artifact_sha256': '2' * 64, 'runtime_identity': self.native['runtime_identity'],
                      'compatibility': {'status': 'pass', 'policy': 'receipt-smoke-v1'},
                      'cache_identity': {'verified': True, 'lane': 'prod', 'namespace': self.native['binary'],
                                         'binary_sha256': self.native['binary_sha256']}, 'initialization': {'phase': 'ready'}}
        owner = {'schema': 'service-control-owner-artifact-v1', 'source_commit': 'e' * 40,
                 'binary': 'service-control-owner', 'platform': 'darwin/arm64', 'binary_sha256': m.sha(owner_bytes)}
        manifest = {'source_commit': 'd' * 40, 'platform': 'darwin/arm64', 'binary_sha256': m.sha(manifest_bytes)}
        encode = lambda value: (json.dumps(value) + '\n').encode()
        self.entries = {'native/artifact.json': encode(self.native), 'native/parent-ci.json': encode(self.parent),
                        'native/cache-info.json': b'{}\n', 'native/' + self.native['binary']: native_bytes,
                        'owner/artifact.json': encode(owner), 'owner/service-control-owner': owner_bytes,
                        'manifest-tool/artifact.json': encode(manifest), 'manifest-tool/ocr-release-manifest': manifest_bytes,
                        'reports/startup-smoke.json': encode(self.smoke), 'reports/ocr-gitleaks.json': b'[]\n',
                        'reports/owner-gitleaks.json': b'[]\n',
                        'reports/scan-summary.json': encode({'status': 'PASS', 'gitleaks': {'status': 'PASS'}, 'govulncheck': {'status': 'PASS'}})}

    def tearDown(self):
        self.temporary.cleanup()

    def artifact(self):
        archive = self.directory / m.FILES[0]
        with tarfile.open(archive, 'w') as tar:
            for name, data in self.entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        log = b'contracts passed\n'
        (self.directory / 'contracts.log').write_bytes(log)
        receipt = {'schema': 'apple-ocr-native-ci-v1', 'result': 'PASS', 'identity': self.identity,
                   'archive_sha256': m.sha(archive.read_bytes()), 'archive_size': archive.stat().st_size,
                   'contracts_log_sha256': m.sha(log), 'native_artifact_sha256': m.sha(self.entries['native/artifact.json']),
                   'owner_artifact_sha256': m.sha(self.entries['owner/artifact.json']), 'runtime_deployed': False,
                   'pipeline_final_success': False,
                   'files': {name: {'sha256': m.sha(data), 'size': len(data)} for name, data in self.entries.items()}}
        (self.directory / 'ci-receipt.json').write_text(json.dumps(receipt))
        (self.directory / 'SHA256SUMS').write_text(''.join(m.sha((self.directory / name).read_bytes()) + '  ' + name + '\n'
                                                        for name in m.FILES if name != 'SHA256SUMS'))
        return receipt

    def api(self):
        actions = [{'causes': [{'_class': 'hudson.model.Cause$UserIdCause', 'userId': 'claude-code-use'}]}]
        for repo, commit in [(m.OCR_REPO, 'd' * 40), (m.OWNER_REPO, 'e' * 40), (m.LIB_REPO, 'f' * 40)]:
            actions.append({'remoteUrls': [repo], 'lastBuiltRevision': {'SHA1': commit}})
        actions.append({'libraries': [{'name': 'jenkins-pipeline', 'version': 'f' * 40, 'trusted': True}]})
        return {'number': 1, 'result': 'SUCCESS', 'building': False, 'actions': actions,
                'artifacts': [{'relativePath': 'artifact/' + name} for name in m.FILES]}

    def test_independent_parent_commit_is_preserved(self):
        self.assertNotEqual(self.parent['source_commit'], self.identity['source_commit'])
        self.assertEqual(m.validate_identity(self.identity), self.identity)
        broken = copy.deepcopy(self.parent)
        broken['build_url'] = 'https://untrusted.example/job/46/'
        with self.assertRaisesRegex(ValueError, 'origin'):
            m.validate_parent(broken)

    def test_merge_source_scan_includes_all_changes_since_reviewed_baseline(self):
        repository = self.directory / 'git-source'
        repository.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=repository, env=env, stderr=subprocess.DEVNULL).decode().strip()
        git('init', '-b', 'baseline')
        (repository / 'base').write_text('existing\n')
        git('add', '.')
        git('commit', '-m', 'base')
        baseline = git('rev-parse', 'HEAD')
        git('checkout', '-b', 'changes')
        (repository / 'native.swift').write_text('SCOPED_NATIVE_CHANGE\n')
        git('add', '.')
        git('commit', '-m', 'scoped source')
        git('checkout', 'baseline')
        git('merge', '--no-ff', '-m', 'production merge', 'changes')
        self.assertEqual(git('show', '--format=', 'HEAD'), '')
        self.assertIn(b'SCOPED_NATIVE_CHANGE', m.changed_patch(repository, baseline))
        with self.assertRaises(ValueError):
            m.changed_patch(repository, '0' * 40)

    def test_integrity_is_not_final_ci_success(self):
        self.artifact()
        receipt = m.verify(self.directory, 'd' * 40, 'e' * 40, 'f' * 40)
        self.assertFalse(receipt['pipeline_final_success'])
        with self.assertRaisesRegex(ValueError, 'owner_source_commit'):
            m.verify(self.directory, 'd' * 40, '9' * 40, 'f' * 40)

    def test_binary_tamper_and_unsafe_members_fail(self):
        self.entries['native/' + self.native['binary']] = b'different binary'
        self.artifact()
        with self.assertRaisesRegex(ValueError, 'native component'):
            m.verify(self.directory)
        self.entries['../escape'] = b'bad'
        self.artifact()
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            m.verify(self.directory)

    def test_prior_smoke_cannot_attest_new_binary(self):
        bad = copy.deepcopy(self.smoke)
        bad['cache_identity']['namespace'] = 'shiba_ocr_dev_' + '0' * 12
        with self.assertRaisesRegex(ValueError, 'cache preparation'):
            m.validate_smoke(bad, self.native)
        bad = copy.deepcopy(self.smoke)
        bad['runtime_artifact_sha256'] = '3' * 64
        with self.assertRaisesRegex(ValueError, 'compatibility identity'):
            m.validate_smoke(bad, self.native)

    def test_missing_library_checkout_or_replay_cause_fails(self):
        m.validate_build_api(self.api(), self.identity)
        for mutation in ['library', 'replay', 'source', 'unstable', 'extra-artifact']:
            api = self.api()
            if mutation == 'library':
                api['actions'][-1]['libraries'][0]['trusted'] = False
            elif mutation == 'replay':
                api['actions'][0]['causes'].append({'_class': 'org.jenkinsci.plugins.workflow.cps.replay.ReplayCause'})
            elif mutation == 'source':
                api['actions'][1]['lastBuiltRevision']['SHA1'] = '0' * 40
            elif mutation == 'unstable':
                api['result'] = 'UNSTABLE'
            else:
                api['artifacts'].append({'relativePath': 'unapproved'})
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                m.validate_build_api(api, self.identity)

    def test_renderer_disabled_by_default_and_pins_library(self):
        path = self.directory / 'identity.json'
        path.write_text(json.dumps(self.identity))
        rendered = m.render(argparse.Namespace(identity=path, enable=False))
        self.assertIn('<disabled>true</disabled>', rendered)
        self.assertIn('jenkins-pipeline@' + 'f' * 40, rendered)
        self.assertNotIn('main', rendered)
        self.assertIn("ocrNativeReleasePipeline('", rendered)
        self.assertNotIn('JsonSlurper', rendered)
        self.assertNotIn('new String', rendered)
        self.assertNotIn('decodeBase64', rendered)

    def test_deployment_renderer_has_only_reviewed_fixed_plan_inputs(self):
        config = {'libraryRevision': 'f' * 40, 'sourceCommit': 'd' * 40, 'approvers': ['claude-code-use'],
                  'plan': '/Users/surpend/private/plan.json', 'planSHA256': '1' * 64,
                  'ciReceipt': '/Users/surpend/private/trusted-build-evidence.json', 'ciReceiptSHA256': '2' * 64,
                  'artifactDirectory': '/Users/surpend/private/review/ci-artifacts'}
        path = self.directory / 'deploy-config.json'
        path.write_text(json.dumps(config))
        args = argparse.Namespace(config=path, enable=False)
        rendered = m.render_deployment(args)
        self.assertIn('<disabled>true</disabled>', rendered)
        self.assertIn("ocrNativeDeploymentPipeline('", rendered)
        self.assertNotIn('JsonSlurper', rendered)
        self.assertNotIn('new String', rendered)
        self.assertNotIn('decodeBase64', rendered)
        self.assertIn('jenkins-pipeline@' + 'f' * 40, rendered)
        config['command'] = 'arbitrary'
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'exact reviewed'):
            m.render_deployment(args)

    def test_collector_cannot_accept_running_build(self):
        args = argparse.Namespace(build='1', source_commit='d' * 40, owner_source_commit='e' * 40,
                                  library_revision='f' * 40, requested_by='claude-code-use', output=self.directory / 'result')
        with patch.object(m, 'authenticated_get', return_value=json.dumps({'result': None, 'building': True}).encode()):
            with self.assertRaisesRegex(ValueError, 'not successful'):
                m.collect(args)
        self.assertFalse(Path(args.output).exists())

    def test_successful_collector_binds_parent_and_all_components(self):
        runtime = {'commit': self.parent['source_commit'], 'image': self.parent['image'],
                   'health': 'PASS', 'shutdown': 'PASS', 'cleanup': 'PASS'}
        parent_files = {'.pipeline/release-manifest.env': b'release receipt\n', 'image-ref.txt': b'image receipt\n',
                        'reports/runtime-image.json': json.dumps(runtime).encode()}
        self.parent['artifact_hash_inventory'] = {name: m.sha(raw) for name, raw in parent_files.items()}
        self.entries['native/parent-ci.json'] = json.dumps(self.parent).encode()
        self.artifact()
        data = {name: (self.directory / name).read_bytes() for name in m.FILES}
        parent_api = {'number': 46, 'result': 'SUCCESS', 'building': False,
                      'actions': [{'remoteUrls': [m.OCR_REPO], 'lastBuiltRevision': {'SHA1': self.parent['source_commit']}}]}
        workflow = {'status': 'SUCCESS', 'stages': [{'name': name, 'status': 'SUCCESS'} for name in m.STAGES]}
        lookup = {m.api_path(m.JOB, '1'): json.dumps(self.api()).encode(),
                  m.api_path(m.PARENT_JOB, 46): json.dumps(parent_api).encode(),
                  m.build_path(m.JOB, '1') + 'wfapi/describe': json.dumps(workflow).encode()}
        lookup.update({m.build_path(m.JOB, '1') + 'artifact/artifact/' + name: raw for name, raw in data.items()})
        lookup.update({m.build_path(m.PARENT_JOB, 46) + 'artifact/' + name: raw for name, raw in parent_files.items()})
        args = argparse.Namespace(build='1', source_commit='d' * 40, owner_source_commit='e' * 40,
                                  library_revision='f' * 40, requested_by='claude-code-use', output=self.directory / 'result')
        with patch.object(m, 'authenticated_get', side_effect=lambda path: lookup[path]):
            evidence = m.collect(args)
        self.assertEqual(evidence['schema'], 'ocr-native-ci-v1')
        self.assertEqual(evidence['status'], 'SUCCESS')
        self.assertTrue(evidence['pipeline_final_success'])
        self.assertEqual(evidence['artifact_hash_inventory']['reports/startup-smoke.json'], m.sha(self.entries['reports/startup-smoke.json']))
        self.assertEqual(evidence['parent_ci']['source_commit'], 'a' * 40)
        self.assertNotEqual(evidence['source_commit'], evidence['parent_ci']['source_commit'])
        self.assertFalse(evidence['runtime_deployed'])
        self.assertTrue((Path(args.output) / 'trusted-build-evidence.json').is_file())

        # A self-consistent native archive cannot hide changed parent CI bytes.
        lookup[m.build_path(m.PARENT_JOB, 46) + 'artifact/image-ref.txt'] = b'changed parent bytes'
        args.output = self.directory / 'rejected'
        with patch.object(m, 'authenticated_get', side_effect=lambda path: lookup[path]):
            with self.assertRaisesRegex(ValueError, 'parent artifact hash'):
                m.collect(args)
        self.assertFalse(Path(args.output).exists())


if __name__ == '__main__':
    unittest.main()
