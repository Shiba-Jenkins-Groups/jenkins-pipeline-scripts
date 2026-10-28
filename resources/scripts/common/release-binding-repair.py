#!/usr/bin/env python3
"""Signed, one-attempt App binding maintenance in the fixed Jenkins owner.

No build, deployment, scanner, SQLite access, rollback or standalone operator
mutation is implied. The coordinator authenticates the original successful
owner build and issues a new short-lived maintenance request. The owner checks
its persistent original receipt before touching the native binding.
"""
import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


deploy = module('binding_repair_deploy', 'release-deploy.py')
control, gate, require = deploy.control, deploy.gate, deploy.require
adapter = module('binding_repair_evidence', 'release-evidence.py')
COORDINATOR = 'shiba-release-automation/' + gate.PRODUCT + '-auto-release'
OWNER = 'shiba-release-automation/' + gate.PRODUCT + '-prod-deploy'
PINS = {'owner.json', 'registry.json', 'installation.json', 'consumer-admission.json'}
HASH = re.compile(r'[0-9a-f]{64}\Z')


def sha(value):
    return hashlib.sha256(gate.canonical(value)).hexdigest()


def pairs(values):
    result = {}
    for name, value in values:
        require(name not in result, 'duplicate binding repair JSON key')
        result[name] = value
    return result


def read(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536,
            'binding repair input missing or oversized')
    return json.loads(path.read_bytes(), object_pairs_hook=pairs)


def write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(gate.canonical(value))
        out.flush()
        os.fsync(out.fileno())


def signed_payload(value, key):
    require(isinstance(value, dict) and set(value) == {'payload', 'signature'}
            and isinstance(value['payload'], dict) and isinstance(value['signature'], str)
            and HASH.fullmatch(value['signature']), 'invalid binding repair signed envelope')
    return control.verify(value, key)


def binding_pins(value):
    require(isinstance(value, dict) and set(value) == PINS
            and all(isinstance(v, str) and HASH.fullmatch(v) for v in value.values()),
            'four exact installed binding hashes required')
    return value


def original_identity(original, key, now):
    require(isinstance(original, dict) and set(original) == {
        'coordinator_build', 'owner_build', 'request', 'receipt'}, 'invalid original binding repair coordinates')
    require(all(type(original[n]) is int and 0 < original[n] <= 999999999
                for n in ('coordinator_build', 'owner_build')), 'invalid original binding repair builds')
    request = signed_payload(original['request'], key)
    # Validate the historical request at its original issuance, without
    # extending its deployment permission or re-running its expired gates.
    deploy.request_identity(original['request'], key, gate.timestamp(request['created_at']))
    require(gate.SHA.fullmatch(request.get('library_revision', ''))
            and request.get('deployment_script_revision') == request['commit'],
            'original binding repair library or source not pinned')
    require('mode' not in request and 'recovery' not in request, 'binding repair requires normal original deployment')
    receipt = signed_payload(original['receipt'], key)
    require(receipt.get('schema_version') == 1 and receipt.get('kind') == 'runtime-deployment'
            and receipt.get('product') == gate.PRODUCT and receipt.get('status') == 'SUCCESS'
            and receipt.get('automatic_rollback') is False
            and 'mode' not in receipt and 'recovery' not in receipt and not receipt.get('app_binding')
            and receipt.get('request_sha256') == sha(original['request']),
            'binding repair requires original successful unbound owner receipt')
    for field in ('commit', 'version', 'digest', 'deployment_script_revision', 'library_revision'):
        require(receipt.get(field) == request.get(field), 'original binding repair release mismatch')
    observed = receipt.get('observed', {})
    require(isinstance(observed, dict) and observed.get('heartbeat') == 200
            and observed.get('compose_project') == deploy.PROJECT
            and HASH.fullmatch(observed.get('container_id', ''))
            and gate.DIGEST.fullmatch(observed.get('image_id', ''))
            and HASH.fullmatch(receipt.get('previous_container_id', ''))
            and gate.DIGEST.fullmatch(receipt.get('previous_image_id', ''))
            and observed['container_id'] != receipt['previous_container_id'],
            'original binding repair runtime observation invalid')
    require(all(observed.get(n) == request[n] for n in ('commit', 'version', 'digest')),
            'original binding repair observation mismatch')
    issued, expires = gate.timestamp(request['created_at']), gate.timestamp(request['expires_at'])
    started, finished = gate.timestamp(receipt['started_at']), gate.timestamp(receipt['finished_at'])
    require(issued <= started < expires and started <= finished <= now,
            'original binding repair timestamps invalid')
    return request, receipt


def request_identity(signed, key, now, library_revision):
    request = signed_payload(signed, key)
    require(set(request) == {'schema_version', 'kind', 'product', 'library_revision', 'created_at',
                            'expires_at', 'operation_id', 'authorized_by', 'coordinator_build',
                            'original', 'binding_hashes'}, 'invalid binding repair request fields')
    require(request['schema_version'] == 1 and request['kind'] == 'app-binding-repair'
            and request['product'] == gate.PRODUCT and gate.SHA.fullmatch(library_revision)
            and request['library_revision'] == library_revision, 'binding repair library or product mismatch')
    require(isinstance(request['operation_id'], str)
            and str(uuid.UUID(request['operation_id'], version=4)) == request['operation_id']
            and uuid.UUID(request['operation_id']).version == 4, 'invalid binding repair operation ID')
    require(isinstance(request['authorized_by'], str)
            and re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', request['authorized_by'])
            and type(request['coordinator_build']) is int and 0 < request['coordinator_build'] <= 999999999,
            'binding repair authorization missing')
    issued, expires = gate.timestamp(request['created_at']), gate.timestamp(request['expires_at'])
    require(issued <= now < expires and 0 < (expires - issued).total_seconds() <= 900,
            'binding repair request expired or invalid')
    binding_pins(request['binding_hashes'])
    original_identity(request['original'], key, now)
    return request


def prefix(job, number):
    require(job in {COORDINATOR, OWNER} and type(number) is int and 0 < number <= 999999999,
            'invalid binding repair Jenkins coordinate')
    return '/' + '/'.join('job/' + part for part in job.split('/')) + '/' + str(number) + '/'


def upstream(build, job, number):
    causes = [c for a in build.get('actions', []) for c in a.get('causes', [])]
    require(len(causes) == 1 and causes[0].get('_class') in {
        'hudson.model.Cause$UpstreamCause',
        'org.jenkinsci.plugins.workflow.support.steps.build.BuildUpstreamCause'}
        and causes[0].get('upstreamProject') == job and causes[0].get('upstreamBuild') == number,
        'binding repair original upstream mismatch')


def collect(base, coordinator_build, owner_build, hashes, policy, key, now, author, current_build):
    require(author in policy['approvers'], 'binding repair author not approved')
    require(gate.SHA.fullmatch(policy['library_revision']), 'binding repair library not pinned')
    binding_pins(hashes)
    cp, op = prefix(COORDINATOR, coordinator_build), prefix(OWNER, owner_build)
    def get(path):
        return json.loads(adapter.jenkins_get(base, path), object_pairs_hook=pairs)
    coord, owner = get(cp + 'api/json'), get(op + 'api/json')
    for build, number in ((coord, coordinator_build), (owner, owner_build)):
        require(build.get('number') == number and build.get('building') is False
                and build.get('result') == 'SUCCESS', 'binding repair original builds must be successful')
    upstream(owner, COORDINATOR, coordinator_build)
    original = {'coordinator_build': coordinator_build, 'owner_build': owner_build,
                'request': get(cp + 'artifact/deployment-request.json'),
                'receipt': get(op + 'artifact/runtime-receipt.json')}
    original_identity(original, key, now)
    payload = {'schema_version': 1, 'kind': 'app-binding-repair', 'product': gate.PRODUCT,
               'library_revision': policy['library_revision'], 'created_at': now.isoformat(),
               'expires_at': (now + dt.timedelta(seconds=900)).isoformat(),
               'operation_id': str(uuid.uuid4()), 'authorized_by': author,
               'coordinator_build': current_build, 'original': original,
               'binding_hashes': binding_pins(hashes)}
    signed = control.sign(payload, key)
    request_identity(signed, key, now, policy['library_revision'])
    return signed


def state_key(request):
    # A different authorization UUID must not replay this same binding repair.
    return sha({'kind': 'app-binding-repair', 'receipt_sha256': sha(request['original']['receipt']),
                'binding_hashes': request['binding_hashes']})[:40]


def execute(signed, key, runtime, state, engine_id, now, library_revision, output):
    request = request_identity(signed, key, now, library_revision)
    original_request, receipt = original_identity(request['original'], key, now)
    require(sys.platform == 'darwin', 'binding repair requires configured macOS owner')
    require(runtime.is_absolute() and runtime.resolve() == runtime and runtime.is_dir(),
            'invalid binding repair fixed runtime root')
    binding = module('binding_repair_transaction', 'release-app-binding.py')
    lifecycle = binding.NativeIO()
    binding.directory(runtime / 'data/locks')
    binding.directory(state.root)
    state_info = state.root.lstat()
    require(state_info.st_uid == os.getuid() and stat.S_IMODE(state_info.st_mode) == 0o700,
            'binding repair state directory must be private and owned')
    fd = os.open(runtime / 'data/locks/prod.lock', os.O_RDWR | os.O_NOFOLLOW)
    try:
        held = os.fstat(fd)
        require(stat.S_ISREG(held.st_mode) and held.st_uid == os.getuid() and stat.S_IMODE(held.st_mode) == 0o600,
                'unsafe binding repair lifecycle descriptor')
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with state.lock():
            require(state.exists(receipt['commit'])
                    and gate.canonical(state.read(receipt['commit'])) == gate.canonical(request['original']['receipt']),
                    'original owner state changed or unavailable')
            claim = state_key(request)
            require(not state.exists(claim), 'binding repair already claimed; inspect preserved evidence')
            request_identity(signed, key, dt.datetime.now(dt.timezone.utc), library_revision)
            record = {'schema_version': 1, 'kind': 'app-binding-maintenance', 'product': gate.PRODUCT,
                      'status': 'CLAIMED', 'operation_id': request['operation_id'], 'request_sha256': sha(signed),
                      'original_owner_build': request['original']['owner_build'],
                      'original_receipt_sha256': sha(request['original']['receipt']),
                      'commit': receipt['commit'], 'version': receipt['version'], 'digest': receipt['digest'],
                      'library_revision': library_revision, 'started_at': now.isoformat(),
                      'automatic_retry': False, 'automatic_rollback': False, 'app_deployed': False}
            state.write(claim, control.sign(record, key))
            try:
                require(deploy.run(['docker', 'info', '--format', '{{.ID}}']) == engine_id and engine_id,
                        'binding repair Docker engine mismatch')
                actual = deploy.target_container(deploy.containers(), runtime)
                require(actual['Id'] == receipt['observed']['container_id']
                        and actual['Image'] == receipt['observed']['image_id']
                        and deploy.image_identity(original_request) == actual['Image'],
                        'binding repair current App differs from signed receipt')
                verified = {name: original_request[name] for name in ('commit', 'version', 'digest', 'image')}
                verified.update(operation_id=request['operation_id'],
                                previous_container_id=receipt['previous_container_id'],
                                previous_image_id=receipt['previous_image_id'])
                repaired = binding.AppBinding.repair(verified, copy.deepcopy(receipt['observed']), runtime,
                                                      fd, request['binding_hashes'], io=lifecycle)
                require(repaired.get('status') == 'completed', 'binding repair did not complete')
                record.update(status='SUCCESS', app_binding=repaired)
            except BaseException as error:
                record.update(status='FAILED', mutation_may_have_started=True,
                              follow_up='Preserve pending, owner state and backup; no automatic retry or rollback')
                if isinstance(getattr(error, 'receipt', None), dict):
                    record['app_binding'] = error.receipt
                raise
            finally:
                record['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
                signed_record = control.sign(record, key)
                state.write(claim, signed_record)
                write(output, signed_record)
            return signed_record
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['collect', 'inspect', 'repair'])
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--pins', type=Path)
    parser.add_argument('--jenkins-url')
    for name in ('coordinator-build', 'owner-build', 'current-build'):
        parser.add_argument('--' + name, type=int)
    parser.add_argument('--author')
    parser.add_argument('--library-revision')
    parser.add_argument('--runtime-root', type=Path)
    parser.add_argument('--state-directory', type=Path)
    parser.add_argument('--docker-engine-id')
    args = parser.parse_args()
    try:
        key, now = args.key_file.read_bytes(), dt.datetime.now(dt.timezone.utc)
        if args.command == 'collect':
            write(args.output, collect(args.jenkins_url, args.coordinator_build, args.owner_build,
                                      read(args.pins), read(args.policy), key, now, args.author, args.current_build))
        elif args.command == 'inspect':
            value = request_identity(read(args.request), key, now, args.library_revision)
            target, _ = original_identity(value['original'], key, now)
            write(args.output, dict(kind=value['kind'], coordinator_build=value['coordinator_build'],
                                    **{n: target[n] for n in ('commit', 'version', 'digest')}))
        else:
            execute(read(args.request), key, args.runtime_root, control.State(args.state_directory),
                    args.docker_engine_id, now, args.library_revision, args.output)
    except Exception as error:
        print('BLOCKED: ' + (str(error) if isinstance(error, gate.InvalidEvidence)
                            else 'binding repair failed; preserve evidence'), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
