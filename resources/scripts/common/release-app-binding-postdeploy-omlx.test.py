#!/usr/bin/env python3
"""Local tests of the #101 qualification and exact descriptor-only chmod."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "omlx_postdeploy_test", Path(__file__).with_name("release-app-binding-postdeploy-omlx.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def encode(value):
    return M.B.encoded(value) + b"\n"


def fixtures():
    service = {
        "platform": "macos_runtime", "revision": 2, "resource_scope": "host-shared", "shared": True,
        "shared_control_authorized": True, "dependencies": [], "actions": ["start", "status"],
        "mac": {
            "uid": M.OWNER_UID, "port": 8000, "kind": "gui-app", "maintenance_managed": True,
            "bundle_path": str(M.BUNDLE), "bundle_id": M.BUNDLE_ID, "program": str(M.PROGRAM),
            "owner_executable": str(M.GUI), "program_sha256": M.OLD_PROGRAM_SHA, "owner_sha256": M.OLD_GUI_SHA,
        },
    }
    owner = {"environment": "prod", "services": {"omlx": service, "app": {"revision": 6, "actions": ["status"]}},
             "principals": {"fixture": {"grants": ["fixture"]}}}
    registry = {"environment": "prod", "registry_revision": 15, "services": copy.deepcopy(owner["services"]),
                "evidence": {"omlx": {"verified": True}, "app": {"verified": True}}}
    consumers = {"consumers": {"fixture": {"resource_keys": [M.B.resource_keys(service)[0]]}}}
    installation = {"state": "ready", "owner_config_sha256": M.B.sha(encode(owner)),
                    "identity": {"environment": "prod", "owner_uid": M.OWNER_UID,
                                 "registry_digest": M.B.sha(M.B.encoded(registry)),
                                 "consumer_config_digest": M.B.sha(M.B.encoded(consumers))}}
    docs = {
        Path("/private-fixture/prod/owner.json"): encode(owner),
        Path("/private-fixture/prod/registry.json"): encode(registry),
        Path("/private-fixture/prod/installation.json"): encode(installation),
        Path("/private-fixture/consumer-admission.json"): encode(consumers),
    }
    return docs, committed(docs)


def committed(docs):
    return {path.name: M.B.sha(raw) for path, raw in docs.items()}


class ConfigTest(unittest.TestCase):
    def test_changes_only_omlx_scope_and_binds_digests(self):
        docs, pins = fixtures()
        with patch.object(M, "COMMITTED", pins), patch.object(M, "_observed_identity", return_value={"fixture": True}):
            after, identity = M.plan(docs)
            self.assertEqual((after, identity), M.plan(docs))
        self.assertEqual(identity, {"fixture": True})
        old = {p.name: json.loads(raw) for p, raw in docs.items()}
        new = {p.name: json.loads(raw) for p, raw in after.items()}
        self.assertEqual(after[Path("/private-fixture/consumer-admission.json")], docs[Path("/private-fixture/consumer-admission.json")])
        for name in ("owner.json", "registry.json"):
            current = new[name]["services"]["omlx"]
            before = old[name]["services"]["omlx"]
            self.assertEqual(current["revision"], 3)
            self.assertEqual(current["mac"]["program_sha256"], M.PROGRAM_SHA)
            self.assertEqual(current["mac"]["owner_sha256"], M.GUI_SHA)
            self.assertEqual(M.B.resource_keys(current), M.B.resource_keys(before))
            current["revision"] = before["revision"]
            current["mac"].update(program_sha256=M.OLD_PROGRAM_SHA, owner_sha256=M.OLD_GUI_SHA)
        self.assertEqual(new["owner.json"], old["owner.json"])
        self.assertEqual(new["registry.json"]["registry_revision"], 16)
        self.assertEqual(new["registry.json"]["evidence"]["app"], old["registry.json"]["evidence"]["app"])
        installed = new["installation.json"]
        self.assertEqual(installed["owner_config_sha256"], M.B.sha(after[Path("/private-fixture/prod/owner.json")]))
        self.assertEqual(installed["identity"]["registry_digest"],
                         M.B.sha(M.B.encoded(json.loads(after[Path("/private-fixture/prod/registry.json")]))))
        self.assertEqual(installed["identity"]["consumer_config_digest"], old["installation.json"]["identity"]["consumer_config_digest"])

    def test_changed_input_is_rejected_before_probing_bundle(self):
        docs, pins = fixtures()
        path = Path("/private-fixture/prod/owner.json")
        docs[path] += b" "
        with patch.object(M, "COMMITTED", pins), patch.object(M, "_observed_identity") as observe:
            with self.assertRaisesRegex(M.B.BindingError, "omlx_postdeploy_binding_pin_changed"):
                M.plan(docs)
        observe.assert_not_called()


class ModeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.program = self.root / "python3.11"
        self.gui = self.root / "oMLX"
        self.program.write_bytes(b"python fixture")
        self.gui.write_bytes(b"GUI fixture")
        self.program.chmod(0o775)
        self.gui.chmod(0o755)
        self.directory_patch = patch.object(M.B, "directory", side_effect=lambda path: Path(path))
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)
        scope = patch.multiple(
            M, PROGRAM=self.program, GUI=self.gui, BUNDLE=self.root, OWNER_UID=os.getuid(),
            OWNER_GID=self.program.stat().st_gid, PROGRAM_SHA=M.B.sha(self.program.read_bytes()),
            GUI_SHA=M.B.sha(self.gui.read_bytes()),
        )
        scope.start()
        self.addCleanup(scope.stop)
        sig = patch.object(M, "_verify_bundle")
        self.signature = sig.start()
        self.addCleanup(sig.stop)

    def test_only_original_python_descriptor_changes_mode(self):
        before = M._observed_identity(0o775)
        applied = M.apply_mode(before)
        self.assertEqual(applied, M._observed_identity(0o755))
        self.assertEqual(applied["gui"], before["gui"])
        self.assertEqual(applied["program"], {**before["program"], "mode": 0o755})
        self.assertEqual(self.program.read_bytes(), b"python fixture")
        self.assertEqual(stat.S_IMODE(self.gui.stat().st_mode), 0o755)
        self.assertGreaterEqual(self.signature.call_count, 3)

    def test_replaced_inode_is_never_chmodded(self):
        before = M._observed_identity(0o775)
        replacement = self.root / "different-inode"
        replacement.write_bytes(self.program.read_bytes())
        replacement.chmod(0o775)
        os.replace(replacement, self.program)
        with self.assertRaisesRegex(M.B.BindingError, "omlx_reviewed_bundle_identity_changed"):
            M.apply_mode(before)
        self.assertEqual(stat.S_IMODE(self.program.stat().st_mode), 0o775)


if __name__ == "__main__":
    unittest.main()
