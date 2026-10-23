#!/usr/bin/env python3
"""Hermetic same-image revalidation: no CI, Docker, scanners, publish or DB."""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import patch


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


rv = module('same_image_revalidation', 'release-revalidate.py')
fixtures = module('same_image_fixture', 'release-recovery.test.py')
# Reuse only its fixture builder; do not inherit or re-run its existing suite.
fixtures.recovery, fixtures.control, fixtures.gate = rv.recovery, rv.control, rv.gate


class Revalidation(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RecoveryBundle(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.now = dt.datetime.now(dt.timezone.utc)
        self.f.now = self.now - dt.timedelta(hours=2)
        self.f.make_fixture()
        self.root = self.f.base / 'fresh-revalidation'
        self.run = {'job': rv.COORDINATOR, 'build': self.f.coordinator_build + 100}
        self.collected_at = self.now - dt.timedelta(minutes=5)

    def collect(self):
        def inspect(_base, branch, number, candidate_root=None, lean=False):
            self.assertEqual(number, self.f.source_build if branch == 'develop' else self.f.prod_build)
            self.assertEqual(lean, branch == 'develop')
            value = copy.deepcopy(self.f.evidences[branch])
            value['reports'] = []
            if lean: value.pop('version')
            return value
        with patch.object(rv.adapter, 'jenkins_get', side_effect=lambda _base, p: self.f.archive[p]), \
             patch.object(rv.adapter, 'inspect', side_effect=inspect):
            return rv.collect('http://jenkins.invalid', self.f.coordinator_build, self.f.owner_build,
                self.f.source_build, self.f.source_commit, self.root, self.f.policy, self.f.key,
                self.collected_at, self.run)

    def runtime(self):
        seed = rv.read(self.root / 'scan-identity.json')
        return {'schema_version': 1, 'product': rv.gate.PRODUCT, 'commit': self.f.merge,
                'image': seed['immutable_image'], 'profile': 'compose-v2', 'bootstrap_empty': True,
                'health': 'PASS', 'shutdown': 'PASS', 'cleanup': 'PASS', 'revalidation_run': {
                    'coordinator_job': self.run['job'], 'coordinator_build': self.run['build'],
                    'original_prod_build': self.f.prod_build, 'commit': self.f.merge,
                    'version': self.f.version, 'image': seed['immutable_image']}}

    def checks(self):
        for number, kind in enumerate(sorted(rv.CHECKS)):
            report = None
            if kind == 'test': report = self.f.source / 'junit.xml'
            if kind == 'runtime': report = self.root / 'runtime-image.json'
            def execute(command, **kwargs):
                kwargs['stdout'].write(b'executed complete trusted fixed check\n')
                if kind == 'test': report.write_bytes(b'<testsuite><testcase name="actual-test"/></testsuite>')
                if kind == 'runtime': report.write_bytes(rv.gate.canonical(self.runtime()))
                return types.SimpleNamespace(returncode=0)
            times = [self.collected_at + dt.timedelta(seconds=10 + number*2),
                     self.collected_at + dt.timedelta(seconds=11 + number*2)]
            with patch.object(rv, 'local_source'), patch.object(rv.subprocess, 'run', side_effect=execute):
                rv.check(self.root, self.f.source, kind, ['trusted-fixed-check', kind], report,
                         clock=lambda: times.pop(0))

    def scan(self):
        def fake_scan(seed, source, fresh, url):
            self.assertEqual(source, self.f.source)
            reports = []
            origin = self.f.origins['prod']
            for ref in self.f.evidences['prod']['reports']:
                envelope = rv.gate.verified_report(origin, ref)
                native = rv.gate.verified_bytes(origin, envelope['native_report'])
                envelope['native_report'] = self.f.save(fresh, ref['scanner'] + '-native.json', native)
                envelope['completed_at'] = self.now.isoformat()
                reports.append({'scanner': ref['scanner'], **self.f.save(fresh, ref['scanner']+'.json', envelope)})
            graph = {'schema_version': 1, 'complete': True, 'commit': self.f.merge, 'go_version': 'go1.26.10',
                     'graphs': [{**spec, 'goos': 'linux', 'goarch': 'arm64', 'cgo_enabled': '0',
                                 'packages': ['example.invalid/safe']} for spec in rv.adapter.PACKAGE_GRAPHS]}
            seed.update(reports=reports, package_graph=self.f.save(fresh, 'package-graphs.json', graph))
            self.f.save(fresh, 'evidence.json', seed)
            return seed
        with patch.object(rv, 'local_source'), patch.object(rv.adapter, 'scan', side_effect=fake_scan):
            return rv.scan(self.root, self.f.source, self.f.policy, 'http://harbor.invalid')

    def prepare(self):
        self.collect()
        self.checks()
        self.scan()

    def handoff(self):
        def git(_source, *args):
            if args == ('remote', 'get-url', 'origin'): return rv.control.REMOTE
            if args == ('rev-parse', 'HEAD'): return self.f.merge
            if args == ('status', '--porcelain'): return ''
            if args[:2] == ('rev-list', '--parents'): return ' '.join([self.f.merge, self.f.previous, self.f.source_commit])
            if args[:2] == ('ls-remote', '--tags'): return self.f.merge + '\trefs/tags/v' + self.f.version + '^{}'
            raise AssertionError(args)
        images = [{'RepoDigests': [rv.read(self.root / 'scan-identity.json')['immutable_image']], 'Config': {'Labels': {
            'org.opencontainers.image.revision': self.f.merge, 'app.version': self.f.version,
            'app.branch': 'prod', 'app.name': rv.gate.PRODUCT,
            'app.artifact.sha256': self.f.evidences['prod']['artifact']['sha256']}}}]
        with patch.object(rv.control, 'git', side_effect=git), \
             patch.object(rv.control, 'heads', return_value=(self.f.source_commit, self.f.merge)), \
             patch.dict(os.environ, NEXUS_BASE_URL=self.f.nexus), \
             patch.object(rv.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=json.dumps(images))), \
             patch.object(rv.finalizer, 'published_hash', return_value=self.f.evidences['prod']['artifact']['sha256']):
            return rv.handoff(self.root, self.f.source, self.f.policy, self.f.key,
                              dt.datetime.now(dt.timezone.utc), 'test-approver')

    def test_historical_expired_but_real_fresh_checks_sign_same_image(self):
        self.prepare()
        historical = self.root / 'historical'
        before = {str(p.relative_to(historical)): p.read_bytes() for p in historical.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'stale'):
            rv.recovery.validate_bundle(historical, self.f.policy, self.f.key, self.now)
        signed = self.handoff()
        request = rv.control.verify(signed, self.f.key)
        rv.validate_identity(request, self.f.key, dt.datetime.now(dt.timezone.utc))
        original = rv.control.verify(self.f.original, self.f.key)
        for field in ['commit', 'version', 'digest', 'image', 'prod_build', 'promotion', 'finalization', 'evidence_sha256']:
            self.assertEqual(request[field], original[field])
        self.assertEqual(request['promotion']['payload']['develop_build'], self.f.source_build)
        self.assertEqual(request['revalidation']['run'], self.run)
        self.assertEqual(request['recovery']['failed_receipt'], self.f.failure)
        self.assertEqual(request['recovery']['attempt_id'], rv.read(self.root/'revalidation-identity.json')['attempt_id'])
        self.assertLessEqual(rv.gate.timestamp(request['expires_at'])-rv.gate.timestamp(request['created_at']), dt.timedelta(minutes=15))
        self.assertEqual(before, {str(p.relative_to(historical)):p.read_bytes() for p in historical.rglob('*') if p.is_file()})

    def test_collect_rejects_forgery_upstream_and_repeat(self):
        cp = rv.recovery.prefix(rv.COORDINATOR, self.f.coordinator_build)
        signed = json.loads(self.f.archive[cp+'artifact/finalization.json'])
        signed['payload']['digest'] = 'sha256:'+'0'*64
        self.f.archive[cp+'artifact/finalization.json'] = rv.gate.canonical(signed)
        with self.assertRaisesRegex(ValueError, 'signature'): self.collect()
        # A partially collected bundle is immutable and cannot be retried in place.
        with self.assertRaises(FileExistsError): self.collect()

    def test_fresh_checks_require_new_reports_real_exit_and_exact_source(self):
        self.collect()
        path = self.f.source/'preexisting.xml'
        path.write_bytes(b'<testsuite><testcase/></testsuite>')
        with patch.object(rv, 'local_source'), patch.object(rv.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'preexist'):
                rv.check(self.root,self.f.source,'test',['true'],path)
            run.assert_not_called()
        with patch.object(rv, 'local_source', side_effect=rv.gate.InvalidEvidence('source changed')), \
             patch.object(rv.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'source changed'):
                rv.check(self.root,self.f.source,'contract',['true'])
            run.assert_not_called()
        def failed(command, **kwargs):
            kwargs['stdout'].write(b'failed trusted contract\n')
            return types.SimpleNamespace(returncode=1)
        with patch.object(rv, 'local_source'),patch.object(rv.subprocess,'run',side_effect=failed):
            with self.assertRaisesRegex(ValueError,'fresh check failed'):
                rv.check(self.root,self.f.source,'contract',['trusted-contract'])
        self.assertEqual(rv.read(self.root/'fresh/contract.json')['exit_code'],1)

    def test_runtime_rejects_wrong_run_digest_and_failed_cleanup(self):
        self.collect()
        seed = rv.read(self.root/'scan-identity.json')
        for field in ['image','commit','cleanup','run','original-build']:
            value = self.runtime()
            if field == 'run': value['revalidation_run']['coordinator_build'] -= 1
            elif field == 'original-build': value['revalidation_run']['original_prod_build'] += 1
            else: value[field] = 'wrong'
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'runtime verification'):
                rv.runtime_report(value,seed,self.run,self.f.prod_build)

    def test_fresh_bundle_rejects_tampered_reports_stale_checks_policy_and_scans(self):
        self.prepare()
        fresh=self.root/'fresh';evidence=rv.read(fresh/'revalidation.json')
        for kind in ['tamper','old-run','stale','policy','missing-graph','wrong-image','missing-check','stale-scan']:
            with self.subTest(kind=kind):
                saved={p:p.read_bytes() for p in fresh.rglob('*') if p.is_file()}
                policy=copy.deepcopy(self.f.policy);changed=copy.deepcopy(evidence)
                if kind=='tamper': (fresh/'contract.stdout').write_bytes(b'tampered')
                elif kind in {'old-run','stale'}:
                    p=fresh/'contract.json';value=rv.read(p)
                    if kind=='old-run':value['run']['build']-=1
                    else:value['completed_at']=(self.now-dt.timedelta(hours=2)).isoformat()
                    p.write_bytes(rv.gate.canonical(value))
                    changed['checks']=[rv.record(fresh,p) if ref['path']=='contract.json' else ref for ref in changed['checks']]
                elif kind=='policy':policy['required_scanners']=['trivy']
                elif kind=='missing-graph':changed.pop('package_graph')
                elif kind=='wrong-image':changed['immutable_image']+='0'
                elif kind=='missing-check':changed['checks']=changed['checks'][:-1]
                elif kind=='stale-scan':
                    p=fresh/'trivy.json';value=rv.read(p);value['completed_at']=(self.now-dt.timedelta(hours=2)).isoformat();p.write_bytes(rv.gate.canonical(value))
                    changed['reports']=[{'scanner':r['scanner'],**rv.record(fresh,p)} if r['scanner']=='trivy' else r for r in changed['reports']]
                (fresh/'revalidation.json').write_bytes(rv.gate.canonical(changed))
                with self.assertRaises((ValueError,KeyError)):
                    rv.validate_fresh(self.root,policy,self.f.key,dt.datetime.now(dt.timezone.utc))
                for p,raw in saved.items():p.write_bytes(raw)

    def test_owner_contract_rejects_recursive_changed_expired_and_weak_request(self):
        self.prepare();original=self.handoff()['payload']
        for kind in ['digest','version','recursive','extra-recovery','fresh-sha','expired','future','overlong','weak-pass','old-run','extra-root']:
            value=copy.deepcopy(original)
            if kind=='digest':value['digest']='sha256:'+'0'*64
            elif kind=='version':value['version']='9.9.9'
            elif kind=='recursive':value['revalidation']['original_request']=rv.control.sign(original,self.f.key)
            elif kind=='extra-recovery':value['recovery']['another_attempt']=True
            elif kind=='fresh-sha':value['revalidation']['evidence_sha256']='bad'
            elif kind=='expired':value['expires_at']=(self.now-dt.timedelta(minutes=1)).isoformat()
            elif kind=='future':value['created_at']=(self.now+dt.timedelta(days=1)).isoformat()
            elif kind=='overlong':value['expires_at']=(self.now+dt.timedelta(days=1)).isoformat()
            elif kind=='weak-pass':value['decision']['decision']='APPROVED_EXCEPTION'
            elif kind=='old-run':value['revalidation']['run']['build']=value['recovery']['coordinator_build']
            elif kind=='extra-root':value['restore_missing_runtime']=True
            with self.subTest(kind=kind),self.assertRaises((ValueError,KeyError)):
                rv.validate_identity(value,self.f.key,dt.datetime.now(dt.timezone.utc))


if __name__=='__main__':
    unittest.main()
