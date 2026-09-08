#!/usr/bin/env python3
"""Finalize an already revalidated candidate, then verify the published bytes."""
import argparse
import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import urllib.request

spec = importlib.util.spec_from_file_location('finalization_control', Path(__file__).with_name('release-promotion.py'))
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
gate, require = control.gate, control.require


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def published_hash(url):
    token = base64.b64encode((os.environ['NEXUS_CRED_USR'] + ':' + os.environ['NEXUS_CRED_PSW']).encode()).decode()
    request = urllib.request.Request(url, headers={'Authorization': 'Basic ' + token})
    digest = hashlib.sha256()
    with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
        while block := response.read(1024 * 1024): digest.update(block)
    return digest.hexdigest()


def finalize(evidence, policy, root, source, state, key, now, approval=None, approval_key=None, output=None):
    decision = gate.evaluate(evidence, policy, root, now, approval, approval_key)
    require(evidence.get('mode') == 'controlled-candidate-v1' and evidence['gate'] == 'deployment', 'not a revalidated PROD candidate')
    artifact = gate.verified_bytes(root, evidence['artifact'])
    require(evidence['artifact_name'] == f"{gate.PRODUCT}-prod-{evidence['version']}", 'unexpected release artifact name')
    require(control.git(source, 'remote', 'get-url', 'origin') == control.REMOTE, 'finalization remote mismatch')
    require(control.git(source, 'rev-parse', 'HEAD') == evidence['commit'] and not control.git(source, 'status', '--porcelain'), 'finalization checkout mismatch')
    # Bind the archived bytes to the image that passed the digest scans.
    image_result = subprocess.run(['docker', 'image', 'inspect', evidence['immutable_image']], capture_output=True, text=True, timeout=30)
    require(image_result.returncode == 0, 'candidate image unavailable')
    images = json.loads(image_result.stdout)
    require(len(images) == 1 and evidence['immutable_image'] in images[0]['RepoDigests'], 'candidate image digest mismatch')
    labels = images[0]['Config']['Labels']
    require(labels.get('app.artifact.sha256') == evidence['artifact']['sha256']
            and labels.get('org.opencontainers.image.revision') == evidence['commit'] and labels.get('app.name') == gate.PRODUCT
            and labels.get('app.version') == evidence['version'] and labels.get('app.branch') == 'prod', 'candidate binary/image mismatch')
    base = os.environ['NEXUS_BASE_URL'].rstrip('/')
    require(re.fullmatch(r'https?://[A-Za-z0-9.-]+(?::[0-9]+)?', base), 'invalid trusted Nexus endpoint')
    expected_url = f"{base}/repository/raw-artifacts/{gate.PRODUCT}/prod/{evidence['version']}-{evidence['build']}-{evidence['commit'][:7]}/{evidence['artifact_name']}"
    with state.lock():
        require(not state.exists(evidence['commit']), 'finalization already claimed; reconcile, never blindly retry')
        require(control.heads(source)[1] == evidence['commit'], 'prod advanced before finalization')
        require(not control.git(source, 'ls-remote', '--tags', 'origin', 'refs/tags/v' + evidence['version'],
                                'refs/tags/v' + evidence['version'] + '^{}'), 'immutable release tag already exists')
        pipeline = source / '.pipeline'
        pipeline.mkdir(exist_ok=True)
        target = pipeline / evidence['artifact_name']
        with target.open('xb') as out: out.write(artifact)
        metadata = {'APP_NAME': gate.PRODUCT, 'APP_VERSION': evidence['version'], 'BASE_VERSION': evidence['version'],
                    'BRANCH': 'prod', 'BUILD_NUMBER': str(evidence['build']), 'ARTIFACT_LOCAL': str(target)}
        with (pipeline / 'build.env').open('x') as out:
            out.write(''.join(f'{k}={shlex.quote(v)}\n' for k, v in metadata.items()))
        record = {'schema_version': 1, 'kind': 'finalization', 'product': gate.PRODUCT, 'status': 'FINALIZING',
                  'commit': evidence['commit'], 'digest': evidence['image_digest'], 'version': evidence['version'],
                  'evidence_sha256': hashlib.sha256(gate.canonical(evidence)).hexdigest(),
                  'artifact_sha256': evidence['artifact']['sha256'], 'decision': decision, 'started_at': now.isoformat()}
        state.write(evidence['commit'], control.sign(record, key))
        try:
            env = dict(os.environ, WORKSPACE=str(source), GIT_COMMIT=evidence['commit'], DO_PROD_DEPLOY='true',
                       DO_ARTIFACT_PUBLISH='true', DO_GIT_TAG='true', BUILT_IMAGE_REF=evidence['image_ref'],
                       BUILT_IMAGE_DIGEST=evidence['image_digest'], NEXUS_RAW_REPO='raw-artifacts',
                       GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.hooksPath', GIT_CONFIG_VALUE_0='/dev/null')
            result = subprocess.run(['bash', str(Path(__file__).with_name('release-finalize.sh'))], cwd=source, env=env, timeout=900)
            require(result.returncode == 0, 'finalization failed; no retry or deployment')
            manifest = dict(line.split('=', 1) for line in (pipeline / 'release-manifest.env').read_text().splitlines())
            require(manifest == {'RELEASE_TAG': 'v' + evidence['version'], 'GIT_COMMIT': evidence['commit'],
                'IMAGE_REF': evidence['image_ref'], 'IMAGE_DIGEST': evidence['image_digest'], 'NEXUS_ARTIFACT_URL': expected_url}, 'finalization manifest mismatch')
            tags = control.git(source, 'ls-remote', '--tags', 'origin', 'refs/tags/v' + evidence['version'] + '^{}')
            require(tags.split() == [evidence['commit'], 'refs/tags/v' + evidence['version'] + '^{}'], 'published tag readback mismatch')
            require(published_hash(expected_url) == evidence['artifact']['sha256'], 'published artifact readback mismatch')
            record.update(status='SUCCESS', manifest=manifest)
        except BaseException:
            record.update(status='FAILED', external_writes_may_have_started=True)
            raise
        finally:
            record['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            receipt = control.sign(record, key)
            state.write(evidence['commit'], receipt)
            if output is not None:
                with output.open('xb') as out: out.write(gate.canonical(receipt))
        return control.sign(record, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['evidence', 'policy', 'source', 'state-directory', 'receipt-key-file', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--approval-key-file', type=Path)
    args = parser.parse_args()
    try:
        result = finalize(json.loads(args.evidence.read_bytes()), json.loads(args.policy.read_bytes()), args.evidence.parent,
                          args.source.resolve(), control.State(args.state_directory), args.receipt_key_file.read_bytes(),
                          dt.datetime.now(dt.timezone.utc), json.loads(args.approval.read_bytes()) if args.approval else None,
                          args.approval_key_file.read_bytes() if args.approval_key_file else None, args.output)
    except Exception as exc:
        print('BLOCKED: ' + (str(exc) if isinstance(exc, gate.InvalidEvidence) else 'finalization failed'), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
