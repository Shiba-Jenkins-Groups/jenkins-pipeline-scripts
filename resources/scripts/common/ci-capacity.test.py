import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).with_name("ci-capacity.py")
spec = importlib.util.spec_from_file_location("ci_capacity", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AdmissionTest(unittest.TestCase):
    def invoke(self, states, inspect_result=None):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "ci-capacity.json"
            argv = [str(SCRIPT), "--builder", "shiba-app-ci", "--capacity-script",
                    str(Path(directory) / "k3d.py"), "--policy-config",
                    str(Path(directory) / "policy.toml"), "--output", str(output)]
            calls = []

            def observe(_script, _output):
                return {"status": states.pop(0), "nodes": [{"node": "k3d"}], "docker": {"images": {}}}

            def run(command, *, check=True):
                calls.append(command)
                if command[2] == "inspect":
                    return inspect_result or mock.Mock(returncode=1, stdout="", stderr="missing")
                if command[2] == "prune":
                    return mock.Mock(returncode=0, stdout="reclaimed", stderr="")
                raise AssertionError(command)

            with mock.patch.object(sys, "argv", argv), mock.patch.object(module, "observe", side_effect=observe), \
                    mock.patch.object(module, "run", side_effect=run), \
                    mock.patch.object(module, "inventory", return_value=3), \
                    mock.patch.object(module, "verify_ownership"):
                result = module.main()
            return result, json.loads(output.read_text()), calls

    def test_healthy_never_prunes_or_inspects(self):
        result, report, calls = self.invoke(["OK"])
        self.assertEqual(0, result)
        self.assertEqual([], calls)
        self.assertFalse(report["reclaim"]["attempted"])

    def test_low_capacity_prunes_dedicated_old_cache_once_then_rechecks(self):
        inspect = mock.Mock(returncode=0, stdout="Name: shiba-app-ci\nDriver: docker-container\n", stderr="")
        result, report, calls = self.invoke(["BLOCKED", "OK"], inspect)
        self.assertEqual(0, result)
        prunes = [command for command in calls if command[2] == "prune"]
        self.assertEqual(1, len(prunes))
        self.assertEqual("shiba-app-ci", prunes[0][prunes[0].index("--builder") + 1])
        self.assertEqual("until=24h", prunes[0][prunes[0].index("--filter") + 1])
        self.assertNotIn("--all", prunes[0])
        self.assertTrue(report["reclaim"]["attempted"])

    def test_low_capacity_without_builder_fails_closed_without_shared_prune(self):
        result, report, calls = self.invoke(["BLOCKED", "BLOCKED"])
        self.assertEqual(2, result)
        self.assertEqual("BLOCKED", report["status"])
        self.assertFalse(report["reclaim"]["attempted"])
        self.assertFalse(any(command[2] == "prune" for command in calls))

    def test_wrong_driver_fails_without_prune(self):
        inspect = mock.Mock(returncode=0, stdout="Driver: docker\n", stderr="")
        result, report, calls = self.invoke(["BLOCKED"], inspect)
        self.assertEqual(2, result)
        self.assertIn("not isolated", report["reason"])
        self.assertFalse(any(command[2] == "prune" for command in calls))


if __name__ == "__main__":
    unittest.main()
