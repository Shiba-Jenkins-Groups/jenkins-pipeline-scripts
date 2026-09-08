#!/usr/bin/env python3
"""Coordinator behavior with fake API payloads, scanners, and local Git only."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


fixtures = load("gate_tests", "release-gate.test.py")
adapter = load("evidence_adapter", "release-evidence.py")
promotion = load("promotion_control", "release-promotion.py")


class Controls(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReleaseGateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.policy = self.fixture.policy
        self.evidence = self.fixture.evidence
        self.now, self.key = self.fixture.now, self.fixture.key

    def build_inputs(self, branch="develop"):
        built = {"number": 188, "building": False, "result": "SUCCESS", "timestamp": 1788829200000,
                 "duration": 1000, "actions": [
                     {"lastBuiltRevision": {"SHA1": "a" * 40, "branch": [{"name": "main"}]}},
                     {"lastBuiltRevision": {"SHA1": "b" * 40, "branch": [{"name": branch}]}}]}
        stages = [{"name": name, "status": "SUCCESS"} for name in adapter.STAGES]
        stages.append({"name": adapter.FINALIZE, "status": "SUCCESS" if branch == "prod" else "NOT_EXECUTED"})
        workflow = {"id": "188", "status": "SUCCESS", "stages": stages}
        image = (f"APP_NAME={adapter.gate.PRODUCT}\nBRANCH={branch}\nBUILD_NUMBER=188\nAPP_VERSION=1.0.32\n"
                 f"IMAGE_REF=localhost:9290/{adapter.gate.PRODUCT}/{branch}/1.0.32:188\nIMAGE_DIGEST={self.fixture.digest}\n")
        return built, workflow, image

    def test_real_shape_selects_product_commit_not_library(self):
        evidence = adapter.completed_build(*self.build_inputs(), "develop", 188)
        self.assertEqual(evidence["commit"], "b" * 40)
        self.assertTrue(evidence["post_complete"])
        self.assertTrue(evidence["immutable_image"].endswith("@" + self.fixture.digest))

    def test_post_failure_or_absence_blocks(self):
        for missing in [False, True]:
            built, workflow, image = self.build_inputs()
            if missing:
                workflow["stages"] = [s for s in workflow["stages"] if s["name"] != "Declarative: Post Actions"]
            else:
                workflow["stages"][-2]["status"] = "FAILED"
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                adapter.completed_build(built, workflow, image, "develop", 188)

    def test_duplicate_product_shas_or_wrong_artifact_rejected(self):
        built, workflow, image = self.build_inputs()
        built["actions"].append({"lastBuiltRevision": {"SHA1": "f" * 40, "branch": [{"name": "develop"}]}})
        with self.assertRaises(ValueError):
            adapter.completed_build(built, workflow, image, "develop", 188)
        built, workflow, image = self.build_inputs()
        with self.assertRaises(ValueError):
            adapter.completed_build(built, workflow, image.replace("BUILD_NUMBER=188", "BUILD_NUMBER=189"), "develop", 188)

    def test_prod_needs_matching_finalization(self):
        built, workflow, image = self.build_inputs("prod")
        with self.assertRaises(ValueError):
            adapter.completed_build(built, workflow, image, "prod", 188)
        data = adapter.key_values(image)
        release = (f"GIT_COMMIT={'b' * 40}\nRELEASE_TAG=v1.0.32\nIMAGE_REF={data['IMAGE_REF']}\n"
                   f"IMAGE_DIGEST={data['IMAGE_DIGEST']}\nNEXUS_ARTIFACT_URL=http://nexus.invalid/artifact\n")
        self.assertEqual(adapter.completed_build(built, workflow, image, "prod", 188, release)["gate"], "deployment")

    def test_duplicate_image_metadata_blocks(self):
        with self.assertRaises(ValueError):
            adapter.key_values("IMAGE_DIGEST=x\nIMAGE_DIGEST=y")

    def test_prod_routing_requires_complete_trigger_suppression(self):
        valid = '<project><properties><jenkins.branch.NoTriggerBranchProperty><strategy>NONE</strategy><triggeredBranchesRegex>^$</triggeredBranchesRegex></jenkins.branch.NoTriggerBranchProperty></properties></project>'
        adapter.prod_routing(valid)
        for invalid in ['<project/>', valid.replace('NONE', 'INDEXING'), valid.replace('^$', '.*')]:
            with self.subTest(xml=invalid), self.assertRaises(ValueError):
                adapter.prod_routing(invalid)

    def test_stream_parser_rejects_truncated_success_prefix(self):
        self.assertEqual(len(adapter.stream_objects('{"config": {}}\n {"osv": {}}')), 2)
        for raw in ['', '{"config": {}}\n {"osv":', '[]']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                adapter.stream_objects(raw)

    def test_runtime_readiness_blocks_missing_disabled_offline_or_ambiguous_node(self):
        job = {'fullName': adapter.gate.PRODUCT + '-prod-deploy', 'buildable': True}
        node = {'assignedLabels': [{'name': 'mac-prod'}], 'offline': False, 'temporarilyOffline': False, 'numExecutors': 1}
        adapter.runtime_ready(job, {'computer': [node]}, 'mac-prod')
        for computers in [[], [node, node], [dict(node, offline=True)], [dict(node, numExecutors=0)]]:
            with self.subTest(computers=computers), self.assertRaises(ValueError):
                adapter.runtime_ready(job, {'computer': computers}, 'mac-prod')
        with self.assertRaises(ValueError):
            adapter.runtime_ready(dict(job, buildable=False), {'computer': [node]}, 'mac-prod')

    def test_scanner_collection_pins_flags_and_native_digest(self):
        calls = []
        self.evidence["immutable_image"] = "localhost:9290/app@" + self.fixture.digest
        out = self.root / "collected"
        def run(command, cwd, env):
            calls.append(command)
            self.assertNotIn("TRIVY_IGNORE_UNFIXED", env)
            self.assertNotIn("HARBOR_PASS", env)
            if command[:3] == ["git", "rev-parse", "HEAD"]: return self.evidence["commit"]
            if command[0] == "git": return ""
            if command[0] == "govulncheck":
                return json.dumps({"config": {"scanner_version": "test", "db_last_modified": "2026-09-08"}})
            if command[:2] == ["go", "list"]:
                self.assertEqual(env["GOOS"], "linux")
                self.assertEqual(env["GOARCH"], "arm64")
                self.assertEqual(env["CGO_ENABLED"], "0")
                return "example/app\ngolang.org/x/crypto/argon2\n"
            if command[:2] == ["go", "version"]:
                return "go version go1.26.6 linux/arm64"
            if "image" in command:
                self.assertEqual(command[command.index("--ignorefile") + 1], "/dev/null")
                self.assertEqual(command[command.index("--config") + 1], "/dev/null")
                self.assertEqual(set(command[command.index("--severity") + 1].split(',')), adapter.gate.SEVERITIES)
                return json.dumps(self.fixture.native["trivy"])
            return json.dumps({"Version": "test", "VulnerabilityDB": {"UpdatedAt": "2026-09-08"}})
        native = {"vulnerabilities": [], "scanner": {"version": "test"}, "generated_at": "2026-09-08"}
        fake = types.SimpleNamespace(VULNERABILITY_MIME="mime", HarborAPI=lambda *args: None,
                                     scan_image=lambda *args: types.SimpleNamespace(digest=self.fixture.digest, report=native))
        with patch.object(adapter, "run", side_effect=run), patch.object(adapter, "module", return_value=fake), \
             patch.dict('os.environ', {"HARBOR_USER": "test", "HARBOR_PASS": "secret", "TRIVY_IGNORE_UNFIXED": "true"}):
            self.evidence["reports"] = []
            adapter.scan(self.evidence, self.root, out, "http://harbor.invalid")
        self.assertEqual(len(self.evidence["reports"]), 3)
        self.assertTrue((out / "package-graphs.json").is_file())
        self.assertTrue((out / "evidence.json").is_file())

    def setup_git(self):
        remote, source = self.root / "remote.git", self.root / "source"
        def cmd(*args, cwd=None):
            result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                                     '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', *args],
                                    cwd=cwd, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        cmd('init', '--bare', '-q', str(remote))
        cmd('init', '-q', '-b', 'prod', str(source))
        (source / 'VERSION').write_text('1.0.32\n')
        cmd('add', '.', cwd=source)
        cmd('commit', '-qm', 'base', cwd=source)
        old = cmd('rev-parse', 'HEAD', cwd=source)
        cmd('remote', 'add', 'origin', str(remote), cwd=source)
        cmd('push', '-q', 'origin', 'prod', cwd=source)
        cmd('checkout', '-qb', 'develop', cwd=source)
        (source / 'app.txt').write_text('candidate\n')
        cmd('add', '.', cwd=source)
        cmd('commit', '-qm', 'candidate', cwd=source)
        commit = cmd('rev-parse', 'HEAD', cwd=source)
        cmd('push', '-q', 'origin', 'develop', cwd=source)
        self.evidence.update(commit=commit, version='1.0.32')
        self.fixture.write_reports()
        state_dir = self.root / 'state'
        state_dir.mkdir()
        return cmd, source, remote, old, promotion.State(state_dir)

    def promote(self, source, remote, state):
        return promotion.promote(source, state, self.evidence, self.policy, self.root, self.key, self.now,
                                 expected_remote=str(remote))

    def test_exact_merge_persists_receipt_and_duplicate_is_rejected(self):
        cmd, source, remote, old, state = self.setup_git()
        receipt = self.promote(source, remote, state)
        payload = promotion.verify(receipt, self.key)
        self.assertEqual(payload['status'], 'MERGED')
        self.assertEqual(cmd('rev-list', '--parents', '-n', '1', 'HEAD', cwd=source).split(),
                         [payload['merge_commit'], old, self.evidence['commit']])
        self.assertEqual(promotion.heads(source)[1], payload['merge_commit'])
        cmd('checkout', '--detach', self.evidence['commit'], cwd=source)
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.promote(source, remote, state)

    def test_failed_gate_never_pushes_or_claims(self):
        cmd, source, remote, old, state = self.setup_git()
        self.fixture.vulnerable()
        with self.assertRaises(ValueError):
            self.promote(source, remote, state)
        self.assertEqual(promotion.heads(source)[1], old)
        self.assertFalse(state.exists(self.evidence['commit']))

    def test_superseded_develop_never_merges(self):
        cmd, source, remote, old, state = self.setup_git()
        (source / 'app.txt').write_text('next\n')
        cmd('commit', '-qam', 'next', cwd=source)
        cmd('push', '-q', 'origin', 'develop', cwd=source)
        cmd('checkout', '--detach', self.evidence['commit'], cwd=source)
        with self.assertRaisesRegex(ValueError, 'superseded'):
            self.promote(source, remote, state)
        self.assertEqual(promotion.heads(source)[1], old)

    def test_immutable_tag_collision_stops_before_push(self):
        cmd, source, remote, old, state = self.setup_git()
        cmd('tag', 'v1.0.32', old, cwd=source)
        cmd('push', '-q', 'origin', 'refs/tags/v1.0.32', cwd=source)
        with self.assertRaisesRegex(ValueError, 'version tag'):
            self.promote(source, remote, state)
        self.assertEqual(promotion.heads(source)[1], old)

    def test_lost_push_response_preserves_unknown_state(self):
        cmd, source, remote, old, state = self.setup_git()
        real_git = promotion.git
        def lost(source, *args):
            result = real_git(source, *args)
            if args[0] == 'push': raise ValueError('simulated lost response')
            return result
        with patch.object(promotion, 'git', side_effect=lost), self.assertRaises(ValueError):
            self.promote(source, remote, state)
        saved = promotion.verify(json.loads(state.path(self.evidence['commit']).read_bytes()), self.key)
        self.assertEqual(saved['status'], 'PUSHING')
        self.assertEqual(promotion.heads(source)[1], saved['merge_commit'])
        cmd('checkout', '--detach', self.evidence['commit'], cwd=source)
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.promote(source, remote, state)

    def test_exception_signer_requires_real_identity_and_reason(self):
        self.fixture.vulnerable()
        identity = {'id': 'fixture', 'approver': 'test-approver', 'reason': 'accepted exact fixture CVE'}
        signed = promotion.approve(self.evidence, self.policy, self.root, identity, self.key, self.now)
        self.assertEqual(promotion.gate.evaluate(self.evidence, self.policy, self.root, self.now,
                                                signed, self.key)['decision'], 'APPROVED_EXCEPTION')
        identity['approver'] = 'outsider'
        with self.assertRaises(ValueError):
            promotion.approve(self.evidence, self.policy, self.root, identity, self.key, self.now)

    def test_handoff_rejects_direct_prod_push_and_forged_receipt(self):
        receipt = promotion.sign({'kind': 'promotion', 'status': 'MERGED', 'product': promotion.gate.PRODUCT,
                                  'merge_commit': 'c' * 40, 'version': '1.0.32'}, self.key)
        self.evidence.update(gate='deployment', branch='prod', version='1.0.32')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            promotion.deploy_handoff(receipt, self.key, self.evidence, self.policy, self.root, self.now)
        receipt['payload']['merge_commit'] = self.evidence['commit']
        with self.assertRaisesRegex(ValueError, 'signature'):
            promotion.deploy_handoff(receipt, self.key, self.evidence, self.policy, self.root, self.now)


if __name__ == '__main__':
    unittest.main()
