#!/usr/bin/env python3
"""Offline fake compiler/agent fixtures only. No Go, network, Jenkins or runtime."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest

path = Path(__file__).with_name('recognition-local-artifact.py')
spec = importlib.util.spec_from_file_location('local_artifact', path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def binary():
    raw = bytearray(128)
    raw[:6] = b'\x7fELF\x02\x01'
    raw[18:20] = struct.pack('<H', 183)
    return bytes(raw)


class FakeNative:
    def __init__(self):
        self.calls = []
        self.source = m.SOURCE
        self.dirty = False
        self.fail_test = False
        self.dirty_after_build = False
        self.bad_provenance = False

    def guard_agent(self):
        self.guarded = True

    def command(self, args, cwd, environment, timeout):
        self.calls.append((args, dict(environment), timeout))
        if args[:3] == ['git', 'rev-parse', 'HEAD']:
            return self.source.encode() + b'\n'
        if args[:2] == ['git', 'rev-parse']:
            return b'b' * 40 + b'\n'
        if args[:2] == ['git', 'status']:
            return b' M source.go\n' if self.dirty else b''
        if args[1:] == ['version']:
            return b'go version go1.26.10 linux/arm64\n'
        if args[1] == 'test':
            if self.fail_test:
                raise m.Blocked('build_command_failed')
            return b'ok fixture/contracts 0.001s\n'
        if args[1] == 'build':
            Path(args[args.index('-o') + 1]).write_bytes(binary())
            self.dirty = self.dirty_after_build
            return b''
        if args[1:3] == ['version', '-m']:
            return ("path\t" + m.MODULE + "/cmd/service/receipt_recognition\n"
                    "CGO_ENABLED=0 GOOS=linux GOARCH=arm64 vcs.revision=" +
                    ('f' * 40 if self.bad_provenance else m.SOURCE) + " vcs.modified=false\n").encode()
        raise AssertionError(args)


class FakeDocker:
    def __init__(self, record):
        self.record = record
        self.calls = []
        self.engine_id = '11111111-1111-1111-1111-111111111111'
        self.wrong_engine = False
        self.bad_platform = False
        self.bad_saved_binary = False
        self.bad_saved_tag = False
        self.base_layer = self.layer()
        self.base_diff = 'sha256:' + m.sha(self.base_layer)

    def layer(self, raw=None):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as t:
            if raw is not None:
                p = tarfile.TarInfo('usr/local/bin/receipt-recognition')
                p.size, p.mode = len(raw), 0o555
                t.addfile(p, io.BytesIO(raw))
        return out.getvalue()

    def guard(self):
        self.calls.append('guard')

    def engine(self):
        self.calls.append('engine')
        return '22222222-2222-2222-2222-222222222222' if self.wrong_engine else self.engine_id

    def inspect(self, reference):
        self.calls.append('inspect')
        if reference == m.BASE:
            return {'Id': 'sha256:' + 'b' * 64, 'RepoDigests': [m.BASE.replace(':nonroot@', '@')],
                    'Os': 'linux', 'Architecture': 'arm64', 'RootFS': {'Layers': [self.base_diff]}}
        return {'Id': self.image_id, 'Os': 'linux',
                'Architecture': 'amd64' if self.bad_platform else 'arm64',
                'Config': self.config['config'], 'RootFS': {'Layers': self.config['rootfs']['diff_ids']}}

    def build(self, context, reference, record):
        self.calls.append('build')
        assert sorted(p.name for p in context.iterdir()) == ['Dockerfile', 'receipt-recognition']
        assert (context / 'Dockerfile').read_text() == m.DOCKERFILE
        assert 'RUN ' not in m.DOCKERFILE
        self.copy_layer = self.layer((context / 'receipt-recognition').read_bytes())
        self.reference = reference
        self.config = {'architecture': 'arm64', 'os': 'linux',
            'config': {'User': 'nonroot:nonroot', 'Entrypoint': ['/usr/local/bin/receipt-recognition'],
                'Env': ['TZ=Asia/Taipei', 'APP_VERSION=' + m.VERSION + '-' + record['build_number']],
                'Labels': m.expected_labels(record)},
            'rootfs': {'type': 'layers', 'diff_ids': [self.base_diff, 'sha256:' + m.sha(self.copy_layer)]}}
        self.config_raw = m.raw_json(self.config)
        self.image_id = 'sha256:' + m.sha(self.config_raw)
        return self.image_id

    def save(self, reference, output):
        self.calls.append('save')
        assert reference == self.reference
        config_name = m.sha(self.config_raw) + '.json'
        manifest = [{'Config': config_name, 'RepoTags': [] if self.bad_saved_tag else [self.reference],
                     'Layers': ['base/layer.tar', 'copy/layer.tar']}]
        items = {'manifest.json': m.raw_json(manifest), config_name: self.config_raw,
                 'base/layer.tar': self.base_layer,
                 'copy/layer.tar': self.layer(b'wrong binary') if self.bad_saved_binary else self.copy_layer}
        with tarfile.open(output, 'x') as t:
            for name, raw in items.items():
                p = tarfile.TarInfo(name)
                p.size = len(raw)
                t.addfile(p, io.BytesIO(raw))


class Artifact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'VERSION').write_text(m.VERSION + '\n')
        self.output = self.root / 'artifact'
        self.sdk = self.root / 'sdk/go'
        for name in ('bin/go', 'pkg/tool/linux_arm64/compile'):
            p = self.sdk / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('offline SDK fixture only')
            p.chmod(0o755)
        self.sdk_receipt = {'version': m.SDK_VERSION, 'platform': 'linux-arm64',
            'archive_sha256': m.SDK_SHA, 'archive_size': 63827526,
            'source_url': 'https://go.dev/dl/go1.26.10.linux-arm64.tar.gz',
            'executable_sha256': {n: m.file_sha(self.sdk / n) for n in
                                 ('bin/go', 'pkg/tool/linux_arm64/compile')}}
        (self.sdk.parent / 'receipt.json').write_bytes(m.raw_json(self.sdk_receipt))
        self.record = {'schema_version': 1, 'job_name': m.JOB, 'build_number': '7',
            'build_url': 'http://jenkins.invalid/job/shiba-local-artifacts/job/recognition-autorecovery/7/',
            'source_commit': m.SOURCE, 'library_revision': 'a' * 40, 'requested_by': 'reviewer'}
        self.native = FakeNative()

    def build(self):
        return m.build(self.source, self.output, self.sdk, self.record, self.native)

    def verify(self, expected=None, build='7', library='a' * 40):
        receipt = self.output / 'artifact-receipt.json'
        return m.verify(self.output / 'recognition-local.tar', receipt,
                        expected or m.file_sha(receipt), build, library)

    def image(self, backend=None):
        self.build()
        backend = backend or FakeDocker(self.record)
        self.image_output = self.root / 'image-artifact'
        receipt = m.image(self.output / 'recognition-local.tar', self.output / 'artifact-receipt.json',
            m.file_sha(self.output / 'artifact-receipt.json'), '7', 'a' * 40,
            '11111111-1111-1111-1111-111111111111', self.image_output, backend)
        return receipt, backend

    def test_exact_source_archive_and_consumption_contract(self):
        receipt = self.build()
        self.assertTrue(self.native.guarded)
        self.assertEqual(receipt['result'], 'SUCCESS')
        self.assertFalse(receipt['deployment_performed'])
        proof = self.verify()
        self.assertTrue(proof['artifact_integrity_verified'])
        self.assertIn('requires trusted exact successful Jenkins', proof['ci_authenticity'])
        commands = [x[0] for x in self.native.calls]
        tests = next(a for a in commands if len(a) > 1 and a[1] == 'test')
        self.assertEqual(tests[3:-2], list(m.PACKAGES))
        for _, env, _ in self.native.calls:
            self.assertEqual(env['GOPROXY'], m.PROXY)
            self.assertEqual(env['GOTOOLCHAIN'], 'local')
            self.assertNotIn('GITHUB_TOKEN', env)
            self.assertNotIn('RECOGNITION_TOKEN', env)
        self.assertFalse(any(any(t in a for t in ('push', 'tag', 'docker', 'deploy', 'generate')) for a in commands))
        with tarfile.open(self.output / 'recognition-local.tar') as t:
            self.assertEqual(t.getnames(), ['receipt-recognition', 'artifact-manifest.json'])
            self.assertTrue(all(x.uid == 0 and x.mtime == 0 for x in t.getmembers()))

    def test_wrong_source_rejected_before_output_or_compile(self):
        self.native.source = 'c' * 40
        with self.assertRaisesRegex(m.Blocked, 'exact_source'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_dirty_checkout_rejected_before_compile(self):
        self.native.dirty = True
        with self.assertRaisesRegex(m.Blocked, 'dirty_source'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_failed_contracts_do_not_emit_success_archive(self):
        self.native.fail_test = True
        with self.assertRaises(m.Blocked):
            self.build()
        self.assertFalse((self.output / 'artifact-receipt.json').exists())
        self.assertFalse((self.output / 'recognition-local.tar').exists())

    def test_post_build_source_drift_rejected(self):
        self.native.dirty_after_build = True
        with self.assertRaisesRegex(m.Blocked, 'dirty_source'):
            self.build()
        self.assertFalse((self.output / 'artifact-receipt.json').exists())

    def test_sdk_tamper_rejected(self):
        (self.sdk / 'bin/go').write_text('changed')
        with self.assertRaisesRegex(m.Blocked, 'sdk_program'):
            self.build()

    def test_binary_embedded_revision_is_required(self):
        self.native.bad_provenance = True
        with self.assertRaisesRegex(m.Blocked, 'embedded_binary'):
            self.build()
        self.assertFalse((self.output / 'artifact-receipt.json').exists())

    def test_once_only_output_directory(self):
        self.build()
        with self.assertRaisesRegex(m.Blocked, 'new_separate_output'):
            self.build()

    def test_receipt_raw_hash_required(self):
        self.build()
        with self.assertRaisesRegex(m.Blocked, 'trusted_exact_build_receipt'):
            self.verify('0' * 64)

    def test_exact_build_and_library_required(self):
        self.build()
        for build, library in [('8', 'a' * 40), ('7', 'b' * 40)]:
            with self.assertRaisesRegex(m.Blocked, 'receipt_binding'):
                self.verify(build=build, library=library)

    def test_archive_tamper_rejected(self):
        self.build()
        with (self.output / 'recognition-local.tar').open('ab') as f:
            f.write(b'changed')
        with self.assertRaisesRegex(m.Blocked, 'archive_integrity'):
            self.verify()

    def test_unsafe_tar_member_rejected_even_if_fixture_receipt_hash_updated(self):
        self.build()
        archive = self.output / 'recognition-local.tar'
        archive.unlink()
        with tarfile.open(archive, 'x') as tar:
            symlink = tarfile.TarInfo('receipt-recognition')
            symlink.type, symlink.linkname = tarfile.SYMTYPE, '/etc/passwd'
            tar.addfile(symlink)
            info = tarfile.TarInfo('artifact-manifest.json')
            info.size = 2
            tar.addfile(info, io.BytesIO(b'{}'))
        receipt = self.output / 'artifact-receipt.json'
        r = json.loads(receipt.read_bytes())
        r['archive'].update(size=archive.stat().st_size, sha256=m.file_sha(archive))
        receipt.write_bytes(m.raw_json(r))
        with self.assertRaisesRegex(m.Blocked, 'archive_members'):
            self.verify()

    def test_receipt_cannot_redirect_to_prod_or_other_source(self):
        self.build()
        p = self.output / 'artifact-receipt.json'
        original = json.loads(p.read_bytes())
        for key, value in [('job_name', 'shiba-release-automation/prod'), ('source_commit', 'f' * 40)]:
            bad = copy.deepcopy(original)
            bad['identity'][key] = value
            p.write_bytes(m.raw_json(bad))
            with self.assertRaises(m.Blocked):
                self.verify()

    def test_non_arm64_binary_rejected(self):
        with self.assertRaisesRegex(m.Blocked, 'elf_required'):
            m.elf(b'not a Linux runtime')

    def test_saved_archive_must_retain_the_one_reviewed_local_reference(self):
        backend = FakeDocker(self.record)
        backend.bad_saved_tag = True
        with self.assertRaisesRegex(m.Blocked, 'saved_image_reference'):
            self.image(backend)
        self.assertFalse((self.image_output / 'image-receipt.json').exists())

    def collector_fixture(self, result='SUCCESS', bad_source=False, failed_stage=False,
                          library_fault=None):
        self.image()
        build = {'number': 7, 'building': False, 'result': result,
            'url': self.record['build_url'], 'actions': [{'_class': 'hudson.plugins.git.util.BuildData',
            'remoteUrls': [m.REPOSITORY],
            'lastBuiltRevision': {'SHA1': 'f' * 40 if bad_source else m.SOURCE}},
            {'_class': 'hudson.plugins.git.util.BuildData', 'remoteUrls': [m.LIBRARY_REPOSITORY],
             'lastBuiltRevision': {'SHA1': 'a' * 40}},
            {'_class': 'org.jenkinsci.plugins.workflow.libs.LibrariesAction', 'libraries': [
                {'name': m.LIBRARY_NAME, 'version': 'a' * 40, 'trusted': True}]},
            {'causes': [{'_class': 'hudson.model.Cause$UserIdCause', 'userId': 'reviewer'}]}],
            'artifacts': [{'relativePath': n} for n in
                         ('artifact/recognition-local.tar', 'artifact/artifact-manifest.json',
                          'artifact/artifact-receipt.json', 'image-artifact/recognition-local-image.tar',
                          'image-artifact/image-receipt.json')]}
        if library_fault == 'wrong_scm':
            build['actions'][1]['lastBuiltRevision']['SHA1'] = 'b' * 40
        elif library_fault == 'missing_scm':
            del build['actions'][1]
        elif library_fault == 'missing_action':
            del build['actions'][2]
        elif library_fault == 'wrong_action':
            build['actions'][2]['libraries'][0]['version'] = 'b' * 40
        elif library_fault == 'untrusted_action':
            build['actions'][2]['libraries'][0]['trusted'] = False
        elif library_fault == 'ambiguous_scm':
            build['actions'].append({'_class': 'hudson.plugins.git.util.BuildData',
                'remoteUrls': [m.LIBRARY_REPOSITORY], 'lastBuiltRevision': {'SHA1': 'b' * 40}})
        workflow = {'id': '7', 'status': 'SUCCESS', 'stages': [
            {'name': n, 'status': 'FAILED' if failed_stage and i == 2 else 'SUCCESS'}
            for i, n in enumerate(m.STAGES)]}
        calls = []
        def get(base, suffix, maximum):
            calls.append(suffix)
            if suffix.split('?')[0].endswith('api/json'):
                return m.raw_json(build)
            if suffix.endswith('wfapi/describe'):
                return m.raw_json(workflow)
            parent = self.image_output if '/image-artifact/' in suffix else self.output
            return (parent / suffix.rsplit('/', 1)[1]).read_bytes()
        return get, calls

    def test_exact_successful_ci_collection_anchors_receipt_without_triggering(self):
        get, calls = self.collector_fixture()
        target = self.root / 'collection'
        proof = m.collect('http://jenkins.invalid', '7', 'a' * 40, target, get)
        self.assertTrue(proof['artifact_integrity_verified'])
        self.assertEqual(proof['jenkins']['library']['revision'], 'a' * 40)
        self.assertTrue(proof['jenkins']['library']['trusted'])
        self.assertIn('exact completed Jenkins', proof['ci_authenticity'])
        self.assertEqual(len(calls), 7)
        self.assertTrue(all('/7/' in x and '/last' not in x and 'buildWith' not in x for x in calls))
        self.assertEqual((target / 'trusted-build-evidence.json').stat().st_mode & 0o777, 0o600)

    def test_wrong_or_missing_actual_build_library_never_qualifies(self):
        for fault in ('wrong_scm', 'missing_scm', 'missing_action', 'wrong_action',
                      'untrusted_action', 'ambiguous_scm'):
            with self.subTest(fault=fault):
                get, calls = self.collector_fixture(library_fault=fault)
                with self.assertRaisesRegex(m.Blocked, 'jenkins_.*library'):
                    m.collect('http://jenkins.invalid', '7', 'a' * 40,
                              self.root / ('collection-' + fault), get)
                self.assertEqual(len(calls), 1)
                # Reuse only fake fixture output for the next independent scenario.
                import shutil
                shutil.rmtree(self.image_output)
                shutil.rmtree(self.output)

    def test_failed_ci_never_downloads_runtime_archive(self):
        get, calls = self.collector_fixture(result='UNSTABLE')
        with self.assertRaisesRegex(m.Blocked, 'exact_successful_build'):
            m.collect('http://jenkins.invalid', '7', 'a' * 40, self.root / 'collection', get)
        self.assertEqual(len(calls), 1)

    def test_other_checkout_ci_never_qualifies(self):
        get, calls = self.collector_fixture(bad_source=True)
        with self.assertRaisesRegex(m.Blocked, 'checkout_source'):
            m.collect('http://jenkins.invalid', '7', 'a' * 40, self.root / 'collection', get)
        self.assertEqual(len(calls), 1)

    def test_failed_or_missing_flow_stage_never_qualifies(self):
        get, calls = self.collector_fixture(failed_stage=True)
        with self.assertRaisesRegex(m.Blocked, 'complete_artifact_flow'):
            m.collect('http://jenkins.invalid', '7', 'a' * 40, self.root / 'collection', get)
        self.assertEqual(len(calls), 2)

    def test_collector_requires_exact_build_number(self):
        with self.assertRaisesRegex(m.Blocked, 'exact_build_and_library'):
            m.collect('http://jenkins.invalid', 'lastSuccessfulBuild', 'a' * 40, self.root / 'collection',
                      lambda *args: self.fail('network called before input gate'))

    def test_disabled_job_renderer_pins_both_library_references(self):
        path = Path(__file__).with_name('recognition-local-artifact-job.py')
        spec = importlib.util.spec_from_file_location('local_artifact_job', path)
        job = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(job)
        import xml.etree.ElementTree as E
        raw = job.render('a' * 40, 'reviewer', '11111111-1111-1111-1111-111111111111')
        tree = E.fromstring(raw)
        self.assertEqual(tree.findtext('disabled'), 'true')
        self.assertEqual(list(tree.find('triggers')), [])
        script = tree.findtext('definition/script')
        self.assertIn('enabled: false', script)
        self.assertEqual(script.count('a' * 40), 2)
        self.assertIn(m.SOURCE, script)
        self.assertNotIn('ciPipeline(', script)
        with self.assertRaises(ValueError):
            job.render('main', 'reviewer', '11111111-1111-1111-1111-111111111111')

    def test_central_image_archive_has_verified_config_imageid_and_binary_chain(self):
        r, backend = self.image()
        self.assertEqual(r['scope'], m.IMAGE_SCOPE)
        self.assertEqual(r['image']['id'], 'sha256:' + r['image']['config_sha256'])
        self.assertEqual(r['elf']['binary_sha256'], m.sha(binary()))
        verified = m.verify_image(self.image_output / 'recognition-local-image.tar',
            self.image_output / 'image-receipt.json', m.file_sha(self.image_output / 'image-receipt.json'),
            '7', 'a' * 40)
        self.assertEqual(verified, r)
        self.assertEqual(backend.calls, ['guard','engine','inspect','build','inspect','save','engine'])

    def test_bad_elf_pin_never_enters_trusted_image_builder(self):
        self.build()
        backend = FakeDocker(self.record)
        with self.assertRaisesRegex(m.Blocked, 'trusted_exact_build_receipt'):
            m.image(self.output / 'recognition-local.tar', self.output / 'artifact-receipt.json', '0' * 64,
                    '7', 'a' * 40, backend.engine_id, self.root / 'image', backend)
        self.assertEqual(backend.calls, [])

    def test_wrong_builder_engine_does_not_build(self):
        backend = FakeDocker(self.record)
        backend.wrong_engine = True
        with self.assertRaisesRegex(m.Blocked, 'builder_engine'):
            self.image(backend)
        self.assertEqual(backend.calls, ['guard','engine'])

    def test_wrong_image_platform_does_not_save_or_emit_receipt(self):
        backend = FakeDocker(self.record)
        backend.bad_platform = True
        with self.assertRaisesRegex(m.Blocked, 'built_image_binding'):
            self.image(backend)
        self.assertNotIn('save', backend.calls)
        self.assertFalse((self.image_output / 'image-receipt.json').exists())

    def test_saved_image_layer_tamper_never_emits_receipt(self):
        backend = FakeDocker(self.record)
        backend.bad_saved_binary = True
        with self.assertRaisesRegex(m.Blocked, 'image_layer_digest'):
            self.image(backend)
        self.assertFalse((self.image_output / 'image-receipt.json').exists())


if __name__ == '__main__':
    unittest.main()
