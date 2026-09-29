import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("scanner-db-manager.py")
spec = importlib.util.spec_from_file_location("scanner_db", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ScannerManagerTest(unittest.TestCase):
    def test_refresh_uses_db_only_and_requires_provenance(self):
        calls = []
        version = json.dumps({"Version": "0.72.0", "VulnerabilityDB": {
            "UpdatedAt": "2026-09-29T00:00:00Z", "NextUpdate": "2026-09-30T00:00:00Z"}})
        with patch.object(module, "run", side_effect=lambda argv: calls.append(argv) or
                          (version if "--version" in argv else "")):
            value = module.refresh("/cache")
        self.assertEqual("0.72.0", value["scanner_version"])
        self.assertIn("--download-db-only", calls[0])
        self.assertNotIn("--skip-db-update", calls[0])

    def test_harbor_manager_is_bound_to_one_expected_adapter(self):
        row = json.dumps({"ID": "a" * 12, "Image": module.HARBOR_IMAGE}) + "\n"
        with patch.object(module, "run", return_value=row):
            self.assertEqual("a" * 12, module.harbor_container())
        with patch.object(module, "run", return_value=row + row):
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                module.harbor_container()


if __name__ == "__main__":
    unittest.main()
