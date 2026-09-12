#!/usr/bin/env python3
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("preflight", Path(__file__).with_name("release-preflight.py"))
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {variable: "private-test-value" for pair in preflight.PASSWORD_CREDENTIALS.values()
                    for variable in pair}
        for variable in preflight.KEY_CREDENTIALS.values():
            path = self.root / variable
            path.write_bytes(b"secret-key-value-not-for-logs-12345")
            self.env[variable] = str(path)

    def test_valid_credentials(self):
        self.assertEqual(preflight.check(self.env), [])

    def test_reports_both_empty_file_bindings(self):
        for variable in preflight.KEY_CREDENTIALS.values():
            Path(self.env[variable]).write_bytes(b"")
        errors = preflight.check(self.env)
        self.assertEqual(len(errors), 2)
        self.assertIn("approval signing key", errors[0])
        self.assertIn("receipt signing key", errors[1])

    def test_key_boundary_and_missing_path(self):
        path = Path(self.env["RECEIPT_KEY_FILE"])
        path.write_bytes(b"k" * 31)
        self.assertEqual(len(preflight.check(self.env)), 1)
        path.write_bytes(b"k" * 32)
        self.assertEqual(preflight.check(self.env), [])
        path.unlink()
        self.assertEqual(len(preflight.check(self.env)), 1)
        del self.env["RECEIPT_KEY_FILE"]
        self.assertEqual(len(preflight.check(self.env)), 1)

    def test_every_password_binding_is_required(self):
        for pair in preflight.PASSWORD_CREDENTIALS.values():
            for variable in pair:
                with self.subTest(variable=variable):
                    changed = dict(self.env)
                    changed[variable] = ""
                    self.assertEqual(len(preflight.check(changed)), 1)

    def test_cli_does_not_disclose_secret_or_file_path(self):
        for invalid in (False, True):
            if invalid:
                Path(self.env["APPROVAL_KEY_FILE"]).write_bytes(b"secret")
            output = io.StringIO()
            with patch.dict("os.environ", self.env, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(preflight.main(), int(invalid))
            self.assertNotIn("private-test-value", output.getvalue())
            self.assertNotIn("secret", output.getvalue())
            self.assertNotIn(str(self.root), output.getvalue())


if __name__ == "__main__":
    unittest.main()
