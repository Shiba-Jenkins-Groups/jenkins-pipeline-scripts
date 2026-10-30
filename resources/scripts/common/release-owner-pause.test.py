#!/usr/bin/env python3
"""LOCAL pause observations: all native commands, proc, clock and file I/O are mocked."""
import copy
import errno
import importlib.util
import json
from pathlib import Path
import subprocess
import types
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("pause_binding", Path(__file__).with_name("release-app-binding.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.io = B.NativeIO()
        self.clock = 100.0
        self.binary = b"offline original owner fixture"
        self.executable = "/fixture/releases/1.1.18-source/owner-darwin-arm64"
        self.before = {"pid": 818, "kernel_path": self.executable, "uid_start": "501 fixture-start",
                       "binary_sha256": B.sha(self.binary)}
        self.tx = types.SimpleNamespace(id="5" * 32, backup=Path("/fixture/private/backup"),
                                        domain="gui/501/com.shiba.service-control.prod",
                                        config={"executable": self.executable}, owner_before=copy.deepcopy(self.before))
        self.events = []
        self.active = None
        self.calls, self.records = [], []
        self.bootouts = 0
        self.bootout_error = None
        self.write_error_phase = None
        self.request_hook = None
        self.markers = {"owner-upgrade.pending.json": b"original-pending", "resource.pending": b"original-fence"}
        self.files = dict(self.markers)
        library = types.SimpleNamespace(proc_pidpath=mock.Mock(side_effect=self.proc_pidpath))
        for patcher in (
                mock.patch.object(B.sys, "platform", "darwin"),
                mock.patch.object(B.os, "getuid", return_value=501),
                mock.patch.object(B.ctypes, "CDLL", return_value=library),
                mock.patch.object(B.subprocess, "run", side_effect=self.run_native),
                mock.patch.object(B.time, "monotonic", side_effect=lambda: self.clock),
                mock.patch.object(B.time, "sleep", side_effect=self.sleep),
                mock.patch.object(B, "stamp", return_value="2026-10-30T05:07:14Z"),
                mock.patch.object(B, "read", side_effect=self.read),
                mock.patch.object(B, "create", side_effect=self.create)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sleep(self, seconds):
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, 0.05)
        self.clock += seconds

    def create(self, path, raw):
        record = json.loads(raw)
        if record["phase"] == self.write_error_phase:
            raise OSError("private fixture filesystem failure")
        self.assertEqual(path.parent, self.tx.backup)
        self.assertNotIn(str(path), self.files)
        self.files[str(path)] = raw
        self.records.append(record)
        if record["phase"] == "bootout-requested" and record["outcome"] == "request-durable" and self.request_hook:
            self.request_hook()

    def read(self, path, mode):
        self.assertEqual((str(path), mode), (self.active.get("executable", self.executable), 0o700))
        self.clock += self.active.get("read_duration", 0)
        return self.active.get("binary", self.binary)

    def proc_pidpath(self, pid, buffer, size):
        self.assertEqual(pid, self.active.get("pid", 818))
        self.assertEqual(size, 4096)
        self.clock += self.active.get("proc_duration", 0)
        if self.active["kind"] == "missing-proc":
            B.ctypes.set_errno(self.active.get("errno", errno.ESRCH))
            return self.active.get("proc_return", 0)
        buffer.value = self.active.get("executable", self.executable).encode()
        return len(buffer.value)

    def advance_query(self, event, timeout, name):
        duration = event.get(name + "_duration", 0)
        self.clock += min(duration, timeout)
        if duration > timeout and not event.get("late_response"):
            raise subprocess.TimeoutExpired("mocked-native-command", timeout)
        self.clock += max(0, duration - timeout)

    def run_native(self, argv, *, capture_output, timeout):
        self.assertTrue(capture_output)
        self.calls.append((list(argv), timeout))
        if argv[:2] == ["/bin/launchctl", "print"]:
            self.assertEqual(argv[2], self.tx.domain)
            self.assertTrue(self.events, "unexpected native observation")
            self.active = self.events.pop(0)
            self.advance_query(self.active, timeout, "print")
            kind = self.active["kind"]
            if kind == "unloaded":
                return types.SimpleNamespace(returncode=113, stdout=b"", stderr=b"Could not find service fixture")
            if kind == "unknown":
                return types.SimpleNamespace(returncode=113, stdout=b"private raw stdout",
                                             stderr=b"private raw error; no trusted absence")
            text = "state = running\n" if kind == "missing-pid" else "pid = %d\n" % self.active.get("pid", 818)
            return types.SimpleNamespace(returncode=0, stdout=text.encode(), stderr=b"")
        if argv[:2] == ["/bin/launchctl", "bootout"]:
            self.assertEqual(argv[2], self.tx.domain)
            self.assertEqual(self.records[-1]["phase"], "bootout-requested")
            self.assertFalse(self.records[-1]["bootout_acknowledged"])
            self.bootouts += 1
            if self.bootout_error:
                raise self.bootout_error
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        self.assertEqual(argv[:2], ["/bin/ps", "-p"])
        self.advance_query(self.active, timeout, "ps")
        return types.SimpleNamespace(returncode=self.active.get("ps_rc", 0),
                                     stdout=self.active.get("process", "%s fixture-start" % self.active.get("uid", 501)).encode(),
                                     stderr=b"private ps details")

    def event(self, kind="running", **values):
        return dict(kind=kind, **values)

    def assert_preserved(self):
        for key, raw in self.markers.items():
            self.assertEqual(self.files[key], raw)
        self.assertLessEqual(self.bootouts, 1)
        self.assertNotIn("private raw", json.dumps(self.records))
        self.assertNotIn("private ps", json.dumps(self.records))
        self.assertNotIn("filesystem failure", json.dumps(self.records))

    def test_success_requires_explicit_launchd_absence_after_one_acknowledged_bootout(self):
        self.events = [self.event(), self.event(), self.event("unloaded")]
        self.io.pause(self.tx)
        self.assertEqual(self.bootouts, 1)
        self.assertEqual([r["phase"] for r in self.records],
                         ["pre-pause", "bootout-requested", "bootout-acknowledged", "post-bootout"])
        self.assertEqual(self.records[-1]["outcome"], "launchd-not-loaded")
        self.assertTrue(self.records[-1]["bootout_acknowledged"])
        self.assert_preserved()

    def test_exited_original_pid_can_be_reobserved_until_launchd_reports_unloaded(self):
        self.events = [self.event(), self.event(), self.event("missing-proc"), self.event("missing-pid"), self.event("unloaded")]
        self.io.pause(self.tx)
        failure = self.records[3]
        self.assertEqual((failure["pid"], failure["errno"], failure["proc_pidpath_return"]), (818, errno.ESRCH, 0))
        self.assertEqual(failure["outcome"], "owner_kernel_path_unavailable")
        self.assertTrue(failure["bootout_acknowledged"])
        self.assertEqual(self.records[-1]["outcome"], "launchd-not-loaded")
        self.assert_preserved()

    def test_negative_proc_return_and_errno_are_retained_without_claiming_absence(self):
        self.events = [self.event(), self.event(), self.event("missing-proc", proc_return=-1, errno=errno.EPERM), self.event("unloaded")]
        self.io.pause(self.tx)
        self.assertEqual((self.records[3]["errno"], self.records[3]["proc_pidpath_return"]), (errno.EPERM, -1))
        self.assertEqual(self.records[3]["outcome"], "owner_kernel_path_unavailable")
        self.assert_preserved()

    def test_original_owner_still_running_after_ack_is_not_pause_success(self):
        self.events = [self.event(), self.event(), self.event(), self.event("unloaded")]
        self.io.pause(self.tx)
        self.assertEqual(self.records[3]["outcome"], "identity-observed")
        self.assertEqual(self.records[3]["pid"], 818)
        self.assertEqual(self.records[3]["binary_sha256"], self.before["binary_sha256"])
        self.assertEqual(len(self.events), 0)
        self.assert_preserved()

    def test_pre_pause_missing_proc_blocks_before_bootout_and_retains_errno(self):
        self.events = [self.event("missing-proc")]
        with self.assertRaisesRegex(B.BindingError, "owner_kernel_path_unavailable"):
            self.io.pause(self.tx)
        self.assertEqual(self.bootouts, 0)
        self.assertEqual((self.records[0]["phase"], self.records[0]["pid"], self.records[0]["errno"]),
                         ("pre-pause", 818, errno.ESRCH))
        self.assertFalse(self.records[0]["bootout_acknowledged"])
        self.assert_preserved()

    def test_pre_pause_missing_pid_is_not_an_unloaded_owner(self):
        self.events = [self.event("missing-pid")]
        with self.assertRaisesRegex(B.BindingError, "owner_loaded_without_process"):
            self.io.pause(self.tx)
        self.assertEqual(self.bootouts, 0)
        self.assert_preserved()

    def test_pre_pause_absent_owner_is_changed_and_does_not_issue_bootout(self):
        self.events = [self.event("unloaded")]
        with self.assertRaisesRegex(B.BindingError, "owner_changed_before_pause"):
            self.io.pause(self.tx)
        self.assertEqual(self.bootouts, 0)
        self.assert_preserved()

    def test_pre_pause_foreign_pid_binary_uid_and_path_remain_fail_closed(self):
        for values, code in (({"pid": 999}, "owner_changed_before_pause"),
                             ({"binary": b"changed binary"}, "owner_changed_before_pause"),
                             ({"uid": 502}, "foreign_owner_process"),
                             ({"executable": "/fixture/foreign-owner"}, "foreign_owner_process")):
            with self.subTest(values=values):
                self.events = [self.event(**values)]
                with self.assertRaisesRegex(B.BindingError, code):
                    self.io.pause(self.tx)
                self.assertEqual(self.bootouts, 0)
                self.assert_preserved()

    def test_owner_replaced_during_durable_request_is_rejected_before_bootout(self):
        for values, code in (({"pid": 999}, "owner_changed_before_pause"),
                             ({"binary": b"replacement during fsync"}, "owner_changed_before_pause"),
                             ({"uid": 502}, "foreign_owner_process"),
                             ({"executable": "/fixture/replacement-owner"}, "foreign_owner_process")):
            with self.subTest(values=values):
                self.events = [self.event(), self.event(), self.event("unloaded")]
                self.request_hook = lambda: self.events[0].update(values)
                with self.assertRaisesRegex(B.BindingError, code):
                    self.io.pause(self.tx)
                self.assertEqual(self.bootouts, 0)
                self.assertEqual(self.records[-1]["phase"], "pre-bootout")
                self.assertFalse(self.records[-1]["bootout_acknowledged"])
                self.assertEqual(len(self.events), 1)
                self.assert_preserved()

    def test_original_proc_becomes_unavailable_during_durable_request_without_bootout(self):
        self.events = [self.event(), self.event(), self.event("unloaded")]
        self.request_hook = lambda: self.events[0].update(kind="missing-proc")
        with self.assertRaisesRegex(B.BindingError, "owner_kernel_path_unavailable"):
            self.io.pause(self.tx)
        self.assertEqual(self.bootouts, 0)
        self.assertEqual((self.records[-1]["phase"], self.records[-1]["pid"], self.records[-1]["errno"]),
                         ("pre-bootout", 818, errno.ESRCH))
        self.assertFalse(self.records[-1]["bootout_acknowledged"])
        self.assertEqual(len(self.events), 1)
        self.assert_preserved()

    def test_pre_bootout_reobservation_error_is_safely_diagnosed_without_mutation(self):
        self.events = [self.event(), self.event(print_duration=6), self.event("unloaded")]
        with self.assertRaises(subprocess.TimeoutExpired):
            self.io.pause(self.tx)
        self.assertEqual(self.bootouts, 0)
        self.assertEqual(self.records[-1]["phase"], "pre-bootout")
        self.assertEqual(self.records[-1]["outcome"], "TimeoutExpired")
        self.assert_preserved()

    def test_foreign_pid_with_missing_proc_after_ack_is_not_a_tolerated_original_pid(self):
        self.events = [self.event(), self.event(), self.event("missing-proc", pid=999), self.event("unloaded")]
        with self.assertRaisesRegex(B.BindingError, "owner_changed_during_pause"):
            self.io.pause(self.tx)
        self.assertEqual(self.records[-1]["pid"], 999)
        self.assertEqual(len(self.events), 1)
        self.assert_preserved()

    def test_foreign_identity_after_ack_is_never_swallowed(self):
        for values, code in (({"pid": 999}, "owner_changed_during_pause"),
                             ({"binary": b"changed binary"}, "owner_changed_during_pause"),
                             ({"uid": 502}, "foreign_owner_process"),
                             ({"executable": "/fixture/foreign-owner"}, "foreign_owner_process")):
            with self.subTest(values=values):
                self.bootouts = 0
                self.events = [self.event(), self.event(), self.event(**values), self.event("unloaded")]
                with self.assertRaisesRegex(B.BindingError, code):
                    self.io.pause(self.tx)
                self.assertEqual(len(self.events), 1)
                self.assert_preserved()

    def test_unknown_launchd_error_after_ack_is_not_absence(self):
        self.events = [self.event(), self.event(), self.event("unknown"), self.event("unloaded")]
        with self.assertRaisesRegex(B.BindingError, "owner_launchd_observation_unknown"):
            self.io.pause(self.tx)
        self.assertEqual(len(self.events), 1)
        self.assert_preserved()

    def test_missing_proc_without_explicit_absence_expires_with_one_bootout(self):
        self.events = [self.event(), self.event()] + [self.event("missing-proc")] * 400
        with self.assertRaisesRegex(B.BindingError, "owner_pause_unconfirmed"):
            self.io.pause(self.tx)
        self.assertAlmostEqual(self.clock, 115.0)
        self.assertEqual(self.records[-1]["outcome"], "deadline-expired")
        self.assertTrue(self.records[-1]["bootout_acknowledged"])
        self.assert_preserved()

    def test_bootout_failure_has_no_ack_and_never_starts_post_bootout_poll(self):
        self.events = [self.event(), self.event(), self.event("unloaded")]
        self.bootout_error = B.BindingError("native_binding_command_failed")
        with self.assertRaisesRegex(B.BindingError, "native_binding_command_failed"):
            self.io.pause(self.tx)
        self.assertFalse(any(r["bootout_acknowledged"] for r in self.records))
        self.assertEqual(len(self.events), 1)
        self.assert_preserved()

    def test_bootout_timeout_is_preserved_as_unconfirmed_and_not_reissued(self):
        self.events = [self.event(), self.event(), self.event("unloaded")]
        self.bootout_error = subprocess.TimeoutExpired("private command", 20)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.io.pause(self.tx)
        self.assertEqual(self.records[-1]["outcome"], "acknowledgement-unconfirmed")
        self.assertFalse(self.records[-1]["bootout_acknowledged"])
        self.assertEqual(len(self.events), 1)
        self.assert_preserved()

    def test_diagnostic_write_failure_before_command_cannot_issue_bootout(self):
        self.events = [self.event(), self.event(), self.event("unloaded")]
        self.write_error_phase = "bootout-requested"
        with self.assertRaises(OSError):
            self.io.pause(self.tx)
        self.assertEqual(self.bootouts, 0)
        self.assert_preserved()

    def test_diagnostic_write_failure_after_ack_cannot_confirm_or_reissue(self):
        self.events = [self.event(), self.event(), self.event("unloaded")]
        self.write_error_phase = "bootout-acknowledged"
        with self.assertRaises(OSError):
            self.io.pause(self.tx)
        self.assertEqual(self.bootouts, 1)
        self.assertEqual(len(self.events), 1)
        self.assert_preserved()

    def test_final_absence_diagnostic_write_failure_is_not_success(self):
        self.events = [self.event(), self.event(), self.event("unloaded")]
        self.write_error_phase = "post-bootout"
        with self.assertRaises(OSError):
            self.io.pause(self.tx)
        self.assertEqual(self.bootouts, 1)
        self.assert_preserved()

    def test_post_ack_print_timeout_is_bounded_by_remaining_budget(self):
        self.events = [self.event(), self.event(), self.event("missing-pid", print_duration=4.99),
                       self.event("missing-pid", print_duration=4.99), self.event("unloaded", print_duration=6)]
        with self.assertRaisesRegex(B.BindingError, "owner_pause_unconfirmed"):
            self.io.pause(self.tx)
        print_timeouts = [timeout for argv, timeout in self.calls if argv[:2] == ["/bin/launchctl", "print"]]
        self.assertLess(print_timeouts[-1], 5)
        self.assertAlmostEqual(self.clock, 115.0)
        self.assert_preserved()

    def test_post_ack_ps_timeout_uses_remaining_budget_instead_of_twenty_seconds(self):
        self.events = [self.event(), self.event(), self.event(print_duration=4, ps_duration=20)]
        with self.assertRaisesRegex(B.BindingError, "owner_pause_unconfirmed"):
            self.io.pause(self.tx)
        ps_timeouts = [timeout for argv, timeout in self.calls if argv[0] == "/bin/ps"]
        self.assertEqual(ps_timeouts, [20, 20, 11])
        self.assertAlmostEqual(self.clock, 115.0)
        self.assert_preserved()

    def test_late_successful_absence_response_cannot_confirm_after_deadline(self):
        self.events = [self.event(), self.event(), self.event("unloaded", print_duration=16, late_response=True)]
        with self.assertRaisesRegex(B.BindingError, "owner_pause_unconfirmed"):
            self.io.pause(self.tx)
        self.assertNotEqual(self.records[-1]["outcome"], "launchd-not-loaded")
        self.assert_preserved()

    def test_final_identity_file_read_cannot_carry_a_success_past_deadline(self):
        self.events = [self.event(), self.event(), self.event(read_duration=16), self.event("unloaded")]
        with self.assertRaisesRegex(B.BindingError, "owner_pause_unconfirmed"):
            self.io.pause(self.tx)
        self.assertEqual(len(self.events), 1)
        self.assert_preserved()

    def test_time_spent_persisting_ack_counts_against_pause_deadline(self):
        original = self.create

        def slow_create(path, raw):
            original(path, raw)
            if json.loads(raw)["phase"] == "bootout-acknowledged":
                self.clock += 15

        self.events = [self.event(), self.event(), self.event("unloaded")]
        with mock.patch.object(B, "create", side_effect=slow_create), \
                self.assertRaisesRegex(B.BindingError, "owner_pause_unconfirmed"):
            self.io.pause(self.tx)
        self.assertEqual(len(self.events), 1)
        self.assert_preserved()

    def test_resume_keeps_existing_missing_proc_failure_semantics(self):
        error = B.BindingError("owner_kernel_path_unavailable")
        with mock.patch.object(self.io, "owner", side_effect=[None, error]), \
                mock.patch.object(self.io, "command") as command:
            self.tx.plist = Path("/fixture/original.plist")
            with self.assertRaisesRegex(B.BindingError, "owner_kernel_path_unavailable"):
                self.io.resume(self.tx)
        command.assert_called_once_with(["/bin/launchctl", "bootstrap", "gui/501", "/fixture/original.plist"])
        self.assertEqual(self.records, [])

    def test_owner_default_call_keeps_native_command_timeouts_and_identity(self):
        self.events = [self.event()]
        self.assertEqual(self.io.owner(self.tx), self.before)
        self.assertEqual([timeout for _, timeout in self.calls], [5, 20])
        self.assertEqual(self.records, [])


if __name__ == "__main__":
    unittest.main()
