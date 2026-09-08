#!/usr/bin/env python3
"""Pure release decision evaluator; never merges, deploys, or changes Jenkins.

Evidence/policy must be assembled by the trusted release coordinator after the
upstream run has completed. This evaluator is not an authentication boundary.
Exception signatures are supplied by that coordinator, never by SCM input.
"""

import argparse
import datetime as dt
import hashlib
import hmac
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


PRODUCT = "shiba-go-ditch-api-project"
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
WAIVABLE_STAGES = {'Test', 'Fast Contract Test', 'Dependency Scan', 'Image Scan'}
CANDIDATE_STAGES = WAIVABLE_STAGES | {'Harbor Vulnerability Report'}
NOT_APPLICABLE_RULE = {
    "id": "GO-2026-5932",
    "affected_package_prefix": "golang.org/x/crypto/openpgp",
    "required_package_graphs": ["linux-arm64-nodynamic-tests",
                                "linux-arm64-devseed-nodynamic-tests",
                                "linux-arm64-nodynamic-server"],
    "require_no_govuln_affected_package_finding": True,
}


class InvalidEvidence(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise InvalidEvidence(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def timestamp(value):
    require(isinstance(value, str), "missing timestamp")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "timestamp must include timezone")
    return parsed


def finding_key(finding):
    fields = ("scanner", "id", "package", "version", "target")
    require(all(isinstance(finding.get(f), str) and finding[f] for f in fields),
            "incomplete finding identity")
    return hashlib.sha256(canonical({f: finding[f] for f in fields})).hexdigest()


def native_findings(scanner, native, digest):
    """Derive findings from native bytes, not a caller-supplied zero count."""
    findings = []
    if scanner == "trivy":
        require(native.get("SchemaVersion") == 2, "unsupported Trivy schema")
        require(any(ref.endswith("@" + digest) for ref in native["Metadata"]["RepoDigests"]),
                "Trivy native digest mismatch")
        require(isinstance(native.get("Results"), list) and native["Results"], "missing Trivy targets")
        for target in native["Results"]:
            vulnerabilities = target.get("Vulnerabilities", [])
            require(isinstance(vulnerabilities, list), "invalid Trivy vulnerabilities")
            for vuln in vulnerabilities:
                findings.append({"scanner": scanner, "id": vuln["VulnerabilityID"],
                                 "package": vuln["PkgName"], "version": vuln["InstalledVersion"],
                                 "target": target["Target"], "severity": vuln["Severity"].upper()})
    elif scanner == "harbor":
        require(native.get("artifact", {}).get("digest") == digest, "Harbor native digest mismatch")
        report = native["report"]
        if "application/vnd.security.vulnerability.report; version=1.1" in report:
            report = report["application/vnd.security.vulnerability.report; version=1.1"]
        require(isinstance(report.get("vulnerabilities"), list), "missing Harbor vulnerabilities")
        for vuln in report["vulnerabilities"]:
            findings.append({"scanner": scanner, "id": vuln["id"], "package": vuln["package"],
                             "version": vuln["version"], "target": digest,
                             "severity": vuln["severity"].upper()})
    elif scanner == "govulncheck":
        # govulncheck emits JSON objects as a stream; the collector stores that
        # unmodified sequence in messages. OSV messages are advisory metadata;
        # only explicit finding messages describe this build's modules/packages.
        messages = native.get("messages")
        require(isinstance(messages, list) and any("config" in m for m in messages),
                "missing govulncheck config")
        osvs = {m["osv"]["id"]: m["osv"] for m in messages if "osv" in m}
        for message in messages:
            if "finding" not in message:
                continue
            found = message["finding"]
            require(found["osv"] in osvs, "finding missing OSV evidence")
            trace = found.get("trace")
            require(isinstance(trace, list) and trace, "finding missing trace evidence")
            identified = False
            for frame in trace:
                if frame.get("module"):
                    findings.append({"scanner": scanner, "id": found["osv"],
                                     "package": frame["module"], "version": frame.get("version") or "unknown",
                                     "target": "source", "severity": "UNKNOWN"})
                    identified = True
            require(identified, "finding missing module identity")
    else:
        raise InvalidEvidence("unsupported scanner")
    return findings


def verified_bytes(root, record):
    relative = Path(record["path"])
    require(not relative.is_absolute() and ".." not in relative.parts, "unsafe report path")
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), "report escapes evidence directory")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == record["sha256"], "report checksum mismatch")
    return raw


def verified_report(root, record):
    raw = verified_bytes(root, record)
    value = json.loads(raw)
    require(isinstance(value, dict), "report must be a JSON object")
    return value


def stage_findings(evidence, policy, root):
    if evidence.get('mode') != 'controlled-candidate-v1':
        return []
    require(policy.get('candidate_mode') is True and set(policy.get('waivable_stages', [])) == WAIVABLE_STAGES,
            'candidate stage policy is missing or weakened')
    records = [verified_report(root, record) for record in evidence['stage_checks']]
    require(len(records) == len(CANDIDATE_STAGES) and {r['stage'] for r in records} == CANDIDATE_STAGES,
            'missing or duplicate candidate stage evidence')
    observed = {s['name']: s['result'] for s in evidence['stages']}
    findings = []
    had_findings = False
    for record in records:
        name = record['stage']
        require(record.get('schema_version') == 1 and record.get('complete') is True and record.get('commit') == evidence['commit']
                and record.get('job') == evidence['job'] and record.get('build') == evidence['build'], 'stage evidence identity mismatch')
        rc = record['exit_code']
        require(type(rc) is int and rc in {0, 1}, 'stage execution error cannot be waived')
        reports = [verified_bytes(root, ref) for ref in record['reports']]
        require(reports and all(reports), 'missing stage report')
        count = 0
        if name == 'Test':
            require(len(reports) == 2, 'test assertions report missing')
            xml = ET.fromstring(reports[1])
            cases = list(xml.iter('testcase'))
            require(cases and not list(xml.iter('error')) and any(c.find('skipped') is None for c in cases), 'test execution incomplete')
            count = len(list(xml.iter('failure')))
            require(rc == 0 or count > 0, 'test command failed without assertion evidence')
        elif name == 'Fast Contract Test':
            count = rc
        elif name == 'Dependency Scan':
            require(rc == 0, 'govulncheck execution failed')
            # Decode the entire stream; a valid prefix cannot hide truncation.
            decoder, offset, messages, raw = json.JSONDecoder(), 0, [], reports[0].decode()
            while offset < len(raw):
                if raw[offset].isspace(): offset += 1; continue
                value, offset = decoder.raw_decode(raw, offset)
                require(isinstance(value, dict), 'invalid govulncheck stream')
                messages.append(value)
            require(sum('config' in m for m in messages) == 1, 'missing govulncheck config')
            count = sum('finding' in m for m in messages)
        elif name == 'Image Scan':
            require(rc == 0, 'Trivy execution failed')
            native = json.loads(reports[0])
            require(native.get('SchemaVersion') == 2 and native.get('Results'), 'incomplete image report')
            count = sum(len(item.get('Vulnerabilities', [])) for item in native['Results'])
        else:
            require(rc == 0, 'Harbor execution failed')
        require(record['finding_count'] == count and record['outcome'] == ('WAIVER_REQUIRED' if count else 'PASS'), 'stage report outcome mismatch')
        require(observed[name] == ('UNSTABLE' if count else 'SUCCESS'), 'stage status differs from native evidence')
        if count:
            had_findings = True
            require(name in WAIVABLE_STAGES, 'stage cannot be waived')
            # Scanner-backed stages prove execution/status consistency. Their
            # native findings are reviewed below, so do not create a duplicate
            # broad stage waiver alongside each exact vulnerability.
            if name in {'Test', 'Fast Contract Test'}:
                findings.append({'scanner': 'stage', 'id': name, 'package': evidence['job'], 'version': evidence['commit'],
                    'target': str(evidence['build']) + ':' + hashlib.sha256(canonical(record)).hexdigest(), 'severity': 'UNKNOWN', 'kind': 'stage'})
    require(evidence['result'] == ('UNSTABLE' if had_findings else 'SUCCESS'), 'unexplained candidate build result')
    return findings


def verified_not_applicable(evidence, policy, root, native_reports, findings):
    """Return exact advisory IDs proven outside this build's package graph.

    Raw scanner findings remain archived.  This only implements the product's
    explicitly approved GO-2026-5932 rule and fails closed if graph or
    govulncheck evidence is missing, changed, or reachable.
    """
    candidate_ids = {item["id"] for item in findings}
    if NOT_APPLICABLE_RULE["id"] not in candidate_ids:
        return set()
    rules = policy.get("not_applicable_advisories")
    require(isinstance(rules, list) and rules == [NOT_APPLICABLE_RULE],
            "not-applicable policy is missing or changed")
    graph = verified_report(root, evidence.get("package_graph", {}))
    require(graph.get("schema_version") == 1 and graph.get("complete") is True
            and graph.get("commit") == evidence["commit"] and graph.get("go_version"),
            "package graph evidence is incomplete")
    graphs = graph.get("graphs")
    require(isinstance(graphs, list) and len(graphs) == len(NOT_APPLICABLE_RULE["required_package_graphs"]),
            "package graph set is incomplete")
    require({item.get("name") for item in graphs} == set(NOT_APPLICABLE_RULE["required_package_graphs"]),
            "package graph identity mismatch")
    prefix = NOT_APPLICABLE_RULE["affected_package_prefix"]
    for item in graphs:
        require(item.get("goos") == "linux" and item.get("goarch") == "arm64"
                and item.get("cgo_enabled") == "0" and item.get("tags")
                and isinstance(item.get("test"), bool) and item.get("target")
                and isinstance(item.get("packages"), list) and item["packages"],
                "package graph build conditions are incomplete")
        require(all(isinstance(pkg, str) and pkg and pkg != prefix and not pkg.startswith(prefix + "/")
                    for pkg in item["packages"]), "affected openpgp package is in the build graph")
    go_native = native_reports.get("govulncheck", {})
    messages = go_native.get("messages")
    require(isinstance(messages, list), "govulncheck evidence missing for not-applicable rule")
    affected_findings = []
    for message in messages:
        finding = message.get("finding", {})
        if finding.get("osv") != NOT_APPLICABLE_RULE["id"]:
            continue
        trace = finding.get("trace") or []
        if any(frame.get("package") == prefix or str(frame.get("package", "")).startswith(prefix + "/")
               for frame in trace if isinstance(frame, dict)):
            affected_findings.append(finding)
    require(not affected_findings, "govulncheck reports the affected openpgp package")
    return {NOT_APPLICABLE_RULE["id"]}


def evaluate(evidence, policy, root, now, approval=None, approval_key=None):
    """Fail closed; callers must also authenticate the evidence and policy source."""
    require(evidence.get("schema_version") == 1 and policy.get("schema_version") == 1,
            "unsupported schema")
    require(evidence.get("product") == policy.get("product") == PRODUCT, "wrong product")
    gate = evidence.get("gate")
    require(gate in {"promotion", "deployment"}, "unsupported gate")
    require(evidence.get("branch") == {"promotion": "develop", "deployment": "prod"}[gate],
            "wrong branch")
    require(evidence.get("event") == "branch" and evidence.get("trusted") is True,
            "untrusted event")
    require(SHA.fullmatch(evidence.get("commit", "")) is not None, "invalid commit")
    require(DIGEST.fullmatch(evidence.get("image_digest", "")) is not None, "invalid digest")
    require(type(evidence.get("build")) is int and evidence["build"] > 0, "invalid build")
    require(evidence.get("job") == policy["jobs"][gate], "wrong job")
    require(evidence.get("building") is False and evidence.get("post_complete") is True,
            "build or post processing not complete")
    # A failed run may have skipped necessary work. Approval never fabricates
    # missing artifacts: resume in a separately authorized verification run.
    candidate = evidence.get('mode') == 'controlled-candidate-v1'
    require(not policy.get('candidate_mode') or candidate, 'controlled candidate evidence required')
    if candidate:
        require(verified_bytes(root, evidence['artifact']), 'candidate artifact missing')
    require(evidence.get("result") in ({'SUCCESS', 'UNSTABLE'} if candidate else {'SUCCESS'}), "upstream run requires revalidation")
    required = policy["required_stages"][gate]
    require(isinstance(required, list) and required and len(set(required)) == len(required),
            "invalid required stage policy")
    stages = evidence["stages"]
    require(isinstance(stages, list) and stages, "missing stages")
    names = [stage["name"] for stage in stages]
    require(len(set(names)) == len(names), "duplicate stages")
    require(set(required).issubset(names), "missing required stage")
    require(all(stage['result'] == 'SUCCESS' or candidate and stage['name'] in WAIVABLE_STAGES and stage['result'] == 'UNSTABLE'
                for stage in stages), 'non-success stage')
    age = (now - timestamp(evidence["completed_at"])).total_seconds()
    require(0 <= age <= policy["max_evidence_age_seconds"], "stale or future evidence")
    reports = evidence["reports"]
    require(isinstance(reports, list) and reports, "missing reports")
    scanners = [report["scanner"] for report in reports]
    require(len(scanners) == len(set(scanners)), "duplicate scanner report")
    require(set(scanners) == set(policy["required_scanners"]), "scanner set mismatch")
    require(set(policy["required_scanners"]) == {"trivy", "govulncheck", "harbor"},
            "required scanner policy cannot be weakened")
    findings = {finding_key(item): item for item in stage_findings(evidence, policy, root)}
    native_reports = {}
    for record in reports:
        report = verified_report(root, record)
        # The adapter must retain native report bytes alongside this normalized
        # envelope and bind their hash. Empty findings alone never prove success.
        require(report.get("schema_version") == 1, "invalid report schema")
        require(report.get("scanner") == record["scanner"], "scanner identity mismatch")
        require(report.get("complete") is True and report.get("unfiltered") is True,
                "incomplete or filtered report")
        require(report.get("scanner_version") and report.get("database_revision"),
                "missing scanner provenance")
        require(report.get("commit") == evidence["commit"], "report commit mismatch")
        require(report.get("image_digest") == evidence["image_digest"], "report digest mismatch")
        require(set(report["severities"]) == SEVERITIES, "severity coverage incomplete")
        scan_age = (now - timestamp(report["completed_at"])).total_seconds()
        require(0 <= scan_age <= policy["max_evidence_age_seconds"], "stale scan")
        native = verified_report(root, report["native_report"])
        native_reports[record["scanner"]] = native
        require(native, "empty native report")
        for finding in native_findings(record["scanner"], native, evidence["image_digest"]):
            require(finding.get("scanner") == record["scanner"], "finding scanner mismatch")
            require(finding.get("severity") in SEVERITIES, "unknown severity encoding")
            findings[finding_key(finding)] = finding
    not_applicable = verified_not_applicable(evidence, policy, root, native_reports, findings.values())
    findings = {key: item for key, item in findings.items() if item["id"] not in not_applicable}
    if not findings:
        require(approval is None, "unexpected approval for clean evidence")
        return {"decision": "PASS", "finding_count": 0,
                "not_applicable": sorted(not_applicable)}
    require(approval is not None, "unapproved vulnerabilities")
    require(isinstance(approval_key, bytes) and len(approval_key) >= 32, "missing approval verifier")
    payload = approval["payload"]
    signature = hmac.new(approval_key, canonical(payload), hashlib.sha256).hexdigest()
    require(hmac.compare_digest(signature, approval["signature"]), "invalid approval signature")
    require(payload.get("evidence_sha256") == hashlib.sha256(canonical(evidence)).hexdigest(),
            "approval belongs to different evidence")
    require(payload.get("policy_sha256") == hashlib.sha256(canonical(policy)).hexdigest(),
            "approval belongs to different policy")
    require(payload.get("approver") in policy["approvers"], "unauthorized approver")
    require(isinstance(payload.get("reason"), str) and payload["reason"].strip(), "missing reason")
    require(isinstance(payload.get("id"), str) and payload["id"], "missing approval ID")
    require(payload["id"] not in policy["revoked_approval_ids"], "revoked approval")
    issued, expires = timestamp(payload["issued_at"]), timestamp(payload["expires_at"])
    require(issued <= now < expires, "approval expired or not yet valid")
    require(0 < (expires - issued).total_seconds() <= policy["max_exception_seconds"],
            "approval lifetime exceeds policy")
    allowed = payload["finding_keys"]
    require(isinstance(allowed, list) and len(allowed) == len(set(allowed))
            and set(allowed) == set(findings), "approval does not match exact findings")
    return {"decision": "APPROVED_EXCEPTION", "finding_count": len(findings),
            "approval_id": payload["id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--approval-key-file", type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(args.evidence.read_bytes()), json.loads(args.policy.read_bytes()),
                          args.evidence.parent, dt.datetime.now(dt.timezone.utc),
                          json.loads(args.approval.read_bytes()) if args.approval else None,
                          args.approval_key_file.read_bytes() if args.approval_key_file else None)
    except (InvalidEvidence, KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError) as exc:
        # Do not print arbitrary report content or secrets in parsing errors.
        result = {"decision": "BLOCKED", "reason": str(exc) if isinstance(exc, InvalidEvidence)
                  else "invalid or inaccessible release evidence"}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["decision"] == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
