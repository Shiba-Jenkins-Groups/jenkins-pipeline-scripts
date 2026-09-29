import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("install-prod-owner-agent.py")
spec = importlib.util.spec_from_file_location("owner_agent", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OwnerAgentTest(unittest.TestCase):
    def test_launchd_is_the_explicit_lifecycle_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            java = root / "java"
            java.write_text("")
            (root / "agent.jar").write_text("")
            jnlp = root / "jenkins-agent.jnlp"
            jnlp.write_text("private")
            jnlp.chmod(0o600)
            module.validate(java, root)
            value = module.configuration(java, root)
            self.assertEqual(module.LABEL, value["Label"])
            self.assertTrue(value["KeepAlive"])
            self.assertTrue(value["RunAtLoad"])
            self.assertEqual(value, plistlib.loads(plistlib.dumps(value)))


if __name__ == "__main__":
    unittest.main()
