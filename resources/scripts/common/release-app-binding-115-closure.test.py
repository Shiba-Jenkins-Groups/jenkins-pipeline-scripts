#!/usr/bin/env python3
"""Test the separate grant and capability checks for owner #115 closure."""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


spec = importlib.util.spec_from_file_location(
    "owner115_recovery", Path(__file__).with_name("release-app-binding-115-recovery.py"))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class Owner115ClosureTest(unittest.TestCase):
    def setUp(self):
        self.target = {"id": "new", "image_id": "candidate"}
        self.documents = {}
        for name, actions in (("owner.json", ["status"]), ("registry.json", ["status", "restart"])):
            self.documents[Path("/snapshot") / name] = {
                "services": {"app": {"revision": 20, "actions": actions, "docker": {"targets": [{"id": "old"}]}}}
            }
            current = copy.deepcopy(self.documents[Path("/snapshot") / name])
            current["services"]["app"].update(revision=21, docker={"targets": [self.target]})
            self.documents[Path("/active") / name] = current
        self.observed = {"container_id": "new", "image_id": "candidate"}
        self.owner = {"pid": 1}
        self.binding = SimpleNamespace(document=self.documents.__getitem__)
        self.transaction = SimpleNamespace(
            owner_directory=Path("/active"), backup=Path("/snapshot"), io=SimpleNamespace(owner=lambda _: self.owner),
            pending=Path("/owner115-closure-test-missing"), job_path=Path("/owner115-job-test-missing"),
            owned_pending=[])

    def test_registry_capability_is_preserved_and_is_not_an_owner_grant(self):
        self.assertEqual(recovery.verify_closed(self.binding, self.transaction, self.observed), self.owner)

    def test_changed_effective_grant_is_rejected(self):
        self.documents[Path("/active/owner.json")]["services"]["app"]["actions"] = ["status", "restart"]
        with self.assertRaisesRegex(recovery.Blocked, "unexpected_owner_app_grant"):
            recovery.verify_closed(self.binding, self.transaction, self.observed)

    def test_changed_registry_capabilities_are_rejected(self):
        self.documents[Path("/active/registry.json")]["services"]["app"]["actions"] = ["status"]
        with self.assertRaisesRegex(recovery.Blocked, "unexpected_final_binding"):
            recovery.verify_closed(self.binding, self.transaction, self.observed)


if __name__ == "__main__":
    unittest.main()
