#!/usr/bin/env python3
"""LOCAL Recognition artifact-only builder and read-only integrity verifier.

Only the central Pipeline calls build. Verification is not CI authentication:
the owner must first obtain exact successful Jenkins build evidence and the raw
receipt SHA through its existing trusted Jenkins account, never lastBuild.
"""
import argparse
import base64
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import subprocess
import tarfile
import urllib.parse
import urllib.request

SOURCE = '9d0400d003de68be827524d21543200bf78bfdfd'
REPOSITORY = 'https://github.com/ShibaDev2026/shiba-go-ditch-recognition-project.git'
LIBRARY_REPOSITORY = 'https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts.git'
LIBRARY_NAME = 'jenkins-pipeline'
BUILD_QUERY = 'api/json?tree=' + urllib.parse.quote(
    'number,building,result,url,actions[_class,remoteUrls,lastBuiltRevision[SHA1],'
    'libraries[name,version,trusted],causes[_class,userId]],artifacts[relativePath]', safe=',[]')
JOB = 'shiba-local-artifacts/recognition-autorecovery'
MODULE = 'github.com/ShibaDev2026/shiba-go-ditch-recognition-project'
VERSION = '0.1.11-local-autorecovery-20261028'
SDK_VERSION = '1.26.10'
SDK_SHA = '3f4227ce7b206a513e794f1393adb473a07b46e94d18bd15d1c684bbb7319797'
PROXY = 'http://shiba-docker-nexus-dev:8081/repository/go-group'
PACKAGES = ('./cmd/service/receipt_recognition', './internal/recognition/...',
            './internal/servicecontrolconsumer', './internal/ai/scorer', './internal/ocr/engine')
MAX_BINARY = 128 * 1024 * 1024
MAX_IMAGE = 256 * 1024 * 1024
BASE = 'gcr.io/distroless/static-debian12:nonroot@sha256:1b7b9f0f0e0a1d2155f531db587cc48ec26aaf97ab64364225f5bf18a054e66a'
IMAGE_SCOPE = 'LOCAL_RECOGNITION_DOCKER_IMAGE'
STAGES = ('Check isolated nonprivileged agent', 'Checkout reviewed runtime source',
          'Load reviewed builder and pinned SDK', 'Build test and archive LOCAL runtime only',
          'Build and save approved LOCAL image')
DOCKERFILE = ('FROM ' + BASE + '\n'
    'ARG RUNTIME_VERSION\nARG SOURCE_COMMIT\nARG LIBRARY_REVISION\n'
    'COPY receipt-recognition /usr/local/bin/receipt-recognition\n'
    'USER nonroot:nonroot\nENV TZ=Asia/Taipei APP_VERSION=${RUNTIME_VERSION}\n'
    'LABEL org.opencontainers.image.title="shiba-go-ditch-recognition-project" '
    'org.opencontainers.image.revision="${SOURCE_COMMIT}" '
    'org.opencontainers.image.version="${RUNTIME_VERSION}" '
    'com.shiba.local-artifact.library-revision="${LIBRARY_REVISION}" '
    'com.shiba.local-artifact.scope="LOCAL_RECOGNITION_DOCKER_IMAGE"\n'
    'EXPOSE 8097\nHEALTHCHECK --interval=10s --timeout=4s --retries=6 CMD '
    '["/usr/local/bin/receipt-recognition","healthcheck","-url","http://127.0.0.1:8097/healthz","-timeout","3s"]\n'
    'ENTRYPOINT ["/usr/local/bin/receipt-recognition"]\n')


class Blocked(RuntimeError):
    pass


def need(ok, reason):
    if not ok:
        raise Blocked(reason)


def raw_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def regular(path, maximum):
    s = Path(path).lstat()
    need(stat.S_ISREG(s.st_mode) and s.st_size <= maximum, 'regular_bounded_file_required')
    return s


def identity(value):
    need(set(value) == {'schema_version', 'job_name', 'build_number', 'build_url',
                        'source_commit', 'library_revision', 'requested_by'}, 'identity_schema')
    need(value['schema_version'] == 1 and value['job_name'] == JOB and
         value['source_commit'] == SOURCE and re.fullmatch(r'[1-9][0-9]{0,8}', value['build_number']) and
         re.fullmatch(r'[0-9a-f]{40}', value['library_revision']) and
         re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}', value['requested_by']), 'identity_binding')
    url = urllib.parse.urlsplit(value['build_url'])
    suffix = '/job/shiba-local-artifacts/job/recognition-autorecovery/' + value['build_number'] + '/'
    need(url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password
         and not url.query and not url.fragment and url.path == suffix, 'exact_build_url_required')
    return value


class Native:
    def command(self, args, cwd, environment, timeout):
        result = subprocess.run(args, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, check=False, timeout=timeout)
        need(result.returncode == 0, 'build_command_failed')
        need(len(result.stdout) <= 16 * 1024 * 1024, 'build_log_too_large')
        return result.stdout

    def guard_agent(self):
        need(os.getuid() != 0 and platform.system() == 'Linux' and
             platform.machine() in ('arm64', 'aarch64'), 'isolated_linux_arm64_agent_required')
        need(not any(Path(p).exists() for p in ('/var/run/docker.sock', '/run/docker.sock',
             '/var/jenkins_home', '/Users/surpend/Developer')) and
             not os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT'),
             'privileged_or_host_mounted_agent_rejected')


def verify_sdk(root):
    root = Path(root)
    need(root.is_absolute() and root.resolve(strict=True) == root, 'canonical_sdk_required')
    p = root.parent / 'receipt.json'
    regular(p, 16384)
    value = json.loads(p.read_bytes())
    need(value['version'] == SDK_VERSION and value['platform'] == 'linux-arm64' and
         value['archive_sha256'] == SDK_SHA and value['archive_size'] == 63827526 and
         value['source_url'] == 'https://go.dev/dl/go1.26.10.linux-arm64.tar.gz', 'pinned_sdk_receipt')
    for name in ('bin/go', 'pkg/tool/linux_arm64/compile'):
        program = root / name
        regular(program, MAX_BINARY)
        need(program.resolve(strict=True) == program and os.access(program, os.X_OK) and
             file_sha(program) == value['executable_sha256'][name], 'sdk_program_identity')
    return value


def elf(raw):
    need(len(raw) >= 64 and len(raw) <= MAX_BINARY and raw[:4] == b'\x7fELF' and
         raw[4:6] == b'\x02\x01' and struct.unpack('<H', raw[18:20])[0] == 183,
         'linux_arm64_elf_required')


def build(source, output, sdk, record, native=None):
    native = native or Native()
    native.guard_agent()
    record = identity(record)
    source, output, sdk = Path(source), Path(output), Path(sdk)
    need(source.is_absolute() and source.resolve(strict=True) == source, 'canonical_source_required')
    need(output.is_absolute() and output.resolve() == output and not output.exists() and
         not output.is_relative_to(source) and not source.is_relative_to(output), 'new_separate_output_required')
    sdk_receipt = verify_sdk(sdk)
    environment = {'PATH': str(sdk / 'bin') + ':/usr/bin:/bin', 'HOME': str(output / 'work/home'),
        'LANG': 'C', 'LC_ALL': 'C', 'GOENV': 'off', 'GOTOOLCHAIN': 'local', 'GOROOT': str(sdk),
        'GOPROXY': PROXY, 'GOSUMDB': 'sum.golang.org', 'GONOSUMDB': '', 'GOPRIVATE': '',
        'GONOPROXY': '', 'GOFLAGS': '', 'CGO_ENABLED': '0', 'GIT_TERMINAL_PROMPT': '0',
        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
    def run(args, timeout=30):
        return native.command(args, source, environment, timeout)
    need(run(['git', 'rev-parse', 'HEAD']).decode().strip() == SOURCE, 'exact_source_commit_required')
    tree = run(['git', 'rev-parse', 'HEAD^{tree}']).decode().strip()
    need(re.fullmatch(r'[0-9a-f]{40}', tree), 'source_tree_identity')
    def clean():
        need(not run(['git', 'status', '--porcelain=v1', '--untracked-files=all']), 'dirty_source_rejected')
    clean()
    need((source / 'VERSION').read_text().strip() == VERSION, 'local_version_required')
    output.mkdir(mode=0o700)
    for name in ('work/home', 'work/cache', 'work/mod', 'work/tmp'):
        (output / name).mkdir(mode=0o700, parents=True)
    environment.update(GOCACHE=str(output / 'work/cache'), GOMODCACHE=str(output / 'work/mod'),
                       TMPDIR=str(output / 'work/tmp'))
    go = str(sdk / 'bin/go')
    need(run([go, 'version']).decode().strip() == 'go version go1.26.10 linux/arm64', 'sdk_runtime_version')
    log = run([go, 'test', '-mod=readonly', *PACKAGES, '-count=1', '-timeout=90s'], 600)
    (output / 'contracts.log').write_bytes(log)
    binary = output / 'work/receipt-recognition'
    environment.update(GOOS='linux', GOARCH='arm64')
    run([go, 'build', '-mod=readonly', '-trimpath', '-buildvcs=true', '-o', str(binary),
         './cmd/service/receipt_recognition'], 300)
    regular(binary, MAX_BINARY)
    raw = binary.read_bytes()
    elf(raw)
    metadata = run([go, 'version', '-m', str(binary)]).decode()
    for field in ('path\t' + MODULE + '/cmd/service/receipt_recognition', 'CGO_ENABLED=0',
                  'GOOS=linux', 'GOARCH=arm64', 'vcs.revision=' + SOURCE, 'vcs.modified=false'):
        need(field in metadata, 'embedded_binary_provenance')
    clean()
    need(verify_sdk(sdk) == sdk_receipt, 'sdk_changed_during_build')
    manifest = {'schema_version': 1, 'scope': 'LOCAL_RECOGNITION_OWNER_ARTIFACT',
        'repository': REPOSITORY, 'source_commit': SOURCE, 'source_tree': tree, 'source_dirty': False,
        'version': VERSION, 'platform': 'linux-arm64', 'cgo_enabled': False, 'identity': record,
        'binary': {'path': 'receipt-recognition', 'sha256': sha(raw), 'size': len(raw)},
        'toolchain': sdk_receipt, 'packages_tested': list(PACKAGES), 'contracts_sha256': sha(log),
        'builder_sha256': file_sha(Path(__file__)), 'runtime_operations': 0,
        'model_calls': 0, 'prod_downstream': False, 'deployment_performed': False}
    manifest_raw = raw_json(manifest)
    archive = output / 'recognition-local.tar'
    with tarfile.open(archive, 'x', format=tarfile.USTAR_FORMAT) as tar:
        for name, data, mode in [('receipt-recognition', raw, 0o555),
                                 ('artifact-manifest.json', manifest_raw, 0o644)]:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime, info.uid, info.gid = len(data), mode, 0, 0, 0
            tar.addfile(info, io.BytesIO(data))
    receipt = {'schema_version': 1, 'scope': manifest['scope'], 'result': 'SUCCESS',
        'identity': record, 'archive': {'path': archive.name, 'sha256': file_sha(archive),
        'size': archive.stat().st_size}, 'manifest_sha256': sha(manifest_raw),
        'binary_sha256': sha(raw), 'prod_downstream': False, 'deployment_performed': False}
    (output / 'artifact-manifest.json').write_bytes(manifest_raw)
    (output / 'artifact-receipt.json').write_bytes(raw_json(receipt))
    (output / 'SHA256SUMS').write_text(''.join(file_sha(output / n) + '  ' + n + '\n'
        for n in ('recognition-local.tar', 'artifact-manifest.json', 'artifact-receipt.json', 'contracts.log')))
    return receipt


def verify(archive, receipt_path, expected_receipt_sha, build_number, library_revision):
    archive, receipt_path = Path(archive), Path(receipt_path)
    regular(receipt_path, 65536)
    raw = receipt_path.read_bytes()
    need(re.fullmatch(r'[0-9a-f]{64}', expected_receipt_sha) and sha(raw) == expected_receipt_sha,
         'trusted_exact_build_receipt_sha_required')
    receipt = json.loads(raw)
    record = identity(receipt['identity'])
    need(record['build_number'] == str(build_number) and record['library_revision'] == library_revision and
         receipt['schema_version'] == 1 and receipt['scope'] == 'LOCAL_RECOGNITION_OWNER_ARTIFACT' and
         receipt['result'] == 'SUCCESS' and receipt['prod_downstream'] is False and
         receipt['deployment_performed'] is False, 'receipt_binding')
    regular(archive, MAX_BINARY + 1024 * 1024)
    need(receipt['archive']['path'] == 'recognition-local.tar' and
         archive.stat().st_size == receipt['archive']['size'] and
         file_sha(archive) == receipt['archive']['sha256'], 'archive_integrity')
    with tarfile.open(archive, 'r:') as tar:
        members = tar.getmembers()
        need(len(members) == 2 and {m.name for m in members} ==
             {'receipt-recognition', 'artifact-manifest.json'} and
             all(m.isfile() and 0 < m.size <= MAX_BINARY for m in members), 'archive_members')
        manifest_raw = tar.extractfile('artifact-manifest.json').read(65537)
        binary = tar.extractfile('receipt-recognition').read(MAX_BINARY + 1)
    need(len(manifest_raw) <= 65536 and sha(manifest_raw) == receipt['manifest_sha256'], 'manifest_integrity')
    manifest = json.loads(manifest_raw)
    elf(binary)
    need(manifest['schema_version'] == 1 and manifest['scope'] == receipt['scope'] and
         manifest['identity'] == record and manifest['repository'] == REPOSITORY and
         manifest['source_commit'] == SOURCE and manifest['source_dirty'] is False and
         manifest['version'] == VERSION and manifest['platform'] == 'linux-arm64' and
         manifest['cgo_enabled'] is False and manifest['prod_downstream'] is False and
         manifest['deployment_performed'] is False and manifest['binary']['size'] == len(binary) and
         manifest['runtime_operations'] == 0 and manifest['model_calls'] == 0 and
         manifest['packages_tested'] == list(PACKAGES) and
         manifest['toolchain']['version'] == SDK_VERSION and
         manifest['toolchain']['archive_sha256'] == SDK_SHA and
         manifest['builder_sha256'] == file_sha(Path(__file__)) and
         re.fullmatch(r'[0-9a-f]{40}', manifest['source_tree']) and
         manifest['binary']['sha256'] == receipt['binary_sha256'] == sha(binary), 'manifest_binding')
    return {'artifact_integrity_verified': True, 'source_commit': SOURCE, 'identity': record,
            'binary_sha256': sha(binary), 'ci_authenticity': 'requires trusted exact successful Jenkins build evidence',
            'runtime_activated': False}


def expected_labels(record):
    return {'org.opencontainers.image.title': 'shiba-go-ditch-recognition-project',
        'org.opencontainers.image.revision': SOURCE,
        'org.opencontainers.image.version': VERSION + '-' + record['build_number'],
        'com.shiba.local-artifact.library-revision': record['library_revision'],
        'com.shiba.local-artifact.scope': IMAGE_SCOPE}


def image_reference(record):
    return 'shiba-recognition-local-ci:' + SOURCE[:12] + '-b' + record['build_number']


def inspect_save(path, binary_sha, record):
    regular(path, MAX_IMAGE)
    with tarfile.open(path, 'r:') as archive:
        members = archive.getmembers()
        need(len(members) < 10000 and len({m.name for m in members}) == len(members) and
             all(not Path(m.name).is_absolute() and '..' not in Path(m.name).parts for m in members),
             'unsafe_image_archive_members')
        def read(name, maximum):
            member = archive.getmember(name)
            need(member.isfile() and member.size <= maximum, 'image_archive_member_type')
            raw = archive.extractfile(member).read(maximum + 1)
            need(len(raw) <= maximum, 'image_archive_member_size')
            return raw
        manifests_raw = read('manifest.json', 65536)
        manifests = json.loads(manifests_raw)
        need(isinstance(manifests, list) and len(manifests) == 1, 'single_image_archive_required')
        manifest = manifests[0]
        need(manifest.get('RepoTags') == [image_reference(record)], 'saved_image_reference_binding')
        config_raw = read(manifest['Config'], 1024 * 1024)
        config = json.loads(config_raw)
        need(config.get('os') == 'linux' and config.get('architecture') == 'arm64', 'image_platform')
        c = config['config']
        need(c.get('User') == 'nonroot:nonroot' and
             c.get('Entrypoint') == ['/usr/local/bin/receipt-recognition'] and
             all(c.get('Labels', {}).get(k) == v for k, v in expected_labels(record).items()) and
             c.get('Env', []).count('APP_VERSION=' + VERSION + '-' + record['build_number']) == 1,
             'image_runtime_binding')
        layers = manifest.get('Layers')
        diffs = config.get('rootfs', {}).get('diff_ids', [])
        need(isinstance(layers, list) and 0 < len(layers) <= 64 and len(diffs) == len(layers), 'image_layers_missing')
        expanded_total = 0
        for name, expected_diff in zip(layers, diffs):
            layer_raw = read(name, MAX_IMAGE)
            if layer_raw.startswith(b'\x1f\x8b'):
                with gzip.GzipFile(fileobj=io.BytesIO(layer_raw)) as packed:
                    layer_raw = packed.read(MAX_IMAGE + 1)
            expanded_total += len(layer_raw)
            need(len(layer_raw) <= MAX_IMAGE and expanded_total <= 2 * MAX_IMAGE and
                 expected_diff == 'sha256:' + sha(layer_raw), 'image_layer_digest')
    # Only the central COPY layer may add a file. Never extract a host path.
    with tarfile.open(fileobj=io.BytesIO(layer_raw), mode='r:*') as layer:
        items = layer.getmembers()
        need(len(items) < 20 and len({m.name for m in items}) == len(items), 'copy_layer_members')
        files = [m for m in items if not m.isdir()]
        need(len(files) == 1 and files[0].isfile() and
             files[0].name.removeprefix('./') == 'usr/local/bin/receipt-recognition' and
             files[0].size <= MAX_BINARY and
             all(not Path(m.name).is_absolute() and '..' not in Path(m.name).parts for m in items),
             'central_copy_layer_required')
        binary = layer.extractfile(files[0]).read(MAX_BINARY + 1)
        elf(binary)
        need(sha(binary) == binary_sha, 'image_binary_binding')
    return {'id': 'sha256:' + sha(config_raw), 'config_sha256': sha(config_raw),
        'reference': image_reference(record),
        'docker_save_manifest_sha256': sha(manifests_raw), 'os': 'linux', 'architecture': 'arm64',
        'user': c['User'], 'entrypoint': c['Entrypoint'], 'labels': c['Labels'],
        'rootfs_diff_ids': config.get('rootfs', {}).get('diff_ids', [])}


class DockerBuilder:
    def __init__(self, output):
        self.output = output
        self.environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
            'HOME': str(output / 'private'), 'DOCKER_CONFIG': str(output / 'private/docker'),
            'DOCKER_BUILDKIT': '1'}

    def guard(self):
        need(platform.system() == 'Linux' and platform.machine() in ('arm64', 'aarch64') and
             Path('/var/run/docker.sock').exists() and not os.environ.get('DOCKER_HOST') and
             not os.environ.get('DOCKER_CONTEXT'), 'fixed_trusted_image_builder_required')

    def run(self, args, timeout=30):
        result = subprocess.run(['docker', *args], env=self.environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
        need(result.returncode == 0 and len(result.stdout) <= 16 * 1024 * 1024,
             'image_builder_command_failed_or_unknown')
        return result.stdout

    def engine(self):
        return self.run(['info', '--format', '{{.ID}}']).decode().strip()

    def inspect(self, ref):
        rows = json.loads(self.run(['image', 'inspect', ref]))
        need(len(rows) == 1, 'ambiguous_builder_image')
        return rows[0]

    def build(self, context, ref, record):
        self.run(['build', '--network', 'none', '--pull=false', '--platform', 'linux/arm64',
            '--iidfile', str(self.output / 'image-id.txt'), '--tag', ref,
            '--build-arg', 'RUNTIME_VERSION=' + VERSION + '-' + record['build_number'],
            '--build-arg', 'SOURCE_COMMIT=' + SOURCE,
            '--build-arg', 'LIBRARY_REVISION=' + record['library_revision'],
            '--file', str(context / 'Dockerfile'), str(context)], 300)
        value = (self.output / 'image-id.txt').read_text().strip()
        need(re.fullmatch(r'sha256:[0-9a-f]{64}', value), 'immutable_image_id_required')
        return value

    def save(self, reference, output):
        # Named save retains the sole LOCAL tag. Its saved immutable config ID is
        # checked below, so a concurrent tag move cannot yield a valid receipt.
        self.run(['save', '--output', str(output), reference], 180)


def image(elf_archive, elf_receipt, expected_receipt_sha, number, library_revision,
          expected_engine, output, backend=None):
    proof = verify(elf_archive, elf_receipt, expected_receipt_sha, number, library_revision)
    record = proof['identity']
    need(re.fullmatch(r'[0-9a-f-]{36}', expected_engine), 'reviewed_builder_engine_required')
    output = Path(output)
    need(output.is_absolute() and output.resolve() == output and not output.exists(), 'new_image_output_required')
    output.mkdir(mode=0o700)
    (output / 'private/docker').mkdir(mode=0o700, parents=True)
    context = output / 'context'
    context.mkdir(mode=0o700)
    backend = backend or DockerBuilder(output)
    backend.guard()
    need(backend.engine() == expected_engine, 'builder_engine_mismatch')
    base = backend.inspect(BASE)
    need(BASE.replace(':nonroot@', '@') in base.get('RepoDigests', []) and base.get('Os') == 'linux' and
         base.get('Architecture') == 'arm64', 'approved_base_must_exist_locally')
    with tarfile.open(elf_archive, 'r:') as archive:
        binary = archive.extractfile('receipt-recognition').read(MAX_BINARY + 1)
    need(sha(binary) == proof['binary_sha256'], 'elf_transfer_drift')
    (context / 'receipt-recognition').write_bytes(binary)
    (context / 'receipt-recognition').chmod(0o555)
    (context / 'Dockerfile').write_text(DOCKERFILE)
    ref = image_reference(record)
    image_id = backend.build(context, ref, record)
    built = backend.inspect(image_id)
    need(built['Id'] == image_id and built.get('Os') == 'linux' and built.get('Architecture') == 'arm64' and
         built['Config'].get('User') == 'nonroot:nonroot' and
         built['Config'].get('Entrypoint') == ['/usr/local/bin/receipt-recognition'] and
         all(built['Config'].get('Labels', {}).get(k) == v for k, v in expected_labels(record).items()) and
         built['RootFS']['Layers'][:-1] == base['RootFS']['Layers'], 'built_image_binding')
    archive = output / 'recognition-local-image.tar'
    backend.save(ref, archive)
    inspected = inspect_save(archive, proof['binary_sha256'], record)
    need(inspected['id'] == image_id and inspected['rootfs_diff_ids'] == built['RootFS']['Layers'] and
         backend.engine() == expected_engine, 'saved_image_or_engine_drift')
    receipt = {'schema_version': 1, 'scope': IMAGE_SCOPE, 'result': 'SUCCESS', 'identity': record,
        'version': VERSION, 'runtime_version': VERSION + '-' + str(number), 'platform': 'linux/arm64',
        'archive': {'path': archive.name, 'sha256': file_sha(archive), 'size': archive.stat().st_size},
        'image': dict(inspected, reference=ref, base_reference=BASE, base_image_id=base['Id']),
        'elf': {'binary_sha256': proof['binary_sha256'], 'receipt_sha256': expected_receipt_sha,
                'archive_sha256': file_sha(elf_archive),
                'manifest_sha256': json.loads(Path(elf_receipt).read_bytes())['manifest_sha256']},
        'builder_engine_id': expected_engine, 'central_dockerfile_sha256': sha(DOCKERFILE.encode()),
        'registry_push': False, 'prod_downstream': False, 'deployment_performed': False}
    (output / 'image-receipt.json').write_bytes(raw_json(receipt))
    (output / 'SHA256SUMS').write_text(''.join(file_sha(output / n) + '  ' + n + '\n'
        for n in ('recognition-local-image.tar', 'image-receipt.json')))
    return receipt


def verify_image(archive, receipt_path, receipt_sha, number, library_revision):
    regular(receipt_path, 65536)
    raw = Path(receipt_path).read_bytes()
    need(re.fullmatch(r'[0-9a-f]{64}', receipt_sha) and sha(raw) == receipt_sha, 'image_receipt_raw_binding')
    r = json.loads(raw)
    record = identity(r['identity'])
    need(r['schema_version'] == 1 and r['scope'] == IMAGE_SCOPE and r['result'] == 'SUCCESS' and
         r['version'] == VERSION and r['runtime_version'] == VERSION + '-' + str(number) and
         record['build_number'] == str(number) and record['library_revision'] == library_revision and
         r['platform'] == 'linux/arm64' and r['registry_push'] is False and
         r['prod_downstream'] is False and r['deployment_performed'] is False and
         r['image']['base_reference'] == BASE and
         r['central_dockerfile_sha256'] == sha(DOCKERFILE.encode()), 'image_receipt_scope')
    regular(archive, MAX_IMAGE)
    need(r['archive']['path'] == 'recognition-local-image.tar' and
         Path(archive).stat().st_size == r['archive']['size'] and
         file_sha(archive) == r['archive']['sha256'], 'image_archive_integrity')
    observed = inspect_save(archive, r['elf']['binary_sha256'], record)
    need(all(r['image'].get(k) == v for k, v in observed.items()), 'saved_image_receipt_mismatch')
    return r


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def jenkins_get(base, suffix, maximum):
    # Reuse the existing trusted evidence collector's credential convention.
    # No credential bytes enter arguments, output, receipt, archive or redirects.
    url = urllib.parse.urlsplit(base)
    need(url.scheme in ('http', 'https') and url.hostname in
         ('localhost', '127.0.0.1', 'shiba-docker-jenkins-controller-dev') and
         not url.username and not url.password and not url.query and not url.fragment and
         url.path in ('', '/'), 'trusted_local_jenkins_base_required')
    user, token = os.environ.get('JENKINS_API_USER', ''), os.environ.get('JENKINS_API_TOKEN', '')
    need(user and token and not any(c in user + token for c in '\r\n'), 'existing_jenkins_read_credentials_required')
    auth = base64.b64encode((user + ':' + token).encode()).decode()
    request = urllib.request.Request(base.rstrip('/') + suffix, method='GET',
                                     headers={'Authorization': 'Basic ' + auth})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        raw = response.read(maximum + 1)
        need(response.status == 200 and len(raw) <= maximum, 'jenkins_response_rejected')
        return raw


def collect(base, number, library_revision, output, get=None):
    """Read one fixed successful build and complete image artifacts; never trigger it."""
    need(re.fullmatch(r'[1-9][0-9]{0,8}', str(number)) and
         re.fullmatch(r'[0-9a-f]{40}', library_revision), 'exact_build_and_library_required')
    output = Path(output)
    need(output.is_absolute() and output.resolve() == output and not output.exists(),
         'new_private_collection_directory_required')
    get = get or jenkins_get
    prefix = '/job/shiba-local-artifacts/job/recognition-autorecovery/' + str(number) + '/'
    # Explicit exported fields/depth bind the historical build, never mutable job XML.
    # A compact API response which omits Library SCM metadata cannot qualify.
    build_raw = get(base, prefix + BUILD_QUERY, 4 * 1024 * 1024)
    build_record = json.loads(build_raw)
    need(build_record.get('number') == int(number) and build_record.get('building') is False and
         build_record.get('result') == 'SUCCESS' and
         urllib.parse.urlsplit(build_record.get('url', '')).path == prefix, 'exact_successful_build_required')
    actions = build_record.get('actions', [])
    def scm_revision(repository, expected, reason):
        matches = [a for a in actions if a.get('_class') == 'hudson.plugins.git.util.BuildData'
                   and repository in a.get('remoteUrls', [])]
        need(matches and all(a.get('remoteUrls') == [repository] and
                             a.get('lastBuiltRevision', {}).get('SHA1') == expected
                             for a in matches), reason)
    scm_revision(REPOSITORY, SOURCE, 'jenkins_checkout_source_mismatch')
    scm_revision(LIBRARY_REPOSITORY, library_revision, 'jenkins_library_scm_mismatch_or_missing')
    libraries = [lib for a in actions
                 if a.get('_class') == 'org.jenkinsci.plugins.workflow.libs.LibrariesAction'
                 for lib in a.get('libraries', [])]
    need(len(libraries) == 1 and libraries[0].get('name') == LIBRARY_NAME and
         libraries[0].get('version') == library_revision and libraries[0].get('trusted') is True,
         'jenkins_trusted_exact_library_action_required')
    names = {'artifact/recognition-local.tar': MAX_BINARY + 1024 * 1024,
             'artifact/artifact-manifest.json': 65536, 'artifact/artifact-receipt.json': 65536,
             'image-artifact/recognition-local-image.tar': MAX_IMAGE,
             'image-artifact/image-receipt.json': 65536}
    listed = {x.get('relativePath') for x in build_record.get('artifacts', [])}
    need(set(names).issubset(listed), 'required_build_artifacts_missing')
    workflow_raw = get(base, prefix + 'wfapi/describe', 1024 * 1024)
    workflow = json.loads(workflow_raw)
    stages = workflow.get('stages', [])
    need(str(workflow.get('id')) == str(number) and workflow.get('status') == 'SUCCESS' and
         len(stages) == len(STAGES) and {s.get('name') for s in stages} == set(STAGES) and
         all(s.get('status') == 'SUCCESS' for s in stages), 'complete_artifact_flow_required')
    data = {name.rsplit('/', 1)[1]: get(base, prefix + 'artifact/' + name, maximum)
            for name, maximum in names.items()}
    image_receipt = json.loads(data['image-receipt.json'])
    ridentity = identity(image_receipt['identity'])
    causes = [c for a in build_record.get('actions', []) for c in a.get('causes', [])]
    need(len(causes) == 1 and causes[0].get('_class') == 'hudson.model.Cause$UserIdCause' and
         causes[0].get('userId') == ridentity['requested_by'], 'approved_manual_build_cause_required')
    output.mkdir(mode=0o700)
    for name, raw in list(data.items()) + [('jenkins-build.json', build_raw), ('jenkins-workflow.json', workflow_raw)]:
        with (output / name).open('xb') as f:
            os.chmod(f.name, 0o600)
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    elf_raw = data['artifact-receipt.json']
    elf_proof = verify(output / 'recognition-local.tar', output / 'artifact-receipt.json',
                       sha(elf_raw), str(number), library_revision)
    image_proof = verify_image(output / 'recognition-local-image.tar', output / 'image-receipt.json',
                               sha(data['image-receipt.json']), str(number), library_revision)
    need(elf_proof['identity'] == ridentity == image_proof['identity'] and
         ridentity['build_url'] == build_record['url'] and
         image_proof['elf']['receipt_sha256'] == sha(elf_raw) and
         image_proof['elf']['binary_sha256'] == elf_proof['binary_sha256'] and
         image_proof['elf']['archive_sha256'] == sha(data['recognition-local.tar']) and
         image_proof['elf']['manifest_sha256'] == sha(data['artifact-manifest.json']), 'complete_elf_image_chain_required')
    proof = {'schema_version': 1, 'kind': 'recognition-local-ci-collection/v1', 'scope': IMAGE_SCOPE,
        'result': 'SUCCESS', 'collect_finished': True, 'pipeline_final_success': True,
        'identity': ridentity, 'version': VERSION, 'runtime_version': image_proof['runtime_version'],
        'image_receipt_sha256': sha(data['image-receipt.json']),
        'archive_sha256': sha(data['recognition-local-image.tar']),
        'archive_size': len(data['recognition-local-image.tar']),
        'api_sha256': sha(build_raw), 'wfapi_sha256': sha(workflow_raw),
        'image_id': image_proof['image']['id'],
        'image': image_proof['image'], 'elf': image_proof['elf'],
        'image_archive': dict(image_proof['archive'], path=str(output / 'recognition-local-image.tar')),
        'image_receipt': {'path': str(output / 'image-receipt.json'), 'sha256': sha(data['image-receipt.json'])},
        'jenkins': {'base_url': base, 'job': JOB, 'build_number': str(number), 'building': False,
                    'result': 'SUCCESS', 'build_record_sha256': sha(build_raw),
                    'workflow_sha256': sha(workflow_raw),
                    'library': {'name': LIBRARY_NAME, 'repository': LIBRARY_REPOSITORY,
                                'revision': library_revision, 'trusted': True,
                                'authority': 'exact build BuildData and LibrariesAction'}},
        'artifact_sha256': {name: sha(raw) for name, raw in data.items()},
        'artifact_integrity_verified': True, 'ci_authenticity': 'fixed exact completed Jenkins build, workflow and artifact GETs',
        'runtime_activated': False, 'deployment_performed': False, 'registry_push': False,
        'signature': 'none; existing authenticated exact-build Jenkins evidence authority'}
    with (output / 'trusted-build-evidence.json').open('xb') as f:
        os.chmod(f.name, 0o600)
        f.write(raw_json(proof));f.flush();os.fsync(f.fileno())
    fd = os.open(output, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return proof


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build')
    for name in ('source', 'output', 'sdk-root', 'identity'):
        b.add_argument('--' + name, type=Path, required=True)
    v = sub.add_parser('verify')
    v.add_argument('--archive', type=Path, required=True)
    v.add_argument('--receipt', type=Path, required=True)
    v.add_argument('--expected-receipt-sha256', required=True)
    v.add_argument('--build-number', required=True)
    v.add_argument('--library-revision', required=True)
    i = sub.add_parser('image')
    for name in ('elf-archive', 'elf-receipt', 'output'):
        i.add_argument('--' + name, type=Path, required=True)
    for name in ('expected-elf-receipt-sha256', 'build-number', 'library-revision', 'expected-engine-id'):
        i.add_argument('--' + name, required=True)
    vi = sub.add_parser('verify-image')
    for name in ('archive', 'receipt'):
        vi.add_argument('--' + name, type=Path, required=True)
    for name in ('expected-receipt-sha256', 'build-number', 'library-revision'):
        vi.add_argument('--' + name, required=True)
    c = sub.add_parser('collect')
    c.add_argument('--jenkins-url', required=True)
    c.add_argument('--build-number', required=True)
    c.add_argument('--library-revision', required=True)
    c.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.command == 'build':
        result = build(a.source, a.output, a.sdk_root, json.loads(a.identity.read_bytes()))
        print(json.dumps({'scope': result['scope'], 'result': result['result']}))
    elif a.command == 'image':
        r = image(a.elf_archive, a.elf_receipt, a.expected_elf_receipt_sha256,
                  a.build_number, a.library_revision, a.expected_engine_id, a.output)
        print(json.dumps({'scope': r['scope'], 'image_id': r['image']['id'], 'archive_sha256': r['archive']['sha256']}))
    elif a.command == 'verify-image':
        r = verify_image(a.archive, a.receipt, a.expected_receipt_sha256, a.build_number, a.library_revision)
        print(json.dumps({'artifact_integrity_verified': True, 'image_id': r['image']['id'], 'runtime_activated': False}))
    elif a.command == 'verify':
        print(json.dumps(verify(a.archive, a.receipt, a.expected_receipt_sha256,
                                a.build_number, a.library_revision), sort_keys=True))
    else:
        print(json.dumps(collect(a.jenkins_url, a.build_number, a.library_revision, a.output), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (Blocked, OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise SystemExit(str(error) if isinstance(error, Blocked) else type(error).__name__)
