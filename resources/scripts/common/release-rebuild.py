#!/usr/bin/env python3
"""由已發布的 PROD source 重建；保留原 tag／receipt，以新 CI 證據授權災難恢復。"""
import argparse
import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


control = module('rebuild_control', 'release-promotion.py')
gate, require = control.gate, control.require
COORDINATOR = 'shiba-release-automation/' + gate.PRODUCT + '-auto-release'


def sha(value):
    return hashlib.sha256(gate.canonical(value)).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as out:
        out.write(gate.canonical(value))


def published_identity(bundle, key, commit):
    promotion = control.verify(bundle['promotion'], key)
    final = control.verify(bundle['finalization'], key)
    original = control.verify(bundle['request'], key)
    require(promotion.get('kind') == 'promotion' and promotion.get('status') == 'MERGED'
            and promotion.get('product') == gate.PRODUCT and promotion.get('merge_commit') == commit,
            'published promotion mismatch')
    require(promotion.get('develop_job') == gate.PRODUCT + '/develop'
            and gate.SHA.fullmatch(promotion.get('source_commit', '')), 'missing original develop provenance')
    require(final.get('kind') == 'finalization' and final.get('status') == 'SUCCESS'
            and final.get('product') == gate.PRODUCT and final.get('commit') == commit
            and final.get('version') == promotion.get('version'), 'published finalization mismatch')
    require(original.get('schema_version') == 2 and original.get('kind') == 'deployment'
            and original.get('product') == gate.PRODUCT and original.get('commit') == commit
            and original.get('version') == final['version'] and original.get('digest') == final['digest']
            and original.get('evidence_sha256') == final['evidence_sha256']
            and original.get('promotion') == bundle['promotion']
            and original.get('finalization') == bundle['finalization'], 'published handoff mismatch')
    require(original.get('prod_job') == gate.PRODUCT + '/prod'
            and type(original.get('prod_build')) is int and original['prod_build'] > 0,
            'published PROD build missing')
    return promotion, final, original


def collect(base, number, commit, root, key):
    require(type(number) is int and number > 0 and gate.SHA.fullmatch(commit), 'invalid published coordinate')
    adapter = module('rebuild_adapter', 'release-evidence.py')
    prefix = '/job/shiba-release-automation/job/' + gate.PRODUCT + '-auto-release/' + str(number) + '/'
    build = json.loads(adapter.jenkins_get(base, prefix + 'api/json'))
    require(build.get('number') == number and build.get('building') is False
            and build.get('result') == 'SUCCESS', 'published coordinator was not successful')
    bundle = {'coordinator_build': number}
    for field, name in [('promotion', 'promotion.json'), ('finalization', 'finalization.json'),
                        ('request', 'deployment-request.json')]:
        bundle[field] = json.loads(adapter.jenkins_get(base, prefix + 'artifact/' + name))
    _, _, original = published_identity(bundle, key, commit)
    native = json.loads(adapter.jenkins_get(base, '/job/' + gate.PRODUCT + '/job/prod/'
                         + str(original['prod_build']) + '/api/json'))
    causes = [c for a in native.get('actions', []) for c in a.get('causes', [])]
    require(native.get('building') is False and native.get('result') in {'SUCCESS', 'UNSTABLE'}
            and len(causes) == 1 and causes[0].get('upstreamProject') == COORDINATOR
            and causes[0].get('upstreamBuild') == number, 'published native build chain mismatch')
    write(root / 'published.json', bundle)


def validate_identity(request, key, now):
    require(request.get('schema_version') == 3 and request.get('kind') == 'deployment'
            and request.get('mode') == 'published-prod-disaster-rebuild'
            and request.get('product') == gate.PRODUCT and 'recovery' not in request,
            'invalid published rebuild request')
    require(gate.SHA.fullmatch(request.get('commit', '')) and gate.DIGEST.fullmatch(request.get('digest', ''))
            and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', request.get('version', '')), 'invalid rebuild identity')
    require(request.get('image') == f"localhost:9290/{gate.PRODUCT}/prod/{request['version']}@{request['digest']}",
            'unexpected rebuild image')
    require(request.get('prod_job') == gate.PRODUCT + '/prod' and type(request.get('prod_build')) is int
            and request['prod_build'] > 0, 'invalid fresh PROD coordinate')
    require(request.get('restore_missing_runtime') is True and request.get('authorized_by')
            and re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', request['authorized_by'])
            and re.fullmatch(r'[0-9a-f-]{36}', request.get('target_engine_id', '')), 'invalid restore authorization')
    require(request.get('decision', {}).get('decision') in {'PASS', 'APPROVED_EXCEPTION'}
            and re.fullmatch(r'[0-9a-f]{64}', request.get('evidence_sha256', ''))
            and re.fullmatch(r'[0-9a-f]{64}', request.get('artifact_sha256', '')),
            'missing fresh approved evidence')
    issued, expires = gate.timestamp(request['created_at']), gate.timestamp(request['expires_at'])
    require(issued <= now < expires and 0 < (expires-issued).total_seconds() <= 900, 'expired rebuild request')
    require(request.get('deployment_script_revision') == request['commit']
            and gate.SHA.fullmatch(request.get('library_revision', '')), 'invalid rebuild code provenance')
    _, final, original = published_identity(request['published'], key, request['commit'])
    require(final['version'] == request['version'] and request['prod_build'] > original['prod_build'],
            'rebuild must use a newer native CI build of the same published version')
    return request


def verify_source(source, commit, version):
    require(control.git(source, 'remote', 'get-url', 'origin') == control.REMOTE
            and control.git(source, 'rev-parse', 'HEAD') == commit
            and not control.git(source, 'status', '--porcelain'), 'rebuild source mismatch')
    require(control.heads(source)[1] == commit, 'prod head moved')
    tag = 'refs/tags/v' + version + '^{}'
    require(control.git(source, 'ls-remote', '--tags', 'origin', tag).split() == [commit, tag],
            'published tag changed')


def publish_artifact(base, evidence, raw):
    finalizer = module('rebuild_finalizer', 'release-finalization.py')
    require(re.fullmatch(r'https?://[A-Za-z0-9.-]+(?::[0-9]+)?', base), 'invalid Nexus endpoint')
    url = (f"{base}/repository/raw-artifacts/{gate.PRODUCT}/prod/"
           f"{evidence['version']}-{evidence['build']}-{evidence['commit'][:7]}/{evidence['artifact_name']}")
    try:
        existing = finalizer.published_hash(url)
    except urllib.error.HTTPError as exc:
        require(exc.code == 404, 'Nexus artifact status unavailable')
        auth = base64.b64encode((os.environ['NEXUS_CRED_USR'] + ':' + os.environ['NEXUS_CRED_PSW']).encode()).decode()
        request = urllib.request.Request(url, data=raw, method='PUT',
                    headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/octet-stream'})
        with urllib.request.build_opener(finalizer.NoRedirect).open(request, timeout=120) as response:
            require(response.status in {200, 201, 204}, 'rebuild artifact publication failed')
        existing = finalizer.published_hash(url)
    require(existing == evidence['artifact']['sha256'], 'rebuild artifact mismatch; never overwrite existing bytes')
    return url


def handoff(bundle, evidence, policy, root, source, key, now, author, engine, approval=None, approval_key=None):
    require(author in policy['approvers'], 'unauthorized rebuild operator')
    _, final, original = published_identity(bundle, key, evidence['commit'])
    require(evidence.get('mode') == 'controlled-candidate-v1' and evidence.get('gate') == 'deployment'
            and evidence['version'] == final['version'] and evidence['build'] > original['prod_build'],
            'not a fresh build of the published PROD version')
    decision = gate.evaluate(evidence, policy, root, now, approval, approval_key)
    verify_source(source, evidence['commit'], evidence['version'])
    adapter = module('rebuild_image_adapter', 'release-evidence.py')
    images = subprocess.run(['docker', 'image', 'inspect', evidence['immutable_image']], capture_output=True, text=True, timeout=30)
    require(images.returncode == 0, 'rebuild candidate missing')
    adapter.candidate_image(evidence, json.loads(images.stdout))
    raw = gate.verified_bytes(root, evidence['artifact'])
    require(evidence['artifact_name'] == f"{gate.PRODUCT}-prod-{evidence['version']}", 'wrong artifact name')
    base = os.environ['NEXUS_BASE_URL'].rstrip('/')
    # 既有正式成品保持原樣；新 build 使用獨立的 build/commit 路徑。
    finalizer = module('rebuild_published_finalizer', 'release-finalization.py')
    old_url = (f"{base}/repository/raw-artifacts/{gate.PRODUCT}/prod/"
               f"{final['version']}-{original['prod_build']}-{final['commit'][:7]}/{evidence['artifact_name']}")
    require(final['manifest']['NEXUS_ARTIFACT_URL'] == old_url
            and finalizer.published_hash(old_url) == final['artifact_sha256'], 'original publication changed')
    url = publish_artifact(base, evidence, raw)
    now = dt.datetime.now(dt.timezone.utc)
    decision = gate.evaluate(evidence, policy, root, now, approval, approval_key)
    verify_source(source, evidence['commit'], evidence['version'])
    deadlines = [now + dt.timedelta(seconds=900)]
    stamps = [evidence['completed_at']] + [gate.verified_report(root, ref)['completed_at'] for ref in evidence['reports']]
    deadlines += [gate.timestamp(s) + dt.timedelta(seconds=policy['max_evidence_age_seconds']) for s in stamps]
    if approval:
        deadlines.append(gate.timestamp(approval['payload']['expires_at']))
    request = {'schema_version': 3, 'kind': 'deployment', 'mode': 'published-prod-disaster-rebuild',
        'product': gate.PRODUCT, 'published': bundle, 'prod_job': evidence['job'], 'prod_build': evidence['build'],
        'commit': evidence['commit'], 'version': evidence['version'], 'image': evidence['immutable_image'],
        'digest': evidence['image_digest'], 'artifact_sha256': evidence['artifact']['sha256'],
        'artifact_url': url, 'evidence_sha256': sha(evidence), 'decision': decision,
        'authorized_by': author, 'target_engine_id': engine, 'restore_missing_runtime': True,
        'created_at': now.isoformat(), 'expires_at': min(deadlines).isoformat(),
        'library_revision': policy['library_revision'], 'deployment_script_revision': evidence['commit']}
    validate_identity(request, key, now)
    return control.sign(request, key)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['collect', 'handoff'])
    for name in ['root', 'source', 'policy', 'evidence', 'receipt-key-file', 'approval', 'approval-key-file', 'output']:
        p.add_argument('--' + name, type=Path)
    for name in ['jenkins-url', 'expected-commit', 'approver', 'engine-id']:
        p.add_argument('--' + name)
    p.add_argument('--coordinator-build', type=int)
    a = p.parse_args()
    try:
        key = a.receipt_key_file.read_bytes()
        if a.command == 'collect':
            collect(a.jenkins_url, a.coordinator_build, a.expected_commit, a.root, key)
        else:
            result = handoff(read(a.root/'published.json'), read(a.evidence), read(a.policy), a.evidence.parent,
                a.source, key, dt.datetime.now(dt.timezone.utc), a.approver, a.engine_id,
                read(a.approval) if a.approval else None, a.approval_key_file.read_bytes() if a.approval_key_file else None)
            write(a.output, result)
    except Exception as exc:
        print('BLOCKED: ' + (str(exc) if isinstance(exc, gate.InvalidEvidence) else 'published rebuild failed'), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
