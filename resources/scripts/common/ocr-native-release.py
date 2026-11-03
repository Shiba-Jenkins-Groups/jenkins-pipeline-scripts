#!/usr/bin/env python3
"""Bounded Apple OCR + owner artifacts; authenticated exact-build collection.

Build never deploys or opens a product database. Verify proves integrity only;
collect separately authenticates completed native and parent Jenkins builds.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

JOB = 'shiba-release-automation/apple-vision-native-release'
ORIGIN = 'http://127.0.0.1:9090'
PARENT_JOB = 'shiba-go-ditch-recognition-project/prod'
LIB_REPO = 'https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts.git'
OCR_REPO = 'https://github.com/ShibaDev2026/shiba-go-ditch-recognition-project.git'
OWNER_REPO = 'https://github.com/ShibaDev2026/shiba-go-ditch-api-project.git'
STAGES = ['Check macOS native builder', 'Checkout exact OCR and owner sources',
          'Build native OCR and owner artifacts', 'Archive immutable native release']
FILES = ['ocr-native-artifact.tar', 'ci-receipt.json', 'SHA256SUMS', 'contracts.log']
PARENT_FILES = ['.pipeline/release-manifest.env', 'reports/runtime-image.json', 'image-ref.txt']
HEX40 = re.compile(r'[0-9a-f]{40}')
HEX64 = re.compile(r'[0-9a-f]{64}')
MAX_BYTES = 192 * 1024 * 1024
STARTUP_LOG_BYTES = 64 * 1024
STARTUP_LOG_FILES = 4


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def regular(path, executable=False):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), 'regular non-symlink file required: ' + str(path))
    require(path.stat().st_size <= MAX_BYTES, 'oversized artifact')
    if executable:
        require(os.access(path, os.X_OK), 'executable required')
    return path


def write_json(path, value):
    with Path(path).open('xb') as handle:
        handle.write((json.dumps(value, indent=2, sort_keys=True) + '\n').encode())
        handle.flush()
        os.fsync(handle.fileno())


def safe_name(name):
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and '..' not in path.parts and str(path) == name and '\\' not in name


def build_path(job, number):
    require(re.fullmatch(r'[1-9][0-9]{0,8}', str(number)), 'invalid build number')
    require(job in [JOB, PARENT_JOB], 'unexpected Jenkins job')
    return '/'.join('job/' + part for part in job.split('/')) + '/' + str(number) + '/'


def validate_parent(parent):
    keys = {'schema', 'status', 'source_commit', 'image', 'job_name', 'build_number', 'build_url', 'artifact_hash_inventory'}
    require(set(parent) == keys and parent['schema'] == 'recognition-parent-ci-v1' and
            parent['status'] == 'SUCCESS' and HEX40.fullmatch(parent['source_commit']), 'invalid parent CI identity')
    require(parent['job_name'] == PARENT_JOB and parent['build_url'] == ORIGIN + '/' + build_path(PARENT_JOB, parent['build_number']),
            'parent CI origin/job/build mismatch')
    require(re.fullmatch(r'[A-Za-z0-9.:_/-]+@sha256:[0-9a-f]{64}', parent['image']), 'immutable parent image required')
    inventory = parent['artifact_hash_inventory']
    require(isinstance(inventory, dict) and set(inventory) == set(PARENT_FILES) and
            all(HEX64.fullmatch(v) for v in inventory.values()), 'parent artifact hash inventory required')
    return parent


def validate_identity(identity):
    require(identity.get('schema_version') == 1 and identity.get('job_name') == JOB, 'invalid native CI identity')
    for key in ['source_commit', 'source_baseline_commit', 'owner_source_commit', 'owner_baseline_commit', 'library_revision']:
        require(HEX40.fullmatch(identity.get(key, '')), 'missing exact ' + key)
    build_path(JOB, identity.get('build_number', ''))
    require(identity.get('build_url', '').replace('http://localhost:9090/', ORIGIN + '/') ==
            ORIGIN + '/' + build_path(JOB, identity['build_number']), 'native build URL mismatch')
    require(re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}', identity.get('requested_by', '')), 'approved manual user required')
    require(identity.get('owner_package') == './cmd/service-control-owner' and
            identity.get('owner_test_run') == '^TestOCRReplacement', 'unexpected owner build/test target')
    require(re.fullmatch(r'go1\.[0-9]+\.[0-9]+', identity.get('go_version', '')), 'exact Go version required')
    require(set(identity.get('scanner_tools', {})) == {'gitleaks', 'govulncheck'}, 'both scanner tools required')
    for name, tool in identity['scanner_tools'].items():
        if name == 'gitleaks' and tool == {'module': 'github.com/zricethezav/gitleaks/v8', 'version': 'v8.30.1'}:
            continue
        require(set(tool) == {'path', 'sha256'} and Path(tool['path']).is_absolute() and
                HEX64.fullmatch(tool['sha256']), 'pinned scanner executable required')
    validate_parent(identity['parent_ci'])
    return identity


def run(command, cwd, env=None, log=None, input_data=None, timeout=600, separate_stderr=False):
    if log:
        log.write(('$ ' + ' '.join(map(str, command)) + '\n').encode())
        log.flush()
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=cwd, env=env, input=input_data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        if log:
            log.write(error.output or b'')
            log.write(error.stderr or b'')
            log.write(('\ncommand timed out after ' + str(error.timeout) +
                       ' seconds; elapsed_seconds=' + format(time.monotonic() - started, '.3f') + '\n').encode())
            log.flush()
        raise
    if log:
        log.write(result.stdout)
        if separate_stderr:
            log.write(result.stderr)
        log.write(('\ncommand returncode=' + str(result.returncode) +
                   ' elapsed_seconds=' + format(time.monotonic() - started, '.3f') + '\n').encode())
        log.flush()
    require(result.returncode == 0, 'command failed: ' + str(command[0]) + ' (returncode=' + str(result.returncode) + ')')
    return result.stdout


def append_startup_logs(directory, log):
    # Only this build's fresh private directory is inspected. Bound diagnostics,
    # follow no links and never let evidence collection hide the startup error.
    try:
        require(not directory.is_symlink() and directory.is_dir(), 'private startup log directory unavailable')
        paths = sorted(directory.iterdir())
        log.write(('\nstartup private logs: ' + str(len(paths)) + ' entries\n').encode())
        for path in paths[:STARTUP_LOG_FILES]:
            log.write(('startup log ' + json.dumps(path.name) + '\n').encode())
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'rb') as handle:
                    require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), 'not a regular startup log')
                    data = handle.read(STARTUP_LOG_BYTES + 1)
                log.write(data[:STARTUP_LOG_BYTES])
                if len(data) > STARTUP_LOG_BYTES:
                    log.write(b'\nstartup log truncated at byte limit\n')
            except (OSError, ValueError) as error:
                log.write(('startup log unavailable: ' + type(error).__name__ + '\n').encode())
        if len(paths) > STARTUP_LOG_FILES:
            log.write(b'\nstartup logs truncated at file limit\n')
    except (OSError, ValueError) as error:
        log.write(('startup logs unavailable: ' + type(error).__name__ + '\n').encode())
    log.flush()


def startup_compatibility(binary, output, env, log):
    smoke_logs = output / 'startup-logs'
    smoke_logs.mkdir(mode=0o700)
    smoke_env = dict(env, OCR_RUNTIME_LANE='prod', OCR_COLD_START_TIMEOUT_SECONDS='120', OCRSVC_LOG_DIR=str(smoke_logs))
    try:
        return run([str(binary), '-check-compatibility'], output, smoke_env, log,
                   timeout=135, separate_stderr=True)
    finally:
        append_startup_logs(smoke_logs, log)


def exact_checkout(source, commit):
    source = Path(source).resolve(strict=True)
    require(run(['git', 'rev-parse', 'HEAD'], source).decode().strip() == commit, 'source HEAD mismatch')
    require(not run(['git', 'status', '--porcelain', '--untracked-files=all'], source).strip(), 'source checkout is dirty')
    return source


def changed_patch(source, baseline):
    require(HEX40.fullmatch(baseline), 'exact secret-scan baseline required')
    run(['git', 'merge-base', '--is-ancestor', baseline, 'HEAD'], source)
    return run(['git', 'diff', '--binary', '--no-ext-diff', baseline + '..HEAD'], source)


def scan_changes(source, baseline, tool, report, work, env, log):
    # Built-in rules, no repository-provided allowlist/config. The complete exact
    # reviewed source delta includes pre-merge commits too. No diff bytes are logged.
    patch = changed_patch(source, baseline)
    scan_env = {k: v for k, v in env.items() if not k.startswith('GITLEAKS_')}
    run([str(tool), 'stdin', '--redact', '--no-banner', '--report-format', 'json',
         '--report-path', str(report)], work, scan_env, log, input_data=patch, timeout=180)
    require(regular(report).is_file() and json.loads(report.read_text()) == [], 'secret scan did not return an empty findings report')


def validate_smoke(smoke, native):
    require(smoke.get('ready') is True and smoke.get('contract_version') == 1 and
            smoke.get('provider_id') == 'apple-vision' and smoke.get('platform_lane') == 'mac' and
            smoke.get('runtime_artifact_sha256') == native.get('runtime_artifact_sha256') and
            smoke.get('runtime_identity') == native.get('runtime_identity') and
            smoke.get('compatibility') == {'status': 'pass', 'policy': 'receipt-smoke-v1'}, 'native compatibility identity mismatch')
    cache = smoke.get('cache_identity', {})
    require(cache.get('verified') is True and cache.get('lane') == 'prod' and
            cache.get('namespace') == native['binary'] and cache.get('binary_sha256') == native['binary_sha256'] and
            smoke.get('initialization', {}).get('phase') == 'ready', 'native cache preparation identity mismatch')


def build(args):
    identity = validate_identity(json.loads(regular(args.identity).read_text()))
    require(platform.system() == 'Darwin' and platform.machine() == 'arm64' and os.getuid() != 0, 'native macOS arm64 nonroot builder required')
    source = exact_checkout(args.recognition_source, identity['source_commit'])
    owner = exact_checkout(args.owner_source, identity['owner_source_commit'])
    output = Path(args.output).absolute()
    require(not output.exists(), 'create-once artifact output already exists')
    output.mkdir(mode=0o700, parents=False)
    reports = output / 'reports'
    reports.mkdir(mode=0o700)
    # Go's built-in official alternate endpoint retains the sum.golang.org
    # verifier key while avoiding the native builder's HiNet DNS redirection.
    env = dict(os.environ, GOTOOLCHAIN=identity['go_version'], GOWORK='off', GOFLAGS='', GOOS='darwin', GOARCH='arm64',
               CGO_ENABLED='0', GOPROXY='http://127.0.0.1:9190/repository/go-group',
               GOSUMDB='sum.golang.org https://sum.golang.google.cn',
               PYTHONDONTWRITEBYTECODE='1')
    tools = {}
    for name, pin in identity['scanner_tools'].items():
        if 'module' in pin:
            continue
        binary = regular(pin['path'], executable=True).resolve(strict=True)
        require(sha(binary.read_bytes()) == pin['sha256'], name + ' scanner hash mismatch')
        tools[name] = binary
    with (output / 'contracts.log').open('xb') as log:
        require(run(['go', 'env', 'GOVERSION'], owner, env, log, separate_stderr=True).decode().strip() == identity['go_version'], 'Go toolchain version mismatch')
        if 'gitleaks' not in tools:
            # Versioned task-local scanner, Go module/sumdb verification and the
            # existing Nexus proxy only. No global install or fallback proxy.
            tool_dir = output / 'scanner-tools'
            tool_dir.mkdir(mode=0o700)
            tool_env = dict(env, GOBIN=str(tool_dir))
            pin = identity['scanner_tools']['gitleaks']
            run(['go', 'install', pin['module'] + '@' + pin['version']], output, tool_env, log)
            tools['gitleaks'] = regular(tool_dir / 'gitleaks', executable=True)
        for name, command in [('gitleaks', ['version']), ('govulncheck', ['-version'])]:
            run([str(tools[name]), *command], output, env, log, timeout=30)
        scan_changes(source, identity['source_baseline_commit'], tools['gitleaks'], reports / 'ocr-gitleaks.json', output, env, log)
        scan_changes(owner, identity['owner_baseline_commit'], tools['gitleaks'], reports / 'owner-gitleaks.json', output, env, log)
        run(['python3', '-B', 'tests/apple_vision_preparation_process_test.py'], source, env, log, timeout=30)
        run(['python3', '-B', 'tests/apple_vision_preparation_receipt.py'], source, env, log, timeout=300)
        run(['go', 'test', '-mod=readonly', './internal/hostcontrol', '-run', identity['owner_test_run'], '-count=1'], owner, env, log)
        run(['go', 'vet', '-mod=readonly', identity['owner_package'], './internal/hostcontrol'], owner, env, log)
        run(['go', 'test', '-mod=readonly', './cmd/verify/ocr_release_manifest', '-count=1'], source, env, log)
        run(['go', 'vet', '-mod=readonly', './cmd/verify/ocr_release_manifest'], source, env, log)
        scan_summaries = {}
        for label, directory, package in [('owner', owner, identity['owner_package']),
                                          ('manifest-tool', source, './cmd/verify/ocr_release_manifest')]:
            vuln = run([str(tools['govulncheck']), '-json', package], directory, env, log, separate_stderr=True)
            (reports / (label + '-govulncheck.jsonl')).write_bytes(vuln)
            # A zero process status alone is not a complete scanner receipt.
            decoder, remaining, messages = json.JSONDecoder(), vuln.decode().strip(), []
            while remaining:
                value, end = decoder.raw_decode(remaining)
                messages.append(value)
                remaining = remaining[end:].lstrip()
            configs = [m['config'] for m in messages if 'config' in m]
            require(len(configs) == 1 and not any(m.get('finding') for m in messages), 'incomplete or affected govulncheck result')
            scan_summaries[label] = {'status': 'PASS', 'package': package, 'config': configs[0]}
        parent = output / 'parent-ci.json'
        write_json(parent, identity['parent_ci'])
        native = output / 'native'
        run(['python3', '-B', 'scripts/ocr_native_artifact.py', 'build', '--source-commit', identity['source_commit'],
             '--parent-ci-receipt', str(parent), '--parent-ci-receipt-sha256', sha(parent.read_bytes()),
             '--output-dir', str(native), '--lane', 'prod'], source, env, log)
        run(['python3', '-B', 'scripts/ocr_native_artifact.py', 'verify', '--artifact-dir', str(native),
             '--source-commit', identity['source_commit'], '--parent-ci-receipt', str(parent),
             '--parent-ci-receipt-sha256', sha(parent.read_bytes())], source, env, log)
        native_receipt = json.loads(regular(native / 'artifact.json').read_text())
        native_binary_name = native_receipt.get('binary', '')
        require(re.fullmatch(r'shiba_ocr_prod_[0-9a-f]{12}', native_binary_name), 'unexpected native binary name')
        smoke = startup_compatibility(native / native_binary_name, output, env, log)
        smoke_payload = json.loads(smoke)
        validate_smoke(smoke_payload, native_receipt)
        (reports / 'startup-smoke.json').write_bytes(smoke)
        owner_output = output / 'owner'
        owner_output.mkdir(mode=0o700)
        binary = owner_output / 'service-control-owner'
        run(['go', 'build', '-mod=readonly', '-trimpath', '-buildvcs=true', '-o', str(binary), identity['owner_package']], owner, env, log)
        build_info = run(['go', 'version', '-m', str(binary)], owner, env, log).decode()
        require('vcs.revision=' + identity['owner_source_commit'] in build_info and 'vcs.modified=false' in build_info,
                'owner binary is not bound to the clean source revision')
        write_json(owner_output / 'artifact.json', {'schema': 'service-control-owner-artifact-v1',
                   'source_commit': identity['owner_source_commit'], 'binary': binary.name,
                   'binary_sha256': sha(binary.read_bytes()), 'go_version': identity['go_version'],
                   'platform': 'darwin/arm64', 'package': identity['owner_package'], 'build_info': build_info})
        manifest_output = output / 'manifest-tool'
        manifest_output.mkdir(mode=0o700)
        manifest_binary = manifest_output / 'ocr-release-manifest'
        run(['go', 'build', '-mod=readonly', '-trimpath', '-buildvcs=true', '-o', str(manifest_binary),
             './cmd/verify/ocr_release_manifest'], source, env, log)
        manifest_info = run(['go', 'version', '-m', str(manifest_binary)], source, env, log).decode()
        require('vcs.revision=' + identity['source_commit'] in manifest_info and 'vcs.modified=false' in manifest_info,
                'manifest tool binary source revision mismatch')
        write_json(manifest_output / 'artifact.json', {'schema': 'ocr-release-manifest-tool-v1',
                   'source_commit': identity['source_commit'], 'binary': manifest_binary.name,
                   'binary_sha256': sha(manifest_binary.read_bytes()), 'go_version': identity['go_version'],
                   'platform': 'darwin/arm64', 'build_info': manifest_info})
        write_json(reports / 'scan-summary.json', {'schema': 'ocr-native-scan-summary-v1', 'status': 'PASS',
                   'gitleaks': {'status': 'PASS', 'scope': 'complete reviewed baseline-to-source deltas',
                                'native_baseline_commit': identity['source_baseline_commit'],
                                'owner_baseline_commit': identity['owner_baseline_commit'],
                                'binary_sha256': sha(tools['gitleaks'].read_bytes()),
                                'configuration': 'built-in rules, no repository allowlist'},
                   'govulncheck': {'status': 'PASS', 'binary_sha256': sha(tools['govulncheck'].read_bytes()),
                                  'results': scan_summaries}})
        exact_checkout(source, identity['source_commit'])
        exact_checkout(owner, identity['owner_source_commit'])
    native_receipt = json.loads(regular(native / 'artifact.json').read_text())
    native_binary = native_receipt.get('binary', '')
    require(re.fullmatch(r'shiba_ocr_prod_[0-9a-f]{12}', native_binary), 'unexpected native binary name')
    native_files = {'artifact.json', 'parent-ci.json', 'cache-info.json', native_binary}
    require(set(p.name for p in native.iterdir()) == native_files, 'unexpected native artifact files')
    entries = {}
    for directory in [native, owner_output, manifest_output, reports]:
        for path in sorted(directory.iterdir()):
            regular(path)
            entries[path.relative_to(output).as_posix()] = path.read_bytes()
    archive = output / FILES[0]
    with tarfile.open(archive, 'w', format=tarfile.USTAR_FORMAT) as tar:
        for name, data in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.uid, info.gid = len(data), 0, 0, 0
            info.mode = 0o755 if name in ['native/' + native_binary, 'owner/service-control-owner',
                                       'manifest-tool/ocr-release-manifest'] else 0o600
            tar.addfile(info, io.BytesIO(data))
    receipt = {'schema': 'apple-ocr-native-ci-v1', 'result': 'PASS', 'identity': identity,
               'archive_sha256': sha(archive.read_bytes()), 'archive_size': archive.stat().st_size,
               'files': {name: {'sha256': sha(data), 'size': len(data)} for name, data in entries.items()},
               'native_artifact_sha256': sha(entries['native/artifact.json']),
               'owner_artifact_sha256': sha(entries['owner/artifact.json']),
               'contracts_log_sha256': sha((output / 'contracts.log').read_bytes()),
               'runtime_deployed': False, 'pipeline_final_success': False}
    write_json(output / 'ci-receipt.json', receipt)
    (output / 'SHA256SUMS').write_text(''.join(sha((output / name).read_bytes()) + '  ' + name + '\n' for name in FILES if name != 'SHA256SUMS'))
    return receipt


def verify(directory, source_commit=None, owner_commit=None, library_revision=None):
    directory = Path(directory)
    receipt = json.loads(regular(directory / 'ci-receipt.json').read_text())
    require(receipt.get('schema') == 'apple-ocr-native-ci-v1' and receipt.get('result') == 'PASS' and
            receipt.get('runtime_deployed') is False and receipt.get('pipeline_final_success') is False, 'invalid artifact receipt')
    identity = validate_identity(receipt['identity'])
    for key, pin in [('source_commit', source_commit), ('owner_source_commit', owner_commit), ('library_revision', library_revision)]:
        if pin is not None:
            require(identity[key] == pin, key + ' mismatch')
    archive = regular(directory / FILES[0])
    require(sha(archive.read_bytes()) == receipt['archive_sha256'] and archive.stat().st_size == receipt['archive_size'], 'archive identity mismatch')
    require(sha(regular(directory / 'contracts.log').read_bytes()) == receipt['contracts_log_sha256'], 'contracts log mismatch')
    entries = {}
    with tarfile.open(archive, 'r:') as tar:
        for item in tar:
            require(safe_name(item.name) and item.isfile() and item.name not in entries and item.size <= MAX_BYTES,
                    'unsafe or duplicate archive member')
            entries[item.name] = tar.extractfile(item).read()
    require(set(entries) == set(receipt['files']), 'archive file inventory mismatch')
    for name, data in entries.items():
        require(receipt['files'][name] == {'sha256': sha(data), 'size': len(data)}, 'artifact member hash mismatch')
    native = json.loads(entries['native/artifact.json'])
    owner = json.loads(entries['owner/artifact.json'])
    require(sha(entries['native/artifact.json']) == receipt['native_artifact_sha256'] and
            sha(entries['owner/artifact.json']) == receipt['owner_artifact_sha256'], 'component receipt hash mismatch')
    require(native.get('revision') == identity['source_commit'] and native.get('lane') == 'prod' and
            re.fullmatch(r'shiba_ocr_prod_[0-9a-f]{12}', native.get('binary', '')) and
            sha(entries['native/' + native['binary']]) == native['binary_sha256'], 'native component mismatch')
    require(owner.get('schema') == 'service-control-owner-artifact-v1' and owner.get('source_commit') == identity['owner_source_commit'] and
            owner.get('binary') == 'service-control-owner' and owner.get('platform') == 'darwin/arm64' and
            sha(entries['owner/service-control-owner']) == owner['binary_sha256'], 'owner component mismatch')
    require(json.loads(entries['native/parent-ci.json']) == identity['parent_ci'], 'native parent identity mismatch')
    manifest_tool = json.loads(entries['manifest-tool/artifact.json'])
    require(manifest_tool.get('source_commit') == identity['source_commit'] and manifest_tool.get('platform') == 'darwin/arm64' and
            sha(entries['manifest-tool/ocr-release-manifest']) == manifest_tool.get('binary_sha256'), 'manifest tool mismatch')
    smoke = json.loads(entries['reports/startup-smoke.json'])
    validate_smoke(smoke, native)
    summary = json.loads(entries['reports/scan-summary.json'])
    require(summary.get('status') == 'PASS' and summary.get('gitleaks', {}).get('status') == 'PASS' and
            summary.get('govulncheck', {}).get('status') == 'PASS', 'scanner summary missing')
    for report in ['reports/ocr-gitleaks.json', 'reports/owner-gitleaks.json']:
        require(json.loads(entries[report]) == [], 'nonempty secret scan')
    expected_sums = ''.join(sha(regular(directory / name).read_bytes()) + '  ' + name + '\n' for name in FILES if name != 'SHA256SUMS')
    require(regular(directory / 'SHA256SUMS').read_text() == expected_sums, 'SHA256SUMS mismatch')
    return receipt


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Jenkins redirect rejected')


def authenticated_get(path):
    user, token = os.environ.get('JENKINS_API_USER', ''), os.environ.get('JENKINS_API_TOKEN', '')
    require(user and token, 'existing Jenkins API credentials required in memory')
    require(path.startswith('job/') and '..' not in path, 'unexpected Jenkins read path')
    request = urllib.request.Request(ORIGIN + '/' + path,
        headers={'Authorization': 'Basic ' + base64.b64encode((user + ':' + token).encode()).decode()})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        data = response.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, 'oversized Jenkins response')
    return data


def api_path(job, build):
    tree = 'number,result,building,actions[causes[_class,userId],remoteUrls,lastBuiltRevision[SHA1],libraries[name,version,trusted]],artifacts[relativePath]'
    return build_path(job, build) + 'api/json?' + urllib.parse.urlencode({'tree': tree})


def checkout_revisions(api):
    revisions = {}
    for action in api.get('actions', []):
        commit = action.get('lastBuiltRevision', {}).get('SHA1')
        for repo in action.get('remoteUrls', []):
            if commit:
                revisions.setdefault(repo, set()).add(commit)
    return revisions


def validate_build_api(api, identity):
    require(str(api.get('number')) == str(identity['build_number']) and api.get('result') == 'SUCCESS' and api.get('building') is False,
            'native Pipeline has not completed SUCCESS')
    causes = [c for a in api.get('actions', []) for c in a.get('causes', [])]
    require(len(causes) == 1 and causes[0].get('_class') == 'hudson.model.Cause$UserIdCause' and
            causes[0].get('userId') == identity['requested_by'], 'native manual cause mismatch')
    revisions = checkout_revisions(api)
    for repo, commit in [(OCR_REPO, identity['source_commit']), (OWNER_REPO, identity['owner_source_commit']), (LIB_REPO, identity['library_revision'])]:
        require(revisions.get(repo) == {commit}, 'Jenkins checkout proof mismatch: ' + repo)
    libraries = [lib for a in api.get('actions', []) for lib in a.get('libraries', [])]
    require(len(libraries) == 1 and libraries[0].get('name') == 'jenkins-pipeline' and
            libraries[0].get('version') == identity['library_revision'] and libraries[0].get('trusted') is True,
            'trusted pinned Library proof missing')
    require({a['relativePath'] for a in api.get('artifacts', [])} == {'artifact/' + f for f in FILES}, 'native archive whitelist mismatch')


def collect(args):
    target = Path(args.output).absolute()
    require(not target.exists(), 'create-once collection output already exists')
    api_raw = authenticated_get(api_path(JOB, args.build))
    api = json.loads(api_raw)
    require(api.get('result') == 'SUCCESS' and api.get('building') is False, 'native build not successful')
    base = build_path(JOB, args.build)
    data = {name: authenticated_get(base + 'artifact/artifact/' + name) for name in FILES}
    with tempfile.TemporaryDirectory(prefix='ocr-native-readback-') as temporary:
        directory = Path(temporary)
        for name, raw in data.items():
            (directory / name).write_bytes(raw)
        receipt = verify(directory, args.source_commit, args.owner_source_commit, args.library_revision)
    identity = receipt['identity']
    validate_build_api(api, identity)
    require(identity['requested_by'] == args.requested_by, 'unexpected release approver')
    workflow_raw = authenticated_get(base + 'wfapi/describe')
    workflow = json.loads(workflow_raw)
    stages = workflow.get('stages', [])
    require(workflow.get('status') == 'SUCCESS' and len(stages) == len(STAGES) and
            [s.get('name') for s in stages] == STAGES and all(s.get('status') == 'SUCCESS' for s in stages), 'native workflow stages incomplete')
    parent = identity['parent_ci']
    parent_api_raw = authenticated_get(api_path(PARENT_JOB, parent['build_number']))
    parent_api = json.loads(parent_api_raw)
    require(parent_api.get('result') == 'SUCCESS' and parent_api.get('building') is False and
            str(parent_api.get('number')) == str(parent['build_number']) and
            checkout_revisions(parent_api).get(OCR_REPO) == {parent['source_commit']}, 'parent Pipeline identity mismatch')
    parent_artifacts = {}
    for name, digest in parent['artifact_hash_inventory'].items():
        raw = authenticated_get(build_path(PARENT_JOB, parent['build_number']) + 'artifact/' + name)
        require(sha(raw) == digest, 'parent artifact hash mismatch: ' + name)
        parent_artifacts[name] = raw
    runtime = json.loads(parent_artifacts['reports/runtime-image.json'])
    require(runtime.get('commit') == parent['source_commit'] and runtime.get('image') == parent['image'] and
            all(runtime.get(k) == 'PASS' for k in ['health', 'shutdown', 'cleanup']), 'parent image receipt mismatch')
    target.mkdir(mode=0o700, parents=False)
    for name, raw in data.items():
        (target / name).write_bytes(raw)
    (target / 'jenkins-build-api.json').write_bytes(api_raw)
    (target / 'jenkins-workflow.json').write_bytes(workflow_raw)
    (target / 'parent-build-api.json').write_bytes(parent_api_raw)
    evidence = {'schema': 'ocr-native-ci-v1', 'status': 'SUCCESS', 'pipeline_final_success': True,
                'identity': identity, 'ci_receipt_sha256': sha(data['ci-receipt.json']),
                **{key: identity[key] for key in ['job_name', 'build_number', 'build_url', 'source_commit', 'owner_source_commit', 'library_revision']},
                'build_number': int(identity['build_number']),
                'parent_ci': identity['parent_ci'],
                'artifact_hash_inventory': {name: item['sha256'] for name, item in receipt['files'].items()},
                'jenkins_artifact_hash_inventory': {'artifact/' + name: sha(raw) for name, raw in data.items()},
                'archive_sha256': receipt['archive_sha256'], 'archive_size': receipt['archive_size'],
                'native_artifact_sha256': receipt['native_artifact_sha256'], 'owner_artifact_sha256': receipt['owner_artifact_sha256'],
                'api_sha256': sha(api_raw), 'workflow_sha256': sha(workflow_raw), 'parent_api_sha256': sha(parent_api_raw),
                'parent_artifact_hash_inventory': parent['artifact_hash_inventory'], 'runtime_deployed': False}
    write_json(target / 'trusted-build-evidence.json', evidence)
    for path in target.iterdir():
        path.chmod(0o600)
        with path.open('rb') as handle:
            os.fsync(handle.fileno())
    fd = os.open(target, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return evidence


def render(args):
    identity = validate_identity(json.loads(regular(args.identity).read_text()))
    config = dict(enabled=args.enable, libraryRevision=identity['library_revision'], sourceCommit=identity['source_commit'],
                  sourceBaselineCommit=identity['source_baseline_commit'],
                  ownerSourceCommit=identity['owner_source_commit'], ownerBaselineCommit=identity['owner_baseline_commit'],
                  ownerPackage=identity['owner_package'],
                  ownerTestRun=identity['owner_test_run'], goVersion=identity['go_version'], approvers=[identity['requested_by']],
                  parentCI=identity['parent_ci'], scannerTools=identity['scanner_tools'])
    # Only the trusted Library decodes/parses. Sandboxed WorkflowScript contains
    # no constructors, static helpers or ScriptApproval-dependent JSON code.
    encoded = base64.b64encode(json.dumps(config, separators=(',', ':')).encode()).decode()
    script = "@Library('jenkins-pipeline@" + identity['library_revision'] + "') _\n" + \
             "ocrNativeReleasePipeline('" + encoded + "')\n"
    root = ET.Element('flow-definition')
    ET.SubElement(root, 'description').text = 'Pinned macOS OCR and owner artifacts only. Manual approved user; no runtime deployment or automatic downstream.'
    ET.SubElement(root, 'properties')
    definition = ET.SubElement(root, 'definition', {'class': 'org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition', 'plugin': 'workflow-cps'})
    ET.SubElement(definition, 'script').text = script
    ET.SubElement(definition, 'sandbox').text = 'true'
    ET.SubElement(root, 'triggers')
    ET.SubElement(root, 'disabled').text = 'false' if args.enable else 'true'
    return ET.tostring(root, encoding='unicode') + '\n'


def render_deployment(args):
    config = json.loads(regular(args.config).read_text())
    keys = {'libraryRevision', 'sourceCommit', 'approvers', 'plan', 'planSHA256', 'ciReceipt', 'ciReceiptSHA256', 'artifactDirectory'}
    require(set(config) == keys and all(HEX40.fullmatch(config[k]) for k in ['libraryRevision', 'sourceCommit']) and
            all(HEX64.fullmatch(config[k]) for k in ['planSHA256', 'ciReceiptSHA256']), 'exact reviewed deployment pins required')
    require(isinstance(config['approvers'], list) and config['approvers'] and
            all(re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}', v) for v in config['approvers']), 'approved deployment user required')
    for key in ['plan', 'ciReceipt', 'artifactDirectory']:
        value = config[key]
        require(isinstance(value, str) and value.startswith('/Users/surpend/') and '..' not in value and
                not any(ord(c) < 32 or ord(c) == 127 for c in value), 'fixed private deployment path required')
    config['enabled'] = args.enable
    encoded = base64.b64encode(json.dumps(config, separators=(',', ':')).encode()).decode()
    script = "@Library('jenkins-pipeline@" + config['libraryRevision'] + "') _\n" + \
             "ocrNativeDeploymentPipeline('" + encoded + "')\n"
    root = ET.Element('flow-definition')
    ET.SubElement(root, 'description').text = 'One reviewed native OCR owner transaction. Exact plan/CI/source pins, approved manual user, no parameters or automatic retry.'
    ET.SubElement(root, 'properties')
    definition = ET.SubElement(root, 'definition', {'class': 'org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition', 'plugin': 'workflow-cps'})
    ET.SubElement(definition, 'script').text = script
    ET.SubElement(definition, 'sandbox').text = 'true'
    ET.SubElement(root, 'triggers')
    ET.SubElement(root, 'disabled').text = 'false' if args.enable else 'true'
    return ET.tostring(root, encoding='unicode') + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('build')
    for name in ['recognition-source', 'owner-source', 'identity', 'output']:
        p.add_argument('--' + name, required=True)
    p = commands.add_parser('verify')
    p.add_argument('--artifact-dir', required=True)
    for name in ['source-commit', 'owner-source-commit', 'library-revision']:
        p.add_argument('--' + name, required=True)
    p = commands.add_parser('collect')
    for name in ['build', 'source-commit', 'owner-source-commit', 'library-revision', 'requested-by', 'output']:
        p.add_argument('--' + name, required=True)
    p = commands.add_parser('render-job')
    p.add_argument('--identity', required=True)
    p.add_argument('--enable', action='store_true')
    p = commands.add_parser('render-deploy-job')
    p.add_argument('--config', required=True)
    p.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'build':
            result = build(args)
        elif args.command == 'verify':
            result = verify(args.artifact_dir, args.source_commit, args.owner_source_commit, args.library_revision)
        elif args.command == 'collect':
            result = collect(args)
        elif args.command == 'render-job':
            print(render(args), end='')
            return
        else:
            print(render_deployment(args), end='')
            return
        print(json.dumps(result, indent=2, sort_keys=True))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, 'native OCR release refused: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
