#!/usr/bin/env python3
"""Offline security contract tests. No Jenkins, GitHub, Docker, or live DB."""
import copy
import datetime as dt
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("release-gate.py")
spec = importlib.util.spec_from_file_location("release_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc)
        self.digest = "sha256:" + "a" * 64
        self.key = b"offline-test-only-key" * 2
        self.policy = {
            "schema_version": 1, "product": gate.PRODUCT,
            "jobs": {"promotion": "app/develop", "deployment": "app/prod"},
            "required_stages": {"promotion": ["Build", "Scan", "Reports"],
                                "deployment": ["Build", "Scan", "Reports", "Finalize"]},
            "required_scanners": ["trivy", "govulncheck", "harbor"],
            "max_evidence_age_seconds": 3600, "max_exception_seconds": 600,
            "approvers": ["test-approver"], "revoked_approval_ids": [],
        }
        self.evidence = {
            "schema_version": 1, "product": gate.PRODUCT, "gate": "promotion",
            "branch": "develop", "event": "branch", "trusted": True,
            "commit": "b" * 40, "image_digest": self.digest, "job": "app/develop", "build": 1,
            "building": False, "post_complete": True, "result": "SUCCESS",
            "completed_at": self.now.isoformat(),
            "stages": [{"name": name, "result": "SUCCESS"} for name in ["Build", "Scan", "Reports"]],
            "reports": [],
        }
        self.native = {
            "trivy": {"SchemaVersion": 2, "Metadata": {"RepoDigests": ["registry/app@" + self.digest]},
                      "Results": [{"Target": "app", "Vulnerabilities": []}]},
            "govulncheck": {"messages": [{"config": {"scanner_version": "test"}}]},
            "harbor": {"artifact": {"digest": self.digest}, "report": {"vulnerabilities": []}},
        }
        self.write_reports()

    def write(self, name, value):
        raw = gate.canonical(value)
        (self.root / name).write_bytes(raw)
        return {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}

    def write_reports(self):
        self.evidence["reports"] = []
        for scanner, native in self.native.items():
            report = {"schema_version": 1, "scanner": scanner, "complete": True,
                      "unfiltered": True, "scanner_version": "test", "database_revision": "test-db",
                      "commit": self.evidence["commit"], "image_digest": self.digest,
                      "severities": sorted(gate.SEVERITIES), "completed_at": self.now.isoformat(),
                      "native_report": self.write(scanner + "-native.json", native)}
            self.evidence["reports"].append({"scanner": scanner, **self.write(scanner + ".json", report)})

    def evaluate(self, **kwargs):
        return gate.evaluate(self.evidence, self.policy, self.root, self.now, **kwargs)

    def vulnerable(self, severity="LOW"):
        self.native["trivy"]["Results"][0]["Vulnerabilities"] = [{
            "VulnerabilityID": "CVE-TEST-0001", "PkgName": "example", "InstalledVersion": "1",
            "Severity": severity, "FixedVersion": "",
        }]
        self.write_reports()

    def approval(self):
        findings = []
        for scanner, native in self.native.items():
            findings.extend(gate.native_findings(scanner, native, self.digest))
        payload = {
            "id": "test-exception", "approver": "test-approver", "reason": "explicit fixture approval",
            "issued_at": self.now.isoformat(), "expires_at": (self.now + dt.timedelta(minutes=5)).isoformat(),
            "evidence_sha256": hashlib.sha256(gate.canonical(self.evidence)).hexdigest(),
            "policy_sha256": hashlib.sha256(gate.canonical(self.policy)).hexdigest(),
            "finding_keys": sorted(set(gate.finding_key(f) for f in findings)),
        }
        return self.sign(payload)

    def sign(self, payload):
        return {"payload": payload, "signature": hmac.new(self.key, gate.canonical(payload), hashlib.sha256).hexdigest()}

    def test_clean_completed_build_passes(self):
        self.assertEqual(self.evaluate()["decision"], "PASS")

    def test_each_non_success_run_blocks(self):
        for result in ["UNSTABLE", "FAILURE", "ABORTED", "NOT_BUILT", None]:
            with self.subTest(result=result), self.assertRaises(gate.InvalidEvidence):
                self.evidence["result"] = result
                self.evaluate()

    def test_incomplete_post_or_running_build_blocks(self):
        for field, value in [("building", True), ("post_complete", False), ("trusted", False),
                             ("event", "pr"), ("branch", "prod"), ("commit", "short"),
                             ("job", "other/develop"), ("build", True), ("image_digest", "latest")]:
            original = self.evidence[field]
            with self.subTest(field=field), self.assertRaises(gate.InvalidEvidence):
                self.evidence[field] = value
                self.evaluate()
            self.evidence[field] = original

    def test_skipped_missing_extra_failed_and_duplicate_stages_block(self):
        original = copy.deepcopy(self.evidence["stages"])
        variants = [original[:-1], original + [original[0]],
                    original + [{"name": "Cleanup", "result": "FAILURE"}],
                    [{"name": s["name"], "result": "SKIPPED"} for s in original]]
        for stages in variants:
            with self.subTest(stages=stages), self.assertRaises(gate.InvalidEvidence):
                self.evidence["stages"] = stages
                self.evaluate()

    def test_all_severities_and_unfixed_vulnerabilities_block(self):
        for severity in gate.SEVERITIES:
            with self.subTest(severity=severity), self.assertRaises(gate.InvalidEvidence):
                self.vulnerable(severity)
                self.evaluate()

    def test_exact_signed_exception_passes(self):
        self.vulnerable()
        self.assertEqual(self.evaluate(approval=self.approval(), approval_key=self.key)["decision"],
                         "APPROVED_EXCEPTION")

    def test_bad_signature_blocks(self):
        self.vulnerable()
        approval = self.approval()
        approval["payload"]["reason"] = "tampered"
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate(approval=approval, approval_key=self.key)

    def test_expired_future_unauthorized_overlong_and_broad_approval_block(self):
        self.vulnerable()
        variants = [("expires_at", self.now.isoformat()), ("approver", "outsider"),
                    ("reason", ""), ("finding_keys", ["*"]),
                    ("issued_at", (self.now + dt.timedelta(minutes=1)).isoformat()),
                    ("expires_at", (self.now + dt.timedelta(days=1)).isoformat())]
        for field, value in variants:
            payload = self.approval()["payload"]
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(gate.InvalidEvidence):
                self.evaluate(approval=self.sign(payload), approval_key=self.key)

    def test_changed_evidence_and_policy_invalidate_approval(self):
        self.vulnerable()
        approval = self.approval()
        self.evidence["build"] += 1
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate(approval=approval, approval_key=self.key)
        self.evidence["build"] -= 1
        self.policy["revoked_approval_ids"].append("test-exception")
        # Re-sign with current policy to test revocation as well as policy binding.
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate(approval=self.approval(), approval_key=self.key)

    def test_same_approval_cannot_cross_to_prod(self):
        self.vulnerable()
        approval = self.approval()
        self.evidence.update(gate="deployment", branch="prod", job="app/prod")
        self.evidence["stages"].append({"name": "Finalize", "result": "SUCCESS"})
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate(approval=approval, approval_key=self.key)

    def test_native_report_tamper_blocks(self):
        (self.root / "trivy-native.json").write_text('{"Results":[]}')
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()

    def test_envelope_cannot_hide_native_vulnerabilities(self):
        self.vulnerable()
        record = self.evidence["reports"][0]
        envelope = json.loads((self.root / record["path"]).read_bytes())
        envelope["findings"] = []
        record.update(self.write(record["path"], envelope))
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()

    def test_report_missing_filtered_wrong_digest_and_severity_coverage_block(self):
        for field, value in [("complete", False), ("unfiltered", False), ("image_digest", "bad"),
                             ("severities", ["HIGH", "CRITICAL"]), ("scanner_version", "")]:
            self.write_reports()
            record = self.evidence["reports"][0]
            envelope = json.loads((self.root / record["path"]).read_bytes())
            envelope[field] = value
            record.update(self.write(record["path"], envelope))
            with self.subTest(field=field), self.assertRaises(gate.InvalidEvidence):
                self.evaluate()

    def test_missing_scanner_blocks(self):
        self.evidence["reports"].pop()
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()

    def test_path_escape_blocks(self):
        self.evidence["reports"][0]["path"] = "../outside.json"
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()

    def test_stale_evidence_blocks(self):
        self.evidence["completed_at"] = (self.now - dt.timedelta(hours=2)).isoformat()
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()

    def test_harbor_low_blocks(self):
        self.native["harbor"]["report"]["vulnerabilities"] = [
            {"id": "CVE-TEST-0002", "package": "example", "version": "1", "severity": "Low"}]
        self.write_reports()
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()

    def test_unreachable_go_osv_blocks(self):
        self.native["govulncheck"]["messages"].append({"osv": {
            "id": "GO-TEST-0001", "affected": [{"package": {"name": "example/module"}}]}})
        self.write_reports()
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()

    def configure_openpgp_not_applicable(self):
        self.policy["not_applicable_advisories"] = [copy.deepcopy(gate.NOT_APPLICABLE_RULE)]
        graph = {"schema_version": 1, "complete": True, "commit": self.evidence["commit"],
                 "go_version": "go version go1.26.6 linux/arm64", "graphs": []}
        for name in gate.NOT_APPLICABLE_RULE["required_package_graphs"]:
            graph["graphs"].append({"name": name, "goos": "linux", "goarch": "arm64",
                "cgo_enabled": "0", "tags": "nodynamic", "test": name.endswith("tests"),
                "target": "./...", "packages": ["example/app", "golang.org/x/crypto/argon2"]})
        self.evidence["package_graph"] = self.write("package-graphs.json", graph)
        self.native["govulncheck"]["messages"].append({"osv": {"id": "GO-2026-5932",
            "affected": [{"package": {"name": "golang.org/x/crypto"}}]}})
        self.write_reports()
        return graph

    def test_approved_openpgp_not_applicable_rule_passes_with_exact_graph_evidence(self):
        self.configure_openpgp_not_applicable()
        result = self.evaluate()
        self.assertEqual(result["decision"], "PASS")
        self.assertEqual(result["not_applicable"], ["GO-2026-5932"])

    def test_openpgp_rule_fails_closed_for_package_use_or_govuln_finding(self):
        graph = self.configure_openpgp_not_applicable()
        graph["graphs"][0]["packages"].append("golang.org/x/crypto/openpgp/packet")
        self.evidence["package_graph"] = self.write("package-graphs-affected.json", graph)
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()
        graph["graphs"][0]["packages"].pop()
        self.evidence["package_graph"] = self.write("package-graphs-safe.json", graph)
        self.native["govulncheck"]["messages"].append({"finding": {"osv": "GO-2026-5932",
            "trace": [{"module": "golang.org/x/crypto", "package": "golang.org/x/crypto/openpgp"}]}})
        self.write_reports()
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()

    def test_native_digest_mismatch_blocks(self):
        self.native["harbor"]["artifact"]["digest"] = "sha256:" + "f" * 64
        self.write_reports()
        with self.assertRaises(gate.InvalidEvidence):
            self.evaluate()

    def test_cli_malformed_evidence_is_blocked_without_echoing_input(self):
        evidence = self.root / "evidence.json"
        evidence.write_text("fake-sensitive-string-not-json")
        result = subprocess.run([sys.executable, str(SCRIPT), "--evidence", str(evidence),
                                 "--policy", str(evidence)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["decision"], "BLOCKED")
        self.assertNotIn("fake-sensitive", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
