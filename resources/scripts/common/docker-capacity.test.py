import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("docker-capacity.py")
spec = importlib.util.spec_from_file_location("docker_capacity", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DockerCapacityTest(unittest.TestCase):
    def test_df_parser_and_thresholds(self):
        value = module.filesystem_from_df(
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/vda 104857600 73400320 31457280 70% /docker-root\n")
        self.assertEqual(30 * module.GIB, value["available_bytes"])
        self.assertEqual("OK", module.evaluate(value, 20, 12, 80, 90)["status"])
        value["available_bytes"] = 10 * module.GIB
        self.assertEqual("BLOCKED", module.evaluate(value, 20, 12, 80, 90)["status"])

    def test_cli_uses_compose_profile_without_kubernetes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "storage.json").write_text(json.dumps({"capacity_bytes": 100 * module.GIB,
                "used_bytes": 70 * module.GIB, "available_bytes": 30 * module.GIB, "used_percent": 70}))
            (root / "docker.json").write_text("[]")
            result = subprocess.run([sys.executable, str(SCRIPT), "--mode", "preflight",
                "--storage-file", str(root / "storage.json"), "--docker-file", str(root / "docker.json"),
                "--output", str(root / "report.json")])
            self.assertEqual(0, result.returncode)
            report = json.loads((root / "report.json").read_text())
            self.assertEqual("compose", report["profile"])
            self.assertEqual("OK", report["status"])


if __name__ == "__main__":
    unittest.main()
