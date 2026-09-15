#!/usr/bin/env python3
"""Explicit, fresh-evidence-only recovery of a finalized failed deployment.

Invoked only by the fixed coordinator. Never builds, promotes, publishes,
changes release state, extends evidence lifetime, or invokes a runtime itself.
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


control = module('recovery_control', 'release-promotion.py')
adapter = module('recovery_evidence', 'release-evidence.py')
finalizer = module('recovery_finalizer', 'release-finalization.py')
gate, require = control.gate, control.require
COORDINATOR = 'shiba-release-automation/' + gate.PRODUCT + '-auto-release'
OWNER = 'shiba-release-automation/' + gate.PRODUCT + '-prod-deploy'


def sha(value):
    return hashlib.sha256(gate.canonical(value)).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = value if isinstance(value, bytes) else gate.canonical(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(raw)


def read(path):
    return json.loads(path.read_bytes())


def prefix(job, number):
    require(type(number) is int and number > 0, 'invalid recovery build')
    require(job in {COORDINATOR, OWNER, gate.PRODUCT + '/develop', gate.PRODUCT + '/prod'}, 'wrong recovery job')
    return '/' + '/'.join('job/' + item for item in job.split('/')) + '/' + str(number) + '/'


def causes(build):
    return [cause for action in build.get('actions', []) for cause in action.get('causes', [])]


def upstream(build, job, number):
    found = causes(build)
    require(len(found) == 1 and found[0].get('_class') in {
                'hudson.model.Cause$UpstreamCause',
                'org.jenkinsci.plugins.workflow.support.steps.build.BuildUpstreamCause'}
            and found[0].get('upstreamProject') == job
            and found[0].get('upstreamBuild') == number, 'recovery upstream chain mismatch')


def validate_bundle(root, policy, key, now):
    promotion, finalization = read(root / 'promotion.json'), read(root / 'finalization.json')
    original, failed = read(root / 'original-request.json'), read(root / 'failed-receipt.json')
    receipt, finalized = control.verify(promotion, key), control.verify(finalization, key)
    request, failure = control.verify(original, key), control.verify(failed, key)
    identity = read(root / 'recovery-identity.json')
    old_policy = read(root / 'original-policy.json')
    require({k: v for k, v in old_policy.items() if k != 'library_revision'} ==
            {k: v for k, v in policy.items() if k != 'library_revision'}, 'recovery policy changed')
    require(receipt.get('schema_version') == 1 and receipt.get('kind') == 'promotion'
            and receipt.get('status') == 'MERGED' and receipt.get('product') == gate.PRODUCT,
            'invalid original promotion')
    require(receipt.get('source_commit') == identity['source_commit']
            and receipt.get('develop_build') == identity['source_build']
            and receipt.get('develop_job') == gate.PRODUCT + '/develop'
            and receipt.get('policy_sha256') == sha(old_policy), 'original promotion binding mismatch')
    for branch, signed_hash in [('develop', receipt['evidence_sha256']), ('prod', finalized['evidence_sha256'])]:
        evidence_root = root / branch / 'evidence'
        evidence = read(evidence_root / 'evidence.json')
        require(sha(evidence) == signed_hash, 'original evidence signature binding mismatch')
        expected_build = identity['source_build'] if branch == 'develop' else request['prod_build']
        expected_commit = identity['source_commit'] if branch == 'develop' else receipt['merge_commit']
        require(evidence['build'] == expected_build and evidence['commit'] == expected_commit
                and evidence['version'] == receipt['version'], 'original evidence identity mismatch')
        require(gate.evaluate(evidence, policy, evidence_root, now)['decision'] == 'PASS',
                'recovery requires fresh clean evidence')
    prod = read(root / 'prod/evidence/evidence.json')
    # Reuse the normal handoff validator without treating the old request as fresh.
    expected = control.deploy_handoff(promotion, key, prod, policy, root / 'prod/evidence', now,
                                      finalization=finalization)['payload']
    for field in ['schema_version', 'kind', 'product', 'promotion', 'finalization', 'prod_job', 'prod_build',
                  'commit', 'version', 'image', 'digest', 'evidence_sha256', 'deployment_script_revision']:
        require(request.get(field) == expected.get(field), 'original request binding mismatch: ' + field)
    require('recovery' not in request and 'recovery' not in failure, 'recursive recovery is not supported')
    require(failure.get('schema_version') == 1 and failure.get('kind') == 'runtime-deployment'
            and failure.get('status') == 'FAILED' and failure.get('product') == gate.PRODUCT
            and failure.get('request_sha256') == sha(original)
            and failure.get('automatic_rollback') is False
            and failure.get('new_backup_and_migration_files') == [], 'failed receipt is not eligible')
    for field in ['commit', 'version', 'digest', 'library_revision', 'deployment_script_revision']:
        require(failure.get(field) == request.get(field), 'failed receipt identity mismatch')
    require(re.fullmatch(r'[0-9a-f]{64}', failure.get('previous_container_id', ''))
            and gate.DIGEST.fullmatch(failure.get('previous_image_id', '')), 'previous runtime identity missing')
    require(gate.timestamp(request['created_at']) <= gate.timestamp(failure['started_at'])
            <= gate.timestamp(failure['finished_at']) <= now, 'failed attempt timestamps invalid')
    require(gate.timestamp(failure['started_at']) < gate.timestamp(request['expires_at']), 'original attempt was expired')
    return expected


def collect(base, coordinator_build, owner_build, source_build, expected_commit, root, policy, key, now):
    require(gate.SHA.fullmatch(expected_commit), 'invalid exact source commit')
    coord_prefix, owner_prefix = prefix(COORDINATOR, coordinator_build), prefix(OWNER, owner_build)
    def get(path):
        return adapter.jenkins_get(base, path)
    coord, owner = json.loads(get(coord_prefix + 'api/json')), json.loads(get(owner_prefix + 'api/json'))
    for build, number in [(coord, coordinator_build), (owner, owner_build)]:
        require(build.get('number') == number and build.get('building') is False
                and build.get('result') == 'FAILURE', 'recovery requires completed failed coordinator and owner')
    upstream(coord, gate.PRODUCT + '/develop', source_build)
    upstream(owner, COORDINATOR, coordinator_build)
    for target, origin in [('promotion.json', 'promotion.json'), ('finalization.json', 'finalization.json'),
                           ('original-request.json', 'deployment-request.json'), ('original-policy.json', 'control/policy.json')]:
        write(root / target, get(coord_prefix + 'artifact/' + origin))
    write(root / 'failed-receipt.json', get(owner_prefix + 'artifact/runtime-receipt.json'))
    require(json.loads(get(owner_prefix + 'artifact/request.json')) == read(root / 'original-request.json'),
            'owner request differs from coordinator handoff')
    original = control.verify(read(root / 'original-request.json'), key)
    prod_build = original['prod_build']
    upstream(json.loads(get(prefix(gate.PRODUCT + '/prod', prod_build) + 'api/json')), COORDINATOR, coordinator_build)
    write(root / 'recovery-identity.json', {'source_commit': expected_commit, 'source_build': source_build,
          'commit': original['commit'], 'coordinator_build': coordinator_build, 'owner_build': owner_build})
    for branch, number in [('develop', source_build), ('prod', prod_build)]:
        lean = branch == 'develop'
        candidate_root = None if lean else root / branch / 'native'
        native = adapter.inspect(base, branch, number, candidate_root, lean=lean)
        evidence = json.loads(get(coord_prefix + 'artifact/' + branch + '/evidence/evidence.json'))
        # inspect has not scanned yet, so its reports list is intentionally empty.
        # Original scanner reports are separately signature-bound and fully gated.
        require(all(evidence.get(k) == value for k, value in native.items() if k != 'reports'),
                'archived evidence differs from native build')
        evidence_root = root / branch / 'evidence'
        write(evidence_root / 'evidence.json', evidence)
        fetched = {}
        def fetch_refs(value, depth=0):
            require(depth < 16, 'evidence nesting exceeds bound')
            if isinstance(value, list):
                for item in value: fetch_refs(item, depth + 1)
            elif isinstance(value, dict):
                if 'path' in value and 'sha256' in value:
                    name = value['path']
                    require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_./-]+', name)
                            and not Path(name).is_absolute() and '..' not in Path(name).parts, 'unsafe evidence path')
                    require(len(fetched) < 128, 'too many evidence records')
                    if name in fetched:
                        require(fetched[name] == value['sha256'], 'conflicting evidence reference')
                        return
                    raw = get(coord_prefix + 'artifact/' + branch + '/evidence/' + name)
                    require(hashlib.sha256(raw).hexdigest() == value['sha256'], 'archived report checksum mismatch')
                    fetched[name] = value['sha256']
                    write(evidence_root / name, raw)
                    if name.endswith('.json'):
                        envelope = json.loads(raw)
                        for field in ['native_report', 'reports']:
                            if isinstance(envelope, dict) and field in envelope:
                                fetch_refs(envelope[field], depth + 1)
                else:
                    for item in value.values(): fetch_refs(item, depth + 1)
        fetch_refs(evidence)
    validate_bundle(root, policy, key, now)
    return read(root / 'recovery-identity.json')


def handoff(root, source, policy, key, now, approver):
    require(approver in policy['approvers'], 'unauthorized recovery approver')
    payload = validate_bundle(root, policy, key, now)
    receipt = control.verify(read(root / 'promotion.json'), key)
    require(control.git(source, 'remote', 'get-url', 'origin') == control.REMOTE
            and control.git(source, 'rev-parse', 'HEAD') == payload['commit']
            and not control.git(source, 'status', '--porcelain'), 'recovery checkout mismatch')
    require(control.heads(source) == (receipt['source_commit'], payload['commit']), 'release branches advanced')
    require(control.git(source, 'rev-list', '--parents', '-n', '1', payload['commit']).split()
            == [payload['commit'], receipt['previous_prod_commit'], receipt['source_commit']], 'merge parents changed')
    tag = 'refs/tags/v' + payload['version'] + '^{}'
    require(control.git(source, 'ls-remote', '--tags', 'origin', tag).split() == [payload['commit'], tag],
            'finalized release tag changed')
    evidence = read(root / 'prod/evidence/evidence.json')
    images = subprocess.run(['docker', 'image', 'inspect', payload['image']], capture_output=True, text=True, timeout=30)
    require(images.returncode == 0, 'original immutable image unavailable')
    adapter.candidate_image(evidence, json.loads(images.stdout))
    final = control.verify(read(root / 'finalization.json'), key)
    base = os.environ['NEXUS_BASE_URL'].rstrip('/')
    require(re.fullmatch(r'https?://[A-Za-z0-9.-]+(?::[0-9]+)?', base), 'invalid trusted Nexus endpoint')
    url = f"{base}/repository/raw-artifacts/{gate.PRODUCT}/prod/{payload['version']}-{payload['prod_build']}-{payload['commit'][:7]}/{evidence['artifact_name']}"
    require(final.get('artifact_sha256') == evidence['artifact']['sha256']
            and final.get('manifest') == {'RELEASE_TAG': 'v' + payload['version'], 'GIT_COMMIT': payload['commit'],
                'IMAGE_REF': evidence['image_ref'], 'IMAGE_DIGEST': payload['digest'], 'NEXUS_ARTIFACT_URL': url},
            'original published manifest mismatch')
    require(finalizer.published_hash(url) == evidence['artifact']['sha256'], 'published artifact bytes changed')
    # Network operations consume time: evaluate every original timestamp again.
    payload = validate_bundle(root, policy, key, dt.datetime.now(dt.timezone.utc))
    identity = read(root / 'recovery-identity.json')
    payload['recovery'] = {'kind': 'failed-deployment-retry', 'coordinator_build': identity['coordinator_build'],
        'owner_build': identity['owner_build'], 'authorized_by': approver,
        'failed_receipt': read(root / 'failed-receipt.json'), 'attempt_id': str(uuid.uuid4())}
    deadlines = [gate.timestamp(payload['expires_at'])]
    for branch in ['develop', 'prod']:
        evidence_root = root / branch / 'evidence'
        evidence = read(evidence_root / 'evidence.json')
        timestamps = [evidence['completed_at']] + [gate.verified_report(evidence_root, ref)['completed_at']
                                                    for ref in evidence['reports']]
        deadlines.extend(gate.timestamp(value) + dt.timedelta(seconds=policy['max_evidence_age_seconds'])
                         for value in timestamps)
    payload['expires_at'] = min(deadlines).isoformat()
    require(gate.timestamp(payload['created_at']) < min(deadlines), 'recovery evidence expired before handoff')
    return control.sign(payload, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['collect', 'handoff'])
    for name in ['root', 'policy', 'receipt-key-file']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--jenkins-url')
    for name in ['coordinator-build', 'owner-build', 'source-build']:
        parser.add_argument('--' + name, type=int)
    parser.add_argument('--expected-commit')
    parser.add_argument('--approver')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        policy, key, now = read(args.policy), args.receipt_key_file.read_bytes(), dt.datetime.now(dt.timezone.utc)
        if args.command == 'collect':
            collect(args.jenkins_url, args.coordinator_build, args.owner_build, args.source_build,
                    args.expected_commit, args.root, policy, key, now)
        else:
            write(args.output, handoff(args.root, args.source, policy, key, now, args.approver))
    except Exception as exc:
        print('BLOCKED: ' + (str(exc) if isinstance(exc, gate.InvalidEvidence) else 'deployment recovery verification failed'), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
