#!/usr/bin/env python3
"""Fresh coordinator verification of an unchanged, finalized failed release.

No merge, image build/push, publication, tag change or owner action occurs here.
Historical evidence remains historical; current checks have their own run ID.
"""
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

import importlib.util


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


recovery = module('revalidate_recovery', 'release-recovery.py')
control, adapter, finalizer = recovery.control, recovery.adapter, recovery.finalizer
gate, require = control.gate, control.require
COORDINATOR, OWNER = recovery.COORDINATOR, recovery.OWNER
MODE = 'finalized-image-revalidation-v1'
CHECKS = {'test', 'contract', 'runtime'}


def read(path):
    return json.loads(path.read_bytes())


def sha(value):
    return hashlib.sha256(gate.canonical(value)).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = value if isinstance(value, bytes) else gate.canonical(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def record(root, path):
    require(path.is_file() and not path.is_symlink()
            and path.resolve().is_relative_to(root.resolve()), 'unsafe revalidation report')
    return {'path': str(path.relative_to(root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def historical_time(root, key, now):
    failed = control.verify(read(root / 'failed-receipt.json'), key)
    finished = gate.timestamp(failed['finished_at'])
    require(finished <= now, 'future original failed receipt')
    return finished


def validate_historical(root, policy, key, now):
    # The signed failed attempt's completion is a historical validation point,
    # not a refreshed evidence timestamp or a claim of current eligibility.
    recovery.validate_bundle(root, policy, key, historical_time(root, key, now))
    return control.verify(read(root / 'original-request.json'), key)


def collect(base, coordinator_build, owner_build, source_build, expected_commit,
            root, policy, key, now, run):
    require(run.get('job') == COORDINATOR and type(run.get('build')) is int
            and run['build'] > coordinator_build, 'invalid fresh coordinator identity')
    historical = root / 'historical'
    # Read only signed metadata before using its historical clock. The normal
    # collector then authenticates every native upstream and referenced byte.
    failed = json.loads(adapter.jenkins_get(base, recovery.prefix(OWNER, owner_build)
                                           + 'artifact/runtime-receipt.json'))
    finished = gate.timestamp(control.verify(failed, key)['finished_at'])
    require(finished <= now, 'future original failed receipt')
    recovery.collect(base, coordinator_build, owner_build, source_build, expected_commit,
                     historical, policy, key, finished)
    request = validate_historical(historical, policy, key, now)
    require(request.get('mode') is None and request.get('schema_version') == 2,
            'recursive or non-original revalidation is forbidden')
    identity = {'schema_version': 1, 'kind': MODE, 'run': run, 'created_at': now.isoformat(),
                'attempt_id': str(uuid.uuid4()), 'original': read(historical / 'recovery-identity.json'),
                'original_request_sha256': sha(read(historical / 'original-request.json')),
                'failed_receipt_sha256': sha(read(historical / 'failed-receipt.json')),
                'policy_sha256': sha(policy)}
    identity['original']['prod_build'] = request['prod_build']
    write(root / 'revalidation-identity.json', identity)
    prod = read(historical / 'prod/evidence/evidence.json')
    fresh = root / 'fresh'
    artifact = gate.verified_bytes(historical / 'prod/evidence', prod['artifact'])
    write(fresh / 'original-artifact', artifact)
    seed = {name: prod[name] for name in ['product', 'commit', 'version', 'branch', 'image_ref',
                                         'immutable_image', 'image_digest', 'artifact_name']}
    seed.update(schema_version=1, mode=MODE, run=run, reports=[],
                artifact=record(fresh, fresh / 'original-artifact'))
    write(root / 'scan-identity.json', seed)
    return identity


def local_source(source, commit):
    require(control.git(source, 'remote', 'get-url', 'origin') == control.REMOTE
            and control.git(source, 'rev-parse', 'HEAD') == commit
            and not control.git(source, 'status', '--porcelain'), 'revalidation checkout mismatch')


def runtime_report(value, seed, run, original_build):
    expected = {'coordinator_job': run['job'], 'coordinator_build': run['build'],
                'original_prod_build': original_build, 'commit': seed['commit'],
                'version': seed['version'], 'image': seed['immutable_image']}
    require(value.get('schema_version') == 1 and value.get('product') == gate.PRODUCT
            and value.get('commit') == seed['commit'] and value.get('image') == seed['immutable_image']
            and value.get('profile') == 'compose-v2' and value.get('bootstrap_empty') is True
            and all(value.get(name) == 'PASS' for name in ['health', 'shutdown', 'cleanup'])
            and value.get('revalidation_run') == expected, 'fresh runtime verification mismatch')


def junit(raw):
    value = ET.fromstring(raw)
    cases = list(value.iter('testcase'))
    require(value.tag in {'testsuite', 'testsuites'} and cases
            and not list(value.iter('error')) and not list(value.iter('failure'))
            and any(case.find('skipped') is None for case in cases), 'fresh tests lack clean JUnit evidence')


def check(root, source, kind, command, report_path=None, clock=None):
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    require(kind in CHECKS and command, 'unsupported fresh check')
    identity, seed = read(root / 'revalidation-identity.json'), read(root / 'scan-identity.json')
    local_source(source, seed['commit'])
    if kind in {'test', 'runtime'}:
        require(report_path is not None and not report_path.exists(), 'fresh report must not preexist')
        require(not report_path.is_symlink() and report_path.resolve().is_relative_to(
            (source if kind == 'test' else root).resolve()), 'fresh report path escapes scope')
    else:
        require(report_path is None, 'unexpected contract report')
    fresh = root / 'fresh'
    output = fresh / (kind + '.stdout')
    errors = fresh / (kind + '.stderr')
    started = clock()
    with output.open('xb') as stdout, errors.open('xb') as stderr:
        result = subprocess.run(command, cwd=source, stdout=stdout, stderr=stderr,
                                env=os.environ.copy(), timeout=1800)
    completed = clock()
    refs = [record(fresh, output), record(fresh, errors)]
    value = {'schema_version': 1, 'kind': kind, 'run': identity['run'], 'commit': seed['commit'],
             'started_at': started.isoformat(), 'completed_at': completed.isoformat(),
             'exit_code': result.returncode, 'command': command, 'reports': refs}
    if result.returncode == 0:
        require(output.read_bytes(), 'fresh check produced no execution log')
        if kind in {'test', 'runtime'}:
            raw = report_path.read_bytes()
            if kind == 'test':
                junit(raw)
            else:
                runtime_report(json.loads(raw), seed, identity['run'], identity['original']['prod_build'])
            target = fresh / (kind + '.raw')
            write(target, raw)
            value['reports'].append(record(fresh, target))
    write(fresh / (kind + '.json'), value)
    require(result.returncode == 0, 'fresh check failed: ' + kind)
    return value


def scan(root, source, policy, harbor_url):
    identity, seed = read(root / 'revalidation-identity.json'), read(root / 'scan-identity.json')
    require(identity['policy_sha256'] == sha(policy), 'revalidation policy changed')
    local_source(source, seed['commit'])
    fresh = root / 'fresh'
    require(not (fresh / 'evidence.json').exists(), 'fresh scans already claimed')
    for kind in sorted(CHECKS):
        item = read(fresh / (kind + '.json'))
        require(item.get('kind') == kind and item.get('run') == identity['run']
                and item.get('commit') == seed['commit'] and item.get('exit_code') == 0,
                'fresh checks must pass before scanners')
    value = adapter.scan(copy.deepcopy(seed), source, fresh, harbor_url)
    # This is a new coordinator verification, never a relabelled PROD CI build.
    value.update(completed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                 checks=[record(fresh, fresh / (kind + '.json')) for kind in sorted(CHECKS)])
    # scan() owns evidence.json; the final immutable coordinator envelope is a
    # different file so no previous scanner evidence is overwritten here.
    write(fresh / 'revalidation.json', value)
    return value


def validate_fresh(root, policy, key, now):
    original = validate_historical(root / 'historical', policy, key, now)
    identity = read(root / 'revalidation-identity.json')
    fresh = root / 'fresh'
    evidence = read(fresh / 'revalidation.json')
    require(identity['policy_sha256'] == sha(policy) and evidence.get('mode') == MODE
            and evidence.get('run') == identity['run'], 'fresh coordinator binding mismatch')
    for field, target in [('commit', 'commit'), ('version', 'version'), ('image_digest', 'digest'),
                          ('immutable_image', 'image')]:
        require(evidence.get(field) == original[target], 'same-image revalidation identity changed')
    final = control.verify(read(root / 'historical/finalization.json'), key)
    require(evidence['artifact']['sha256'] == final['artifact_sha256']
            and gate.verified_bytes(fresh, evidence['artifact']), 'original binary bytes changed')
    created = gate.timestamp(identity['created_at'])
    require(created >= gate.timestamp(control.verify(read(root / 'historical/failed-receipt.json'), key)['finished_at']),
            'fresh verification predates failed attempt')
    records = [gate.verified_report(fresh, ref) for ref in evidence['checks']]
    require(len(records) == len(CHECKS) and {r['kind'] for r in records} == CHECKS,
            'missing or duplicate fresh check')
    deadlines = []
    for item in records:
        require(item.get('schema_version') == 1 and item.get('run') == identity['run']
                and item.get('commit') == original['commit'] and item.get('exit_code') == 0
                and isinstance(item.get('command'), list) and item['command'], 'fresh check identity/status mismatch')
        started, completed = gate.timestamp(item['started_at']), gate.timestamp(item['completed_at'])
        require(created <= started <= completed <= now, 'fresh check timestamps mismatch')
        require(0 <= (now-completed).total_seconds() <= policy['max_evidence_age_seconds'], 'stale fresh check')
        raw = [gate.verified_bytes(fresh, ref) for ref in item['reports']]
        require(len(raw) == (2 if item['kind'] == 'contract' else 3) and raw[0], 'fresh execution reports missing')
        if item['kind'] == 'test': junit(raw[-1])
        if item['kind'] == 'runtime': runtime_report(json.loads(raw[-1]), evidence, identity['run'], original['prod_build'])
        deadlines.append(completed + dt.timedelta(seconds=policy['max_evidence_age_seconds']))
    # Deliberately do not call evaluate() on old PROD CI with a fresh timestamp.
    # Reuse its exact scanner/native/package-graph/GO-2026-5932 semantics instead.
    findings, not_applicable = gate.verified_findings(evidence, policy, fresh, now)
    require(not findings, 'fresh revalidation requires clean PASS evidence')
    for ref in evidence['reports']:
        completed = gate.timestamp(gate.verified_report(fresh, ref)['completed_at'])
        require(completed >= created, 'scanner evidence predates fresh run')
        deadlines.append(completed + dt.timedelta(seconds=policy['max_evidence_age_seconds']))
    graph = gate.verified_report(fresh, evidence['package_graph'])
    require(graph.get('schema_version') == 1 and graph.get('complete') is True
            and graph.get('commit') == original['commit'] and graph.get('go_version')
            and isinstance(graph.get('graphs'), list) and len(graph['graphs']) == len(adapter.PACKAGE_GRAPHS)
            and {item['name'] for item in graph['graphs']} ==
            {item['name'] for item in adapter.PACKAGE_GRAPHS}, 'fresh package graphs missing')
    specs = {item['name']: item for item in adapter.PACKAGE_GRAPHS}
    for item in graph['graphs']:
        require(all(item.get(name) == value for name, value in specs[item['name']].items())
                and type(item.get('test')) is bool and item.get('goos') == 'linux'
                and item.get('goarch') == 'arm64' and item.get('cgo_enabled') == '0'
                and isinstance(item.get('packages'), list) and item['packages']
                and all(isinstance(name, str) and name for name in item['packages']),
                'fresh package graph build conditions differ')
    completed = gate.timestamp(evidence['completed_at'])
    require(created <= completed <= now, 'fresh envelope timestamp mismatch')
    return original, identity, evidence, min(deadlines), {
        'decision': 'PASS', 'finding_count': 0, 'not_applicable': sorted(not_applicable)}


def validate_identity(request, key, now):
    require(request.get('schema_version') == 4 and request.get('kind') == 'deployment'
            and request.get('mode') == MODE and request.get('product') == gate.PRODUCT,
            'invalid finalized-image revalidation request')
    rv = request['revalidation']
    require(isinstance(rv, dict) and set(rv) == {'kind', 'run', 'original_request', 'evidence_sha256',
                'policy_sha256', 'verified_at', 'evidence_expires_at'} and rv['kind'] == MODE,
            'invalid revalidation evidence contract')
    old = control.verify(rv['original_request'], key)
    require(old.get('schema_version') == 2 and old.get('kind') == 'deployment'
            and old.get('product') == gate.PRODUCT and 'mode' not in old and 'recovery' not in old
            and 'revalidation' not in old, 'recursive revalidation is forbidden')
    require(set(request) == set(old) | {'mode', 'recovery', 'revalidation'},
            'unexpected revalidation request fields')
    for field in ['promotion', 'finalization', 'prod_job', 'prod_build', 'commit', 'version',
                  'image', 'digest', 'evidence_sha256', 'deployment_script_revision']:
        require(request.get(field) == old.get(field), 'original immutable release changed: ' + field)
    promotion, final = control.verify(request['promotion'], key), control.verify(request['finalization'], key)
    require(promotion.get('schema_version') == 1 and promotion.get('kind') == 'promotion'
            and promotion.get('status') == 'MERGED' and promotion.get('product') == gate.PRODUCT
            and promotion.get('develop_job') == gate.PRODUCT + '/develop'
            and gate.SHA.fullmatch(promotion.get('source_commit', ''))
            and promotion.get('merge_commit') == request['commit'] and promotion.get('version') == request['version']
            and final.get('schema_version') == 1 and final.get('kind') == 'finalization'
            and final.get('status') == 'SUCCESS' and final.get('product') == gate.PRODUCT
            and final.get('commit') == request['commit'] and final.get('digest') == request['digest']
            and final.get('version') == request['version']
            and final.get('evidence_sha256') == request['evidence_sha256'], 'original signed chain mismatch')
    require(gate.SHA.fullmatch(request['commit']) and gate.SHA.fullmatch(request.get('library_revision', ''))
            and gate.DIGEST.fullmatch(request['digest']) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', request['version'])
            and request['image'] == f"localhost:9290/{gate.PRODUCT}/prod/{request['version']}@{request['digest']}"
            and request['prod_job'] == gate.PRODUCT + '/prod' and type(request['prod_build']) is int
            and request['prod_build'] > 0 and request['deployment_script_revision'] == request['commit'],
            'invalid same-image release identity')
    require(request['decision'].get('decision') == 'PASS' and request['decision'].get('finding_count') == 0,
            'fresh revalidation requires PASS')
    retry = request['recovery']
    require(set(retry) == {'kind', 'attempt_id', 'coordinator_build', 'owner_build', 'authorized_by', 'failed_receipt'}
            and retry['kind'] == 'failed-deployment-retry' and retry['attempt_id'] == str(uuid.UUID(retry['attempt_id'], version=4))
            and all(type(retry[n]) is int and retry[n] > 0 for n in ['coordinator_build', 'owner_build'])
            and isinstance(retry['authorized_by'], str) and re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', retry['authorized_by']),
            'invalid once-only failed-attempt recovery')
    failed = control.verify(retry['failed_receipt'], key)
    require(failed.get('schema_version') == 1 and failed.get('kind') == 'runtime-deployment'
            and failed.get('status') == 'FAILED' and failed.get('product') == gate.PRODUCT
            and 'recovery' not in failed and 'failed_receipt_sha256' not in failed
            and failed.get('request_sha256') == sha(rv['original_request'])
            and failed.get('new_backup_and_migration_files') == [] and failed.get('automatic_rollback') is False,
            'ineligible or recursive failed receipt')
    require(all(failed.get(field) == old[field] for field in ['commit', 'version', 'digest', 'library_revision',
                                                             'deployment_script_revision']), 'failed release identity differs')
    require(re.fullmatch(r'[0-9a-f]{64}', failed.get('previous_container_id', ''))
            and gate.DIGEST.fullmatch(failed.get('previous_image_id', '')), 'original runtime identity missing')
    require(set(rv['run']) == {'job', 'build'} and rv['run'].get('job') == COORDINATOR
            and type(rv['run'].get('build')) is int
            and rv['run']['build'] > retry['coordinator_build'] and
            all(re.fullmatch(r'[0-9a-f]{64}', rv[n]) for n in ['evidence_sha256', 'policy_sha256']),
            'invalid fresh coordinator provenance')
    issued, expires = gate.timestamp(request['created_at']), gate.timestamp(request['expires_at'])
    verified = gate.timestamp(rv['verified_at'])
    require(verified == issued and issued <= now < expires
            and 0 < (expires-issued).total_seconds() <= 900
            and expires <= gate.timestamp(rv['evidence_expires_at']), 'expired fresh revalidation handoff')
    original_issued, original_expires = gate.timestamp(old['created_at']), gate.timestamp(old['expires_at'])
    require(0 < (original_expires-original_issued).total_seconds() <= 900
            and original_issued <= gate.timestamp(failed['started_at']) < original_expires
            and gate.timestamp(failed['started_at']) <= gate.timestamp(failed['finished_at']) <= issued,
            'invalid historical attempt timestamps')
    return request


def handoff(root, source, policy, key, now, approver):
    require(approver in policy['approvers'], 'unauthorized revalidation operator')
    original, identity, evidence, deadline, decision = validate_fresh(root, policy, key, now)
    receipt = control.verify(read(root / 'historical/promotion.json'), key)
    local_source(source, original['commit'])
    require(control.heads(source) == (receipt['source_commit'], original['commit']), 'release branches advanced')
    require(control.git(source, 'rev-list', '--parents', '-n', '1', original['commit']).split() ==
            [original['commit'], receipt['previous_prod_commit'], receipt['source_commit']], 'merge parents changed')
    tag = 'refs/tags/v' + original['version'] + '^{}'
    require(control.git(source, 'ls-remote', '--tags', 'origin', tag).split() == [original['commit'], tag],
            'published tag changed')
    inspected = subprocess.run(['docker', 'image', 'inspect', original['image']], capture_output=True,
                               text=True, timeout=30)
    require(inspected.returncode == 0, 'original immutable image unavailable')
    adapter.candidate_image(evidence, json.loads(inspected.stdout))
    final = control.verify(read(root / 'historical/finalization.json'), key)
    base = os.environ['NEXUS_BASE_URL'].rstrip('/')
    require(re.fullmatch(r'https?://[A-Za-z0-9.-]+(?::[0-9]+)?', base), 'invalid trusted Nexus endpoint')
    url = (f"{base}/repository/raw-artifacts/{gate.PRODUCT}/prod/"
           f"{original['version']}-{original['prod_build']}-{original['commit'][:7]}/{evidence['artifact_name']}")
    require(final.get('manifest') == {'RELEASE_TAG': 'v' + original['version'], 'GIT_COMMIT': original['commit'],
                'IMAGE_REF': evidence['image_ref'], 'IMAGE_DIGEST': original['digest'], 'NEXUS_ARTIFACT_URL': url},
            'original published manifest mismatch')
    require(finalizer.published_hash(url) == final['artifact_sha256'],
            'original published artifact changed')
    now = dt.datetime.now(dt.timezone.utc)
    original, identity, evidence, deadline, decision = validate_fresh(root, policy, key, now)
    request = copy.deepcopy(original)
    request.update(schema_version=4, mode=MODE, decision=decision, library_revision=policy['library_revision'],
                   created_at=now.isoformat(), expires_at=min(now+dt.timedelta(minutes=15), deadline).isoformat())
    request['revalidation'] = {'kind': MODE, 'run': identity['run'],
        'original_request': read(root / 'historical/original-request.json'),
        'evidence_sha256': sha(evidence), 'policy_sha256': sha(policy),
        'verified_at': now.isoformat(), 'evidence_expires_at': deadline.isoformat()}
    request['recovery'] = {'kind': 'failed-deployment-retry', 'attempt_id': identity['attempt_id'],
        'coordinator_build': identity['original']['coordinator_build'],
        'owner_build': identity['original']['owner_build'], 'authorized_by': approver,
        'failed_receipt': read(root / 'historical/failed-receipt.json')}
    validate_identity(request, key, now)
    return control.sign(request, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['collect', 'check', 'scan', 'handoff'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--receipt-key-file', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--jenkins-url')
    for name in ['coordinator-build', 'owner-build', 'source-build']:
        parser.add_argument('--' + name, type=int)
    parser.add_argument('--expected-commit')
    parser.add_argument('--kind', choices=sorted(CHECKS))
    parser.add_argument('--report-path', type=Path)
    parser.add_argument('--harbor-url')
    parser.add_argument('--approver')
    parser.add_argument('--output', type=Path)
    args, argv = parser.parse_known_args()
    try:
        require(os.environ.get('JOB_NAME') == COORDINATOR
                and re.fullmatch(r'[1-9][0-9]*', os.environ.get('BUILD_NUMBER', '')), 'fixed coordinator required')
        run = {'job': COORDINATOR, 'build': int(os.environ['BUILD_NUMBER'])}
        if args.command != 'collect':
            require(read(args.root / 'revalidation-identity.json')['run'] == run, 'fresh run differs from coordinator')
        now = dt.datetime.now(dt.timezone.utc)
        require(args.command == 'check' or not argv, 'unexpected revalidation command arguments')
        policy = read(args.policy) if args.policy else None
        key = args.receipt_key_file.read_bytes() if args.receipt_key_file else None
        if args.command == 'collect':
            collect(args.jenkins_url, args.coordinator_build, args.owner_build, args.source_build,
                    args.expected_commit, args.root, policy, key, now, run)
        elif args.command == 'check':
            command = argv[1:] if argv[:1] == ['--'] else argv
            check(args.root, args.source, args.kind, command, args.report_path)
        elif args.command == 'scan':
            scan(args.root, args.source, policy, args.harbor_url)
        else:
            write(args.output, handoff(args.root, args.source, policy, key, now, args.approver))
    except Exception as exc:
        print('BLOCKED: ' + (str(exc) if isinstance(exc, gate.InvalidEvidence)
                            else 'same-image revalidation failed; preserve evidence'), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
