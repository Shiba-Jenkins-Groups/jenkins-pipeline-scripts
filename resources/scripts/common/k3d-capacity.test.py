import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).with_name("k3d-capacity.py")
spec = importlib.util.spec_from_file_location("k3d_capacity", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixtures(available_gib=30, used_percent=None, pressure=False, taint=False):
    capacity = 100 * module.GIB
    available = int(capacity * (1 - used_percent / 100)) if used_percent is not None else available_gib * module.GIB
    node = {"metadata": {"name": "node-1"}, "spec": {"taints": []},
            "status": {"conditions": [{"type": "DiskPressure", "status": "True" if pressure else "False"}]}}
    if taint:
        node["spec"]["taints"].append({"key": "node.kubernetes.io/disk-pressure", "effect": "NoSchedule"})
    return {"items": [node]}, {"node-1": {"node": {"fs": {"capacityBytes": capacity, "availableBytes": available}}}}


class CapacityTest(unittest.TestCase):
    def test_ok_warning_and_blocked(self):
        for available, expected in [(30, "OK"), (18, "WARNING"), (10, "BLOCKED")]:
            nodes, summaries = fixtures(available_gib=available)
            self.assertEqual(expected, module.evaluate(nodes, summaries, 20, 12, 80, 90)["status"])

    def test_pressure_and_taint_fail_closed(self):
        for values in [(True, False), (False, True)]:
            nodes, summaries = fixtures(available_gib=30, pressure=values[0], taint=values[1])
            self.assertEqual("BLOCKED", module.evaluate(nodes, summaries, 20, 12, 80, 90)["status"])

    def test_no_nodes_fail_closed(self):
        self.assertEqual("BLOCKED", module.evaluate({"items": []}, {}, 20, 12, 80, 90)["status"])

    def test_cli_monitor_warning_is_nonzero_and_archived(self):
        nodes, summaries = fixtures(available_gib=18)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "nodes.json").write_text(json.dumps(nodes))
            (root / "summaries.json").write_text(json.dumps(summaries))
            result = subprocess.run([sys.executable, str(SCRIPT), "--mode", "monitor", "--output", str(root / "report.json"),
                                     "--nodes-file", str(root / "nodes.json"), "--summaries-file", str(root / "summaries.json")])
            self.assertEqual(1, result.returncode)
            self.assertEqual("WARNING", json.loads((root / "report.json").read_text())["status"])


if __name__ == "__main__":
    unittest.main()
