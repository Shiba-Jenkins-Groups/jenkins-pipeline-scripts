#!/usr/bin/env python3
"""Offline candidate/revalidation/finalization contracts; no external writes."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent
def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value

fixtures = load('candidate_fixtures', 'release-gate.test.py')
candidate = load('candidate_controls', 'release-candidate.py')
adapter = candidate.adapter
control = load('candidate_promotion', 'release-promotion.py')
finalizer = load('candidate_finalization', 'release-finalization.py')
gate = candidate.gate


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReleaseGateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.evidence, self.policy = self.fixture.root, self.fixture.evidence, self.fixture.policy
        self.now, self.key = self.fixture.now, self.fixture.key
        self.policy.update(candidate_mode=True, waivable_stages=sorted(gate.WAIVABLE_STAGES))
        self.policy['required_stages'] = {k: adapter.STAGES for k in ['promotion', 'deployment']}
        self.policy['jobs'] = {'promotion': gate.PRODUCT + '/develop', 'deployment': gate.PRODUCT + '/prod'}
        self.evidence.update(mode='controlled-candidate-v1', job=gate.PRODUCT + '/develop', version='1.0.32',
                             stages=[{'name': name, 'result': 'SUCCESS'} for name in adapter.STAGES], stage_checks=[])
        self.evidence['artifact'] = self.raw('artifact', b'offline binary bytes')
        self.evidence['artifact_name'] = gate.PRODUCT + '-prod-1.0.32'
        self.records = {}
        for name in candidate.STAGES:
            slug = candidate.STAGES[name]
            native = {'Dependency Scan': '{"config":{}}\n',
                      'Image Scan': '{"SchemaVersion":2,"Results":[{"Vulnerabilities":[]}]}'}
            reports = [self.raw(slug + '.log', native.get(name, 'offline successful stage output').encode())]
            if name == 'Test':
                reports.append(self.raw('junit.xml', b'<testsuite><testcase name="test"/></testsuite>'))
            record = {'schema_version': 1, 'stage': name, 'commit': self.evidence['commit'], 'job': self.evidence['job'],
                      'build': self.evidence['build'], 'complete': True, 'exit_code': 0, 'finding_count': 0, 'outcome': 'PASS', 'reports': reports}
            self.records[name] = record
        self.write_checks()

    def raw(self, path, raw):
        (self.root / path).write_bytes(raw)
        return {'path': path, 'sha256': hashlib.sha256(raw).hexdigest()}

    def write_checks(self):
        self.evidence['stage_checks'] = [self.raw('stage-' + candidate.STAGES[name] + '.json', gate.canonical(record)) for name, record in self.records.items()]

    def failed_test(self):
        self.evidence['result'] = 'UNSTABLE'
        next(s for s in self.evidence['stages'] if s['name'] == 'Test')['result'] = 'UNSTABLE'
        self.records['Test'].update(exit_code=1, finding_count=1, outcome='WAIVER_REQUIRED')
        self.records['Test']['reports'][1] = self.raw('junit.xml', b'<testsuite><testcase name="failed"><failure message="offline"/></testcase></testsuite>')
        self.write_checks()

    def evaluate(self, **kwargs):
        return gate.evaluate(self.evidence, self.policy, self.root, self.now, **kwargs)

    def approve(self):
        return control.approve(self.evidence, self.policy, self.root,
            {'id': 'offline-stage-waiver', 'approver': 'test-approver', 'reason': 'Accept exactly these test assertions'}, self.key, self.now)

    def test_clean_candidate_passes_without_finalization(self):
        self.assertEqual(self.evaluate()['decision'], 'PASS')

    def test_failed_test_requires_exact_signed_approval_and_preserves_unstable(self):
        self.failed_test()
        with self.assertRaises(ValueError): self.evaluate()
        review = control.review(self.evidence, self.policy, self.root, self.now)
        self.assertEqual(review['decision'], 'NEEDS_APPROVAL')
        self.assertEqual(next(iter(review['findings'].values()))['id'], 'Test')
        self.assertEqual(self.evaluate(approval=self.approve(), approval_key=self.key)['decision'], 'APPROVED_EXCEPTION')
        self.assertEqual(self.evidence['result'], 'UNSTABLE')

    def test_whitelist_cannot_add_build_secret_scan_or_runtime(self):
        for name in ['Build', 'Secret Scan', 'Deployment Verification — k3s', 'Declarative: Post Actions']:
            self.policy['waivable_stages'] = sorted(gate.WAIVABLE_STAGES | {name})
            with self.subTest(name=name), self.assertRaises(ValueError): self.evaluate()

    def test_mandatory_stage_failure_blocks_even_with_test_approval(self):
        self.failed_test()
        approval = self.approve()
        for name in ['Build', 'Secret Scan', 'Docker Build', 'Harbor Push', 'Smoke Test', 'Deployment Verification — k3s', 'Declarative: Post Actions']:
            stage = next(s for s in self.evidence['stages'] if s['name'] == name)
            stage['result'] = 'UNSTABLE'
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.evaluate(approval=approval, approval_key=self.key)
            stage['result'] = 'SUCCESS'

    def test_missing_tampered_aborted_or_unexplained_stage_blocks(self):
        for change in ['missing', 'tamper', 'abort', 'unknown']:
            evidence = copy.deepcopy(self.evidence)
            if change == 'missing': evidence['stage_checks'].pop()
            elif change == 'tamper': evidence['stage_checks'][0]['sha256'] = '0' * 64
            elif change == 'abort': evidence['result'] = 'ABORTED'
            else: evidence['result'] = 'UNSTABLE'
            with self.subTest(change=change), self.assertRaises(ValueError):
                gate.evaluate(evidence, self.policy, self.root, self.now)

    def test_scanner_errors_cannot_be_waived(self):
        for name in ['Dependency Scan', 'Image Scan', 'Harbor Vulnerability Report']:
            self.records[name]['exit_code'] = 1
            self.write_checks()
            with self.subTest(name=name), self.assertRaises(ValueError): self.evaluate()
            self.records[name]['exit_code'] = 0

    def test_go_vet_missing_junit_and_test_errors_cannot_be_waived(self):
        for raw in [b'<testsuite/>', b'<testsuite><testcase><error/></testcase></testsuite>',
                    b'<testsuite><testcase><skipped/></testcase></testsuite>']:
            self.records['Test']['reports'][1] = self.raw('junit.xml', raw)
            self.write_checks()
            with self.subTest(raw=raw), self.assertRaises(ValueError): self.evaluate()

    def test_changed_native_assertions_invalidate_stage_approval(self):
        self.failed_test()
        approval = self.approve()
        self.records['Test']['reports'][1] = self.raw('junit.xml', b'<testsuite><testcase name="DIFFERENT"><failure/></testcase></testsuite>')
        self.write_checks()
        with self.assertRaises(ValueError): self.evaluate(approval=approval, approval_key=self.key)

    def test_stage_waiver_does_not_cover_new_cve(self):
        self.failed_test()
        approval = self.approve()
        self.fixture.vulnerable('LOW')
        with self.assertRaises(ValueError): self.evaluate(approval=approval, approval_key=self.key)
        self.assertEqual(len(control.review(self.evidence, self.policy, self.root, self.now)['findings']), 2)

    def test_scanner_stage_findings_are_not_duplicate_approval_items(self):
        self.evidence['result'] = 'UNSTABLE'
        next(s for s in self.evidence['stages'] if s['name'] == 'Image Scan')['result'] = 'UNSTABLE'
        self.records['Image Scan'].update(finding_count=1, outcome='WAIVER_REQUIRED')
        self.records['Image Scan']['reports'][0] = self.raw('image.log', gate.canonical({
            'SchemaVersion': 2, 'Results': [{'Vulnerabilities': [{
                'VulnerabilityID': 'CVE-TEST-IMAGE', 'PkgName': 'example',
                'InstalledVersion': '1', 'Severity': 'LOW'}]}]}))
        self.write_checks()
        self.fixture.vulnerable('LOW')
        review = control.review(self.evidence, self.policy, self.root, self.now)
        self.assertEqual(review['decision'], 'NEEDS_APPROVAL')
        self.assertEqual(len(review['findings']), 1)
        self.assertEqual(next(iter(review['findings'].values()))['scanner'], 'trivy')

    def test_collector_rejects_previously_finalized_candidate(self):
        built = {'number': 1, 'building': False, 'result': 'SUCCESS', 'timestamp': 1788868800000, 'duration': 0,
                 'actions': [{'lastBuiltRevision': {'SHA1': self.evidence['commit'], 'branch': [{'name': 'prod'}]}}]}
        workflow = {'id': '1', 'status': 'SUCCESS', 'stages': [{'name': n, 'status': 'SUCCESS'} for n in adapter.STAGES + [adapter.FINALIZE]]}
        image = f"APP_NAME={gate.PRODUCT}\nBRANCH=prod\nBUILD_NUMBER=1\nAPP_VERSION=1.0.32\nIMAGE_REF=localhost:9290/{gate.PRODUCT}/prod/1.0.32:1\nIMAGE_DIGEST={self.fixture.digest}\n"
        with self.assertRaisesRegex(ValueError, 'finalized before'):
            adapter.completed_build(built, workflow, image, 'prod', 1, candidate={'mode': 'controlled-candidate-v1'})

    def test_run_stage_cli_preserves_failed_test_and_returns_review_code(self):
        (self.root / 'reports/junit').mkdir(parents=True)
        (self.root / 'reports/junit/go-tests.xml').write_text('<testsuite><testcase><failure/></testcase></testsuite>')
        env = dict(os.environ, GIT_COMMIT=self.evidence['commit'], JOB_NAME=self.evidence['job'], BUILD_NUMBER='1', PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, str(ROOT / 'release-candidate.py'), '--stage', 'Test', 'run', '--',
                                 sys.executable, '-c', 'print("offline assertion failed"); raise SystemExit(1)'], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 10, result.stderr)
        self.assertEqual(json.loads((self.root / '.pipeline/candidate-stages/test.json').read_text())['outcome'], 'WAIVER_REQUIRED')

    def prod(self):
        self.evidence.update(gate='deployment', branch='prod', job=gate.PRODUCT + '/prod')
        self.evidence['image_ref'] = f'localhost:9290/{gate.PRODUCT}/prod/1.0.32:1'
        self.evidence['immutable_image'] = f'localhost:9290/{gate.PRODUCT}/prod/1.0.32@{self.fixture.digest}'
        for record in self.records.values(): record['job'] = self.evidence['job']
        self.write_checks()

    def finalize(self, rc=0, wrong_hash=False, blocked=False):
        source = self.root / 'source'
        source.mkdir(exist_ok=True)
        state_root = self.root / 'state'
        state_root.mkdir(exist_ok=True)
        url = f"http://nexus.invalid/repository/raw-artifacts/{gate.PRODUCT}/prod/1.0.32-1-{self.evidence['commit'][:7]}/{self.evidence['artifact_name']}"
        manifest = {'RELEASE_TAG': 'v1.0.32', 'GIT_COMMIT': self.evidence['commit'], 'IMAGE_REF': self.evidence['image_ref'],
                    'IMAGE_DIGEST': self.fixture.digest, 'NEXUS_ARTIFACT_URL': url}
        tag_reads = []
        def git(path, *args):
            if args[0] == 'remote': return control.REMOTE
            if args[0] == 'rev-parse': return self.evidence['commit']
            if args[0] == 'ls-remote':
                tag_reads.append(1)
                return '' if len(tag_reads) == 1 else self.evidence['commit'] + '\trefs/tags/v1.0.32^{}'
            return ''
        def process(command, **kwargs):
            if command[0] == 'docker':
                image = {'RepoDigests': [self.evidence['immutable_image']], 'Config': {'Labels': {
                    'app.artifact.sha256': self.evidence['artifact']['sha256'], 'org.opencontainers.image.revision': self.evidence['commit'],
                    'app.name': gate.PRODUCT, 'app.version': '1.0.32', 'app.branch': 'prod'}}}
                return types.SimpleNamespace(returncode=0, stdout=json.dumps([image]))
            (source / '.pipeline/release-manifest.env').write_text(''.join(f'{k}={v}\n' for k, v in manifest.items()))
            return types.SimpleNamespace(returncode=rc)
        with patch.object(finalizer.control, 'git', side_effect=git), patch.object(finalizer.control, 'heads', return_value=('a' * 40, self.evidence['commit'])), \
             patch.object(finalizer.subprocess, 'run', side_effect=process) as run, patch.object(finalizer, 'published_hash', return_value='wrong' if wrong_hash else self.evidence['artifact']['sha256']), \
             patch.dict(os.environ, {'NEXUS_BASE_URL': 'http://nexus.invalid'}):
            if blocked:
                with self.assertRaises(ValueError):
                    finalizer.finalize(self.evidence, self.policy, self.root, source, control.State(state_root), self.key, self.now)
                run.assert_not_called()
                return
            return finalizer.finalize(self.evidence, self.policy, self.root, source, control.State(state_root), self.key, self.now)

    def test_finalization_never_starts_for_unapproved_test_failure(self):
        self.prod()
        self.failed_test()
        self.finalize(blocked=True)

    def test_finalization_requires_published_tag_and_binary_readback(self):
        self.prod()
        receipt = self.finalize()
        self.assertEqual(control.verify(receipt, self.key)['status'], 'SUCCESS')
        with self.assertRaisesRegex(ValueError, 'already claimed'): self.finalize()

    def test_failed_finalization_preserves_claim_and_never_allows_retry(self):
        self.prod()
        with self.assertRaises(ValueError): self.finalize(rc=1)
        state = json.loads((self.root / 'state' / (self.evidence['commit'] + '.json')).read_bytes())
        self.assertEqual(control.verify(state, self.key)['status'], 'FAILED')
        with self.assertRaisesRegex(ValueError, 'already claimed'): self.finalize()

    def test_wrong_published_artifact_hash_is_not_success(self):
        self.prod()
        with self.assertRaisesRegex(ValueError, 'readback mismatch'): self.finalize(wrong_hash=True)

    def test_handoff_requires_matching_finalization_and_stage_approval(self):
        self.prod()
        promotion = control.sign({'kind': 'promotion', 'status': 'MERGED', 'product': gate.PRODUCT,
            'merge_commit': self.evidence['commit'], 'version': '1.0.32'}, self.key)
        with self.assertRaisesRegex(ValueError, 'missing controlled finalization'):
            control.deploy_handoff(promotion, self.key, self.evidence, self.policy, self.root, self.now)
        finalized = self.finalize()
        request = control.deploy_handoff(promotion, self.key, self.evidence, self.policy, self.root, self.now, finalization=finalized)
        self.assertEqual(request['payload']['schema_version'], 2)
        self.assertEqual(request['payload']['finalization'], finalized)
        self.failed_test()
        with self.assertRaises(ValueError):
            control.deploy_handoff(promotion, self.key, self.evidence, self.policy, self.root, self.now, finalization=finalized)

    def test_candidate_artifact_tamper_blocks_promotion_gate(self):
        (self.root / 'artifact').write_bytes(b'different binary')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'): self.evaluate()

    def test_candidate_image_labels_bind_source_and_archived_binary(self):
        self.prod()
        labels = {'org.opencontainers.image.revision': self.evidence['commit'], 'app.artifact.sha256': self.evidence['artifact']['sha256'],
                  'app.version': self.evidence['version'], 'app.branch': 'prod', 'app.name': gate.PRODUCT}
        image = {'RepoDigests': [self.evidence['immutable_image']], 'Config': {'Labels': labels}}
        adapter.candidate_image(self.evidence, [image])
        for name in labels:
            value = copy.deepcopy(image)
            value['Config']['Labels'][name] = 'wrong'
            with self.subTest(name=name), self.assertRaises(ValueError): adapter.candidate_image(self.evidence, [value])

    def test_completed_candidate_downloads_only_hash_bound_artifacts(self):
        self.prod()
        built = {'number': 1, 'building': False, 'result': 'SUCCESS', 'timestamp': 1788868800000, 'duration': 0,
                 'actions': [{'lastBuiltRevision': {'SHA1': self.evidence['commit'], 'branch': [{'name': 'prod'}]}}]}
        workflow = {'id': '1', 'status': 'SUCCESS', 'stages': [{'name': n, 'status': 'SUCCESS'} for n in adapter.STAGES] +
                    [{'name': adapter.FINALIZE, 'status': 'NOT_EXECUTED'}, {'name': 'Develop Image Verification', 'status': 'NOT_EXECUTED'}]}
        image = f"APP_NAME={gate.PRODUCT}\nBRANCH=prod\nBUILD_NUMBER=1\nAPP_VERSION=1.0.32\nIMAGE_REF={self.evidence['image_ref']}\nIMAGE_DIGEST={self.fixture.digest}\n"
        manifest = dict(self.evidence, schema_version=1, product=gate.PRODUCT)
        def get(base, suffix):
            if suffix.endswith('api/json'): return gate.canonical(built)
            if suffix.endswith('wfapi/describe'): return gate.canonical(workflow)
            if suffix.endswith('artifact/image-ref.txt'): return image.encode()
            if suffix.endswith('artifact/.pipeline/candidate.json'): return gate.canonical(manifest)
            return (self.root / suffix.split('/artifact/', 1)[1]).read_bytes()
        download = self.root / 'download'
        with patch.object(adapter, 'jenkins_get', side_effect=get):
            result = adapter.inspect('http://offline', 'prod', 1, download)
        self.assertEqual(result['mode'], 'controlled-candidate-v1')
        self.assertEqual(gate.verified_bytes(download, result['artifact']), b'offline binary bytes')
        self.assertEqual(gate.stage_findings(result, self.policy, download), [])


if __name__ == '__main__':
    unittest.main()
