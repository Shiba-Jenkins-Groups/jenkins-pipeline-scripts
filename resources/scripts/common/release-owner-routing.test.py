#!/usr/bin/env python3
"""Apply the LOCAL routing proposal only to private, inactive text fixtures."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "docs/app-binding-owner-routing.template.patch"
BEFORE = Path(__file__).with_name("fixtures") / "app-binding-owner-routing.before.yaml"
TOKEN = "REVIEWED_LIBRARY_COMMIT_40_HEX"
OLD = "f5c68a5f0276f542754c69b9854f09be8b484011"
REVIEWED = "a" * 40


class OwnerRouting(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="owner-routing-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "config/system/jenkins.yaml"
        self.config.parent.mkdir(parents=True)
        self.before = BEFORE.read_text()
        self.config.write_text(self.before)
        self.template = TEMPLATE.read_text()
        self.patch = self.directory / "reviewed-routing.patch"
        self.patch.write_text(self.template.replace(TOKEN, REVIEWED))

    def apply(self, check=False):
        return subprocess.run(
            ["git", "apply", "-p0", *( ["--check"] if check else [] ), str(self.patch)],
            cwd=self.directory, text=True, capture_output=True, timeout=10)

    def test_all_four_pins_use_one_revision_and_only_reviewed_parameters_change(self):
        result = self.apply(check=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_text(), self.before)
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.config.read_text()
        expected = self.before.replace(OLD, REVIEWED)
        anchor = "              stringParam('RECOVER_OWNER_BUILD', '', 'Authorized finalized recovery: exact failed owner build')\n"
        expected = expected.replace(anchor, anchor +
            "              stringParam('APP_BINDING_REPAIR_COORDINATOR_BUILD', '', 'Explicit App binding repair: prior successful coordinator build')\n" +
            "              stringParam('APP_BINDING_REPAIR_OWNER_BUILD', '', 'Explicit App binding repair: prior successful deployment owner build')\n" +
            "              textParam('APP_BINDING_REPAIR_PINS', '', 'Exact reviewed stale installation/config/registry/consumer raw SHA256 pins as JSON')\n")
        anchor = "      prodDeploymentPipeline(\n          enabled: true,\n"
        expected = expected.replace(anchor, anchor + "          libraryRevision: '" + REVIEWED + "',\n")
        self.assertEqual(after, expected)
        self.assertEqual(re.findall(r"@Library\('jenkins-pipeline@([a-f0-9]{40})'\)", after), [REVIEWED] * 2)
        self.assertEqual(re.findall(r"libraryRevision: '([a-f0-9]{40})'", after), [REVIEWED] * 2)
        self.assertNotIn(OLD, after)
        self.assertNotIn(TOKEN, after)

    def test_each_existing_pin_drift_blocks_without_writing(self):
        for index in range(3):
            with self.subTest(pin=index):
                parts = self.before.split(OLD)
                changed = OLD.join(parts[:index + 1]) + "b" * 40 + OLD.join(parts[index + 1:])
                self.config.write_text(changed)
                result = self.apply(check=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.config.read_text(), changed)

    def test_template_scope_has_one_target_and_no_removed_behavior(self):
        paths = [line[4:] for line in self.template.splitlines() if line.startswith(("--- ", "+++ "))]
        self.assertEqual(paths, ["config/system/jenkins.yaml"] * 2)
        removed = [line[1:] for line in self.template.splitlines() if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(len(removed), 3)
        self.assertTrue(all(OLD in line for line in removed))
        added = [line[1:] for line in self.template.splitlines() if line.startswith("+") and not line.startswith("+++")]
        self.assertEqual(len(added), 7)
        self.assertEqual(sum(TOKEN in line for line in added), 4)
        self.assertEqual(sum("APP_BINDING_REPAIR_" in line for line in added), 3)

    def test_already_applied_configuration_cannot_be_reapplied(self):
        self.assertEqual(self.apply().returncode, 0)
        after = self.config.read_text()
        self.assertNotEqual(self.apply(check=True).returncode, 0)
        self.assertEqual(self.config.read_text(), after)


if __name__ == "__main__":
    unittest.main()
