#!/usr/bin/env python3
"""Candidate evidence only. Never signs approval, publishes a tag, or deploys."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location('candidate_evidence', Path(__file__).with_name('release-evidence.py'))
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
gate, require = adapter.gate, adapter.require
STAGES = {'Test': 'test', 'Fast Contract Test': 'contract', 'Dependency Scan': 'dependency',
          'Image Scan': 'image', 'Harbor Vulnerability Report': 'harbor'}


def record_file(root, path):
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()), 'unsafe candidate artifact')
    raw = path.read_bytes()
    require(raw, 'empty candidate artifact')
    return {'path': str(path.relative_to(root)), 'sha256': hashlib.sha256(raw).hexdigest()}


def junit_failures(raw):
    root = ET.fromstring(raw)
    require(root.tag in {'testsuite', 'testsuites'}, 'invalid test report')
    cases = list(root.iter('testcase'))
    require(cases and not list(root.iter('error')) and any(c.find('skipped') is None for c in cases), 'missing tests or test infrastructure errors')
    return len(list(root.iter('failure')))


def run_stage(root, name, command):
    require(name in STAGES and command, 'unsupported candidate stage')
    directory = root / '.pipeline/candidate-stages'
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / STAGES[name]
    env = {k: v for k, v in os.environ.items() if not k.startswith(('TRIVY_', 'GOVULN'))}
    env.update(GOVULNDB='https://vuln.go.dev', GOWORK='off', GOFLAGS='')
    if name == 'Dependency Scan':
        command = ['govulncheck', '-json', './...']
    elif name == 'Image Scan':
        data = adapter.key_values((root / '.pipeline/build.env').read_text())
        command = ['trivy', '--config', '/dev/null', 'image', '--image-src', 'docker', '--scanners', 'vuln',
                   '--format', 'json', '--exit-code', '0', '--severity', ','.join(sorted(gate.SEVERITIES)),
                   '--ignorefile', '/dev/null', '--ignore-unfixed=false', '--ignore-status', '', '--ignore-policy', '',
                   f"{data['APP_NAME']}:{data['APP_VERSION']}-{data['BUILD_NUMBER']}"]
    # Interrupted/timeout/missing tool is never a waivable test result.
    with base.with_suffix('.log').open('xb') as output, base.with_suffix('.stderr').open('xb') as errors:
        result = subprocess.run(command, cwd=root, env=env, stdout=output, stderr=errors, timeout=1800)
    raw = base.with_suffix('.log').read_bytes()
    failures = 0
    reports = [record_file(root, base.with_suffix('.log'))]
    if name == 'Test':
        report = root / 'reports/junit/go-tests.xml'
        failures = junit_failures(report.read_bytes())
        require(result.returncode in {0, 1} and (result.returncode == 0 or failures > 0), 'test command failed without assertion evidence')
        reports.append(record_file(root, report))
    elif name == 'Fast Contract Test':
        require(result.returncode in {0, 1}, 'contract test execution failed')
        failures = int(result.returncode != 0)
    elif name == 'Dependency Scan':
        require(result.returncode == 0, 'govulncheck execution failed')
        messages = adapter.stream_objects(raw.decode())
        require(sum('config' in m for m in messages) == 1, 'invalid govulncheck completion')
        # OSV messages are advisory metadata. Only finding messages indicate
        # that govulncheck matched this build's module/package/symbol graph.
        failures = sum('finding' in m for m in messages)
    elif name == 'Image Scan':
        require(result.returncode == 0, 'Trivy execution failed')
        report = json.loads(raw)
        require(report.get('SchemaVersion') == 2 and report.get('Results'), 'incomplete Trivy scan')
        failures = sum(len(item.get('Vulnerabilities', [])) for item in report['Results'])
    else:
        require(result.returncode == 0, 'Harbor execution failed')
        # Actual CVEs are collected again against the immutable pushed digest.
    value = {'schema_version': 1, 'stage': name, 'commit': os.environ['GIT_COMMIT'],
             'job': os.environ['JOB_NAME'], 'build': int(os.environ['BUILD_NUMBER']),
             'complete': True, 'exit_code': result.returncode, 'finding_count': failures,
             'outcome': 'WAIVER_REQUIRED' if failures else 'PASS', 'reports': reports}
    with base.with_suffix('.json').open('xb') as output:
        output.write(gate.canonical(value))
    print(f'{name}: {value["outcome"]}; archived candidate evidence required for release review')
    return 10 if failures else 0


def manifest(root):
    metadata = adapter.key_values((root / '.pipeline/build.env').read_text())
    require(metadata['APP_NAME'] == gate.PRODUCT and metadata['BRANCH'] in {'develop', 'prod'}, 'wrong candidate product')
    # This is the same immutable local artifact consumed by Docker Build.
    artifact = Path(metadata['ARTIFACT_LOCAL'])
    target = root / '.pipeline/candidate-artifact'
    with target.open('xb') as out:
        out.write(artifact.read_bytes())
    checks = [record_file(root, root / f'.pipeline/candidate-stages/{slug}.json') for slug in STAGES.values()]
    result = {'schema_version': 1, 'mode': 'controlled-candidate-v1', 'product': gate.PRODUCT,
              'commit': os.environ['GIT_COMMIT'], 'job': os.environ['JOB_NAME'],
              'build': int(metadata['BUILD_NUMBER']), 'branch': metadata['BRANCH'], 'version': metadata['APP_VERSION'],
              'stage_checks': checks, 'artifact': record_file(root, target), 'artifact_name': metadata['ARTIFACT_NAME']}
    with (root / '.pipeline/candidate.json').open('xb') as output:
        output.write(gate.canonical(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['run', 'manifest'])
    parser.add_argument('--stage', choices=STAGES)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        root = Path.cwd()
        if args.mode == 'manifest':
            manifest(root)
            return 0
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        return run_stage(root, args.stage, command)
    except Exception:
        print('Candidate evidence failed; not eligible for an exception', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
