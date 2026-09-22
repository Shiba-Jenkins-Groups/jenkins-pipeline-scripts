#!/usr/bin/env python3
"""Verify the exact pushed Shiba image with isolated Docker-owned storage.

No host mounts, runtime ports, production networks, credentials or Kubernetes.
Both processes use the image's nonroot user. Only the App needs an empty DB.
"""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import uuid

APP = 'shiba-go-ditch-api-project'
RECOGNITION = 'shiba-go-ditch-recognition-project'


def docker(*args, timeout=120, check=True):
    result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'docker {args[0]} failed: {result.stderr.strip()}')
    return result


def identity(root):
    values = {}
    for line in (root / 'image-ref.txt').read_text().splitlines():
        key, value = line.split('=', 1)
        if key in values:
            raise ValueError('duplicate image identity')
        values[key] = value
    product, branch, number = values['APP_NAME'], values['BRANCH'], values['BUILD_NUMBER']
    if product not in {APP, RECOGNITION} or branch != 'prod' or not re.fullmatch(r'[1-9][0-9]*', number):
        raise ValueError('verification requires a Shiba PROD build')
    if os.environ['JOB_NAME'] != product + '/prod' or os.environ['BUILD_NUMBER'] != number:
        raise ValueError('verification build mismatch')
    version, digest, commit = values['APP_VERSION'], values['IMAGE_DIGEST'], os.environ['GIT_COMMIT']
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('invalid version, digest or commit')
    repository = f'localhost:9290/{product}/prod/{version}'
    if values['IMAGE_REF'] != repository + ':' + number:
        raise ValueError('unexpected image repository')
    immutable = repository + '@' + digest
    images = json.loads(docker('image', 'inspect', immutable).stdout)
    if len(images) != 1 or immutable not in images[0].get('RepoDigests', []):
        raise ValueError('image digest mismatch')
    config = images[0]['Config']
    expected = {'app.name': product, 'app.version': version, 'app.branch': branch,
                'app.build': number, 'org.opencontainers.image.revision': commit}
    if product == RECOGNITION:
        expected = {'org.opencontainers.image.title': product,
                    'org.opencontainers.image.version': version + '-' + number,
                    'org.opencontainers.image.ref.name': branch,
                    'org.opencontainers.image.revision': number}
    if any(config.get('Labels', {}).get(k) != v for k, v in expected.items()):
        raise ValueError('image labels do not match build/source')
    if config.get('User') not in {'65532', '65532:65532', 'nonroot', 'nonroot:nonroot'}:
        raise ValueError('image must run as nonroot')
    return product, immutable, commit


def verify(root):
    product, image, commit = identity(root)
    token = 'shiba-ci-' + uuid.uuid4().hex
    name, bootstrap, volume = token, token + '-bootstrap', token + '-data'
    containers, created_volume = [], False
    labels = ['--label', 'shiba.ci.verification=' + token]
    isolation = ['--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                 '--user', '65532:65532', '--pids-limit', '256']
    mount = []
    report = {'schema_version': 1, 'product': product, 'commit': commit,
              'image': image, 'profile': 'compose-v2', 'bootstrap_empty': product == APP}
    try:
        if product == APP:
            # Docker populates the new volume from image-owned /app/data (UID 65532).
            # The DB and all sidecars remain exclusively inside this Docker VM.
            docker('volume', 'create', *labels, volume)
            created_volume = True
            mount = ['--mount', f'type=volume,src={volume},dst=/app/data']
            containers.append(bootstrap)
            docker('run', '--name', bootstrap, *labels, *isolation, *mount,
                   '--entrypoint', '/app/app', image, 'db-migrate', 'bootstrap-empty',
                   '--db', '/app/data/db/app/shiba-go-ditch-api.db',
                   '--backup', '/app/data/db/app/pre-migration.db', '--app-commit', commit)
            environment = {'APP_ENV': 'prod', 'EXTERNAL_ORIGIN': 'http://ci.invalid',
                           'WEB_CONTEXT_SECRET': secrets.token_hex(32), 'STORAGE_MODE': 'blob',
                           'RECOGNITION_URL': 'http://127.0.0.1:1', 'RECOGNITION_SELECTION': 'server-active'}
            health = ['/app/app', 'healthcheck', '-url', 'http://127.0.0.1:8090/api/heartbeat', '-timeout', '3s']
        else:
            environment = {'RECOGNITION_BIND': '0.0.0.0:8097', 'RECOGNITION_PROFILE_ID': 'ci-smoke',
                           'RECOGNITION_RULESET_SHA': 'ci-smoke', 'RECOGNITION_ALLOW_EVALUATION': 'false',
                           'OCR_SERVICE_URL': 'http://127.0.0.1:1', 'OCR_EXPECTED_PROVIDER': 'apple-vision',
                           'OCR_TIMEOUT_SECONDS': '1', 'MODEL_ADAPTER': 'ollama',
                           'MODEL_URL': 'http://127.0.0.1:1', 'MODEL_ID': 'ci-smoke'}
            health = ['/usr/local/bin/receipt-recognition', 'healthcheck', '-url', 'http://127.0.0.1:8097/healthz', '-timeout', '3s']
        envargs = [value for k, v in environment.items() for value in ['-e', f'{k}={v}']]
        containers.append(name)
        docker('run', '-d', '--name', name, *labels, *isolation, *mount, *envargs, image)
        deadline = time.monotonic() + 60
        while True:
            state = json.loads(docker('inspect', '--format', '{{json .State}}', name).stdout)
            if not state['Running']:
                raise RuntimeError('image exited before becoming healthy')
            if docker('exec', name, *health, timeout=10, check=False).returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('image healthcheck timed out')
            time.sleep(2)
        # Exercise graceful shutdown as well as startup; SIGKILL/137 must fail.
        docker('stop', '--time', '65', name, timeout=75)
        state = json.loads(docker('inspect', '--format', '{{json .State}}', name).stdout)
        if state['Running'] or state['ExitCode'] != 0 or state.get('OOMKilled'):
            raise RuntimeError('image did not shut down cleanly')
        report.update(health='PASS', shutdown='PASS')
    except Exception:
        for container in containers:
            logs = docker('logs', '--tail', '60', container, check=False)
            print(logs.stdout + logs.stderr)
        raise
    finally:
        errors = []
        for container in reversed(containers):
            found = docker('inspect', '--format', '{{json .Config.Labels}}', container, check=False)
            if found.returncode == 0:
                if json.loads(found.stdout).get('shiba.ci.verification') != token:
                    errors.append('container ownership mismatch')
                elif docker('rm', '-f', container, check=False).returncode:
                    errors.append('container cleanup failed')
        if created_volume and docker('volume', 'rm', volume, check=False).returncode:
            errors.append('volume cleanup failed')
        if errors:
            raise RuntimeError('; '.join(errors))
    report['cleanup'] = 'PASS'
    target = root / 'reports/runtime-image.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + '\n')
    print(f'[runtime-image] PASS {product}: startup, health, graceful shutdown, cleanup')
    return report


if __name__ == '__main__':
    verify(Path(os.environ.get('WORKSPACE', '.')).resolve())
