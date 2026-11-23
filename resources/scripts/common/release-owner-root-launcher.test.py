#!/usr/bin/env python3
"""Targeted tests for the post-bootstrap launchd root trampoline."""
import importlib.util
from pathlib import Path
import unittest


def load_fixture():
    path = Path(__file__).with_name("release-owner-resume.test.py")
    spec = importlib.util.spec_from_file_location("owner_resume_fixture", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture


fixture = load_fixture()


class RootLauncherTests(unittest.TestCase):
    def setUp(self):
        native = fixture.ResumeTests("test_single_bootstrap_requires_exact_owner_and_two_positive_readiness_proofs")
        native.setUp()
        self.addCleanup(native.doCleanups)
        self.native = native
        fixture.B.subprocess.run.side_effect = self.run_native

    def run_native(self, argv, **kwargs):
        native = self.native
        result = native.run_native(argv, **kwargs)
        if argv[:2] != ["/bin/launchctl", "print"] or "job" not in native.active:
            return result
        attributes = {
            "type": "LaunchAgent",
            "path": native.tx.plist,
            "program": native.executable,
            "pid": native.active.get("pid", 21988),
        }
        attributes.update(native.active["job"])
        properties = "".join(f"\t{key} = {value}\n" for key, value in attributes.items())
        result.stdout = (f"{native.tx.domain} = {{\n" + properties + "}\n").encode()
        return result

    def test_acknowledged_root_launcher_waits_for_same_generation_and_exact_owner(self):
        native = self.native
        native.events = native.ready_events(native.event("launcher", uid=0, job={}))
        owner = native.io.resume(native.tx)
        self.assertEqual(owner["uid_start"], "501 fixture-new-start")
        self.assertEqual(owner["kernel_path"], native.executable)
        self.assertEqual(owner["binary_sha256"], native.before["binary_sha256"])
        waiting = next(r for r in native.records if r["classification"] == "bound-root-launcher")
        self.assertEqual((waiting["uid"], waiting["pid"], waiting["kernel_path"]), (0, 21988, fixture.B.XPCPROXY))
        self.assertTrue(waiting["bootstrap_acknowledged"])
        self.assertIsNone(waiting["binary_sha256"])
        self.assertEqual((native.bootstraps, native.lsof_calls), (1, 2))
        native.assert_preserved()

    def test_root_launcher_with_other_program_is_refused(self):
        native = self.native
        native.events = native.ready_events(native.event("launcher", uid=0, job={"program": "/fixture/other-owner"}))
        with self.assertRaisesRegex(fixture.B.BindingError, "foreign_owner_process"):
            native.io.resume(native.tx)
        self.assertEqual(native.records[-1]["classification"], "foreign-user")
        self.assertEqual(native.lsof_calls, 0)
        native.assert_preserved()

    def test_bound_root_launcher_before_bootstrap_is_refused(self):
        native = self.native
        native.events = [native.event("launcher", uid=0, job={})]
        with self.assertRaisesRegex(fixture.B.BindingError, "foreign_owner_process"):
            native.io.resume(native.tx)
        self.assertEqual(native.bootstraps, 0)
        self.assertFalse(native.records[-1]["bootstrap_acknowledged"])
        native.assert_preserved()

    def test_pid_reuse_with_different_start_after_root_launcher_is_refused(self):
        native = self.native
        native.events = native.ready_events(native.event("launcher", uid=0, job={}, start="previous-generation"))
        with self.assertRaisesRegex(fixture.B.BindingError, "owner_resume_generation_changed"):
            native.io.resume(native.tx)
        self.assertEqual(native.lsof_calls, 0)
        native.assert_preserved()

    def test_root_owner_binary_never_satisfies_final_uid(self):
        native = self.native
        native.events = native.ready_events(native.event("launcher", uid=0, job={}), native.event(uid=0))
        with self.assertRaisesRegex(fixture.B.BindingError, "foreign_owner_process"):
            native.io.resume(native.tx)
        self.assertEqual(native.records[-1]["classification"], "foreign-user")
        self.assertEqual(native.lsof_calls, 0)
        native.assert_preserved()


if __name__ == "__main__":
    unittest.main()
