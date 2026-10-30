#!/usr/bin/env python3
"""Synthetic owner startup observations; no native services, files, network or DB."""
import copy
import errno
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import types
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("resume_binding", Path(__file__).with_name("release-app-binding.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.io, self.clock = B.NativeIO(), 100.0
        self.executable = "/fixture/release/owner-darwin-arm64"
        self.binary = b"original synthetic owner"
        self.before = {"pid": 818, "kernel_path": self.executable, "uid_start": "501 original-start",
                       "binary_sha256": B.sha(self.binary)}
        self.tx = types.SimpleNamespace(id="5" * 32, backup=Path("/fixture/private/backup"),
                                        domain="gui/501/com.shiba.service-control.prod", plist=Path("/fixture/owner.plist"),
                                        journal=Path("/fixture/prod/operations.jsonl"), owner_before=copy.deepcopy(self.before),
                                        config={"executable": self.executable, "listen": "127.0.0.1:8120"})
        self.events, self.calls, self.records = [], [], []
        self.active, self.bootstraps, self.lsof_calls = None, 0, 0
        self.bootstrap_error, self.write_error_phase, self.record_hook = None, None, None
        self.descriptors, self.next_fd, self.journal_lstats = {}, 321, 0
        self.original_markers = {"resource.pending": b"unchanged original fence"}
        self.files = dict(self.original_markers)
        self.journal_stat = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=501, st_dev=10, st_ino=20)
        library = types.SimpleNamespace(proc_pidpath=mock.Mock(side_effect=self.proc_pidpath))
        for patcher in (
                mock.patch.object(B.sys, "platform", "darwin"),
                mock.patch.object(B.os, "getuid", return_value=501),
                mock.patch.object(B.ctypes, "CDLL", return_value=library),
                mock.patch.object(B.subprocess, "run", side_effect=self.run_native),
                mock.patch.object(B.time, "monotonic", side_effect=lambda: self.clock),
                mock.patch.object(B.time, "sleep", side_effect=self.sleep),
                mock.patch.object(B, "stamp", return_value="2026-10-30T05:56:07Z"),
                mock.patch.object(B, "read", side_effect=self.read),
                mock.patch.object(B, "create", side_effect=self.create),
                mock.patch.object(Path, "lstat", autospec=True, side_effect=self.lstat),
                mock.patch.object(B.os, "open", side_effect=self.open_journal),
                mock.patch.object(B.os, "fstat", side_effect=lambda fd: self.descriptors[fd]),
                mock.patch.object(B.os, "close", side_effect=self.close_journal),
                mock.patch.object(B.fcntl, "flock", side_effect=self.flock)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def event(self, kind="owner", **values):
        return dict(kind=kind, **values)

    def ready_events(self, *middle):
        return [self.event("unloaded"), self.event("unloaded"), *middle, self.event(), self.event()]

    def sleep(self, seconds):
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, 0.05)
        self.clock += seconds

    def create(self, path, raw):
        record = json.loads(raw)
        if record["phase"] == self.write_error_phase:
            raise OSError("private synthetic filesystem detail")
        self.assertEqual(path.parent, self.tx.backup)
        self.assertNotIn(str(path), self.files)
        self.files[str(path)] = raw
        self.records.append(record)
        if self.record_hook:
            self.record_hook(record)

    def read(self, path, mode):
        self.assertEqual((str(path), mode), (self.executable, 0o700))
        self.clock += self.active.get("read_duration", 0)
        return self.active.get("binary", self.binary)

    def lstat(self, path):
        self.assertEqual(path, self.tx.journal)
        self.journal_lstats += 1
        value = copy.copy(self.journal_stat)
        if self.active.get("journal_path_replaced") and self.journal_lstats % 2 == 0:
            value.st_ino += 1
        return value

    def open_journal(self, path, flags):
        self.assertEqual((path, flags), (self.tx.journal, B.os.O_RDWR | B.os.O_NOFOLLOW))
        value = copy.copy(self.journal_stat)
        if self.active.get("journal_foreign_uid"):
            value.st_uid = 502
        if self.active.get("journal_public_mode"):
            value.st_mode = stat.S_IFREG | 0o644
        fd = self.next_fd
        self.next_fd += 1
        self.descriptors[fd] = value
        return fd

    def close_journal(self, fd):
        self.assertIn(fd, self.descriptors)
        del self.descriptors[fd]

    def flock(self, fd, flags):
        self.assertIn(fd, self.descriptors)
        self.assertEqual(flags, B.fcntl.LOCK_EX | B.fcntl.LOCK_NB)
        self.clock += self.active.get("journal_duration", 0)
        if self.active.get("journal_held", True):
            raise BlockingIOError()

    def proc_pidpath(self, pid, buffer, size):
        self.assertEqual((pid, size), (self.active.get("pid", 21988), 4096))
        self.clock += self.active.get("proc_duration", 0)
        if self.active["kind"] == "missing-proc":
            B.ctypes.set_errno(self.active.get("errno", errno.ESRCH))
            return 0
        path = "/usr/libexec/xpcproxy" if self.active["kind"] == "launcher" else self.active.get("executable", self.executable)
        buffer.value = path.encode()
        return len(buffer.value)

    def duration(self, event, name, timeout):
        duration = event.get(name + "_duration", 0)
        self.clock += min(duration, timeout)
        if duration > timeout and not event.get("late_response"):
            raise subprocess.TimeoutExpired("private synthetic native command", timeout)
        self.clock += max(0, duration - timeout)

    def run_native(self, argv, *, capture_output, timeout):
        self.assertTrue(capture_output)
        self.calls.append((list(argv), timeout))
        if argv[:2] == ["/bin/launchctl", "print"]:
            self.assertTrue(self.events, "unexpected owner observation")
            self.active = self.events.pop(0)
            self.duration(self.active, "print", timeout)
            kind = self.active["kind"]
            if kind == "unloaded":
                return types.SimpleNamespace(returncode=113, stdout=b"", stderr=b"Could not find service fixture")
            if kind == "unknown":
                return types.SimpleNamespace(returncode=113, stdout=b"private raw stdout", stderr=b"private raw error")
            raw = b"state = running\n" if kind == "missing-pid" else ("pid = %d\n" % self.active.get("pid", 21988)).encode()
            return types.SimpleNamespace(returncode=0, stdout=raw, stderr=b"")
        if argv[:2] == ["/bin/launchctl", "bootstrap"]:
            self.assertEqual(argv, ["/bin/launchctl", "bootstrap", "gui/501", str(self.tx.plist)])
            self.assertEqual(self.records[-1]["phase"], "bootstrap-requested")
            self.bootstraps += 1
            self.assertEqual(self.bootstraps, 1)
            if self.bootstrap_error:
                raise self.bootstrap_error
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if argv[0] == "/bin/ps":
            self.duration(self.active, "ps", timeout)
            raw = self.active.get("process", "%d %s" % (self.active.get("uid", 501), self.active.get("start", "fixture-new-start"))).encode()
            rc = self.active.get("ps_rc", 0)
            if self.active["kind"] == "missing-ps":
                raw, rc = b"", 1
            return types.SimpleNamespace(returncode=rc, stdout=raw, stderr=self.active.get("ps_stderr", b""))
        self.assertEqual(argv, ["/usr/sbin/lsof", "-nP", "-iTCP:8120", "-sTCP:LISTEN", "-t"])
        self.lsof_calls += 1
        self.duration(self.active, "listener", timeout)
        pids = self.active.get("listeners", [self.active.get("pid", 21988)])
        return types.SimpleNamespace(returncode=self.active.get("listener_rc", 0),
                                     stdout="\n".join(map(str, pids)).encode(),
                                     stderr=self.active.get("listener_stderr", b""))

    def assert_preserved(self):
        self.assertLessEqual(self.bootstraps, 1)
        self.assertEqual(self.descriptors, {})
        for key, raw in self.original_markers.items():
            self.assertEqual(self.files[key], raw)
        raw = json.dumps(self.records)
        for forbidden in ("private raw", "private synthetic", "native command", "process-secret"):
            self.assertNotIn(forbidden, raw)
        self.assertFalse(any(argv[:2] == ["/bin/launchctl", "bootout"] for argv, _ in self.calls))

    def test_single_bootstrap_requires_exact_owner_and_two_positive_readiness_proofs(self):
        self.events = self.ready_events()
        current = self.io.resume(self.tx)
        self.assertEqual((current["pid"], current["kernel_path"], current["binary_sha256"]),
                         (21988, self.executable, self.before["binary_sha256"]))
        self.assertEqual((self.bootstraps, self.lsof_calls), (1, 2))
        self.assertEqual(self.records[-1]["phase"], "verification-observed")
        self.assertTrue(self.records[-1]["bootstrap_acknowledged"])
        self.assert_preserved()

    def test_launcher_is_only_unverified_until_same_generation_execs_exact_owner(self):
        self.events = self.ready_events(self.event("launcher"))
        self.assertEqual(self.io.resume(self.tx)["pid"], 21988)
        launcher = next(r for r in self.records if r["classification"] == "system-launcher")
        self.assertEqual((launcher["pid"], launcher["uid"], launcher["kernel_path"]), (21988, 501, "/usr/libexec/xpcproxy"))
        self.assertEqual(launcher["uid_classification"], "same-user")
        self.assertNotEqual(launcher["outcome"], "listener-and-journal-held")
        self.assert_preserved()

    def test_incomplete_loaded_pid_proc_and_ps_can_converge_after_ack(self):
        self.events = self.ready_events(self.event("missing-pid"), self.event("missing-proc"), self.event("missing-ps"))
        self.io.resume(self.tx)
        classifications = {r["classification"] for r in self.records}
        self.assertTrue({"loaded-without-process", "kernel-path-unavailable", "process-unavailable"} <= classifications)
        proc = next(r for r in self.records if r["classification"] == "kernel-path-unavailable")
        self.assertEqual((proc["pid"], proc["errno"]), (21988, errno.ESRCH))
        self.assertTrue(proc["bootstrap_acknowledged"])
        self.assert_preserved()

    def test_explicit_absence_can_wait_only_before_first_new_pid_observation(self):
        self.events = self.ready_events(self.event("unloaded"))
        self.io.resume(self.tx)
        self.assert_preserved()

    def test_existing_owner_before_resume_is_refused_without_bootstrap(self):
        self.events = [self.event()]
        with self.assertRaisesRegex(B.BindingError, "foreign_owner_before_resume"):
            self.io.resume(self.tx)
        self.assertEqual(self.bootstraps, 0)
        self.assertFalse(any(r["bootstrap_acknowledged"] for r in self.records))
        self.assert_preserved()

    def test_launcher_and_incomplete_pre_resume_observations_do_not_get_retry_permission(self):
        for kind in ("launcher", "missing-proc", "missing-pid", "missing-ps"):
            with self.subTest(kind=kind):
                self.events = [self.event(kind)]
                with self.assertRaises(B.BindingError):
                    self.io.resume(self.tx)
                self.assertEqual(self.bootstraps, 0)
                self.assert_preserved()

    def test_owner_appearing_during_diagnostic_fsync_blocks_bootstrap(self):
        self.events = [self.event("unloaded"), self.event("unloaded")]
        def replace(record):
            if record["phase"] == "bootstrap-requested": self.events[0] = self.event()
        self.record_hook = replace
        with self.assertRaisesRegex(B.BindingError, "foreign_owner_before_resume"):
            self.io.resume(self.tx)
        self.assertEqual(self.bootstraps, 0)
        self.assertEqual(self.records[-1]["phase"], "pre-bootstrap")
        self.assert_preserved()

    def test_root_uid_launcher_is_foreign_not_a_startup_transient(self):
        self.events = self.ready_events(self.event("launcher", uid=0))
        with self.assertRaisesRegex(B.BindingError, "foreign_owner_process"):
            self.io.resume(self.tx)
        self.assertEqual(self.records[-1]["uid"], 0)
        self.assertEqual(self.records[-1]["classification"], "foreign-user")
        self.assert_preserved()

    def test_arbitrary_foreign_executable_is_not_a_launcher(self):
        self.events = self.ready_events(self.event(executable="/fixture/foreign-owner"))
        with self.assertRaisesRegex(B.BindingError, "foreign_owner_process"):
            self.io.resume(self.tx)
        self.assertEqual(self.records[-1]["kernel_path"], "/fixture/foreign-owner")
        self.assertEqual(self.records[-1]["classification"], "foreign-executable")
        self.assert_preserved()

    def test_uid_mismatch_on_expected_owner_path_remains_fatal(self):
        self.events = self.ready_events(self.event(uid=502))
        with self.assertRaisesRegex(B.BindingError, "foreign_owner_process"):
            self.io.resume(self.tx)
        self.assertEqual(self.records[-1]["uid_classification"], "foreign-user")
        self.assert_preserved()

    def test_incomplete_ps_for_foreign_path_cannot_be_waited_into_success(self):
        self.events = self.ready_events(self.event("missing-ps", executable="/fixture/foreign-owner"))
        with self.assertRaisesRegex(B.BindingError, "owner_process_unavailable"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_old_owner_pid_is_not_a_new_bootstrap_generation(self):
        self.events = self.ready_events(self.event(pid=818))
        with self.assertRaisesRegex(B.BindingError, "owner_resume_identity_mismatch"):
            self.io.resume(self.tx)
        self.assertEqual(self.lsof_calls, 0)
        self.assert_preserved()

    def test_wrong_binary_hash_is_immediately_rejected_before_listener_checks(self):
        self.events = self.ready_events(self.event(binary=b"different binary"))
        with self.assertRaisesRegex(B.BindingError, "owner_resume_identity_mismatch"):
            self.io.resume(self.tx)
        self.assertEqual(self.lsof_calls, 0)
        self.assert_preserved()

    def test_pid_change_after_launcher_is_rejected(self):
        self.events = self.ready_events(self.event("launcher"))
        self.events[-2] = self.event(pid=21989)
        with self.assertRaisesRegex(B.BindingError, "owner_resume_generation_changed"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_same_pid_with_different_start_time_after_launcher_is_rejected(self):
        self.events = self.ready_events(self.event("launcher", start="earlier-start"))
        with self.assertRaisesRegex(B.BindingError, "owner_resume_generation_changed"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_pid_change_after_missing_proc_is_rejected_even_before_first_full_identity(self):
        self.events = self.ready_events(self.event("missing-proc"))
        self.events[-2] = self.event(pid=21989)
        with self.assertRaisesRegex(B.BindingError, "owner_resume_generation_changed"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_label_disappearing_after_a_pinned_launcher_is_not_a_fresh_start(self):
        self.events = self.ready_events(self.event("launcher"), self.event("unloaded"))
        with self.assertRaisesRegex(B.BindingError, "owner_resume_generation_changed"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_no_listener_yet_can_be_reobserved_without_rebootstrap(self):
        self.events = self.ready_events(self.event(listeners=[], listener_rc=1))
        self.io.resume(self.tx)
        self.assertEqual(self.lsof_calls, 3)
        self.assert_preserved()

    def test_other_or_multiple_listener_pids_are_not_ready(self):
        for listeners in ([999], [21988, 999]):
            with self.subTest(listeners=listeners):
                self.tx._resume_bootstrap_submitted = False
                self.bootstraps = 0
                self.events = self.ready_events(self.event(listeners=listeners))
                with self.assertRaisesRegex(B.BindingError, "owner_listener_identity_mismatch"):
                    self.io.resume(self.tx)
                self.assert_preserved()

    def test_listener_metadata_errors_are_not_silently_retried(self):
        self.events = self.ready_events(self.event(listener_stderr=b"private raw lsof error"))
        with self.assertRaisesRegex(B.BindingError, "owner_listener_observation_unknown"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_free_journal_lock_is_not_owner_ready(self):
        self.events = self.ready_events(self.event(journal_held=False))
        self.io.resume(self.tx)
        self.assert_preserved()

    def test_foreign_journal_uid_or_mode_blocks_even_with_a_matching_listener(self):
        for value in ("journal_foreign_uid", "journal_public_mode", "journal_path_replaced"):
            with self.subTest(value=value):
                self.tx._resume_bootstrap_submitted = False
                self.bootstraps = 0
                self.events = self.ready_events(self.event(**{value: True}))
                with self.assertRaisesRegex(B.BindingError, "owner_journal_identity_changed"):
                    self.io.resume(self.tx)
                self.assert_preserved()

    def test_final_identity_after_ready_diagnostic_must_still_be_exact(self):
        self.events = self.ready_events()
        self.events[-1] = self.event(pid=21989)
        with self.assertRaisesRegex(B.BindingError, "owner_resume_generation_changed"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_listener_and_journal_are_rechecked_after_diagnostic_fsync(self):
        self.events = self.ready_events()
        self.events[-1] = self.event(journal_held=False)
        with self.assertRaisesRegex(B.BindingError, "owner_resume_readiness_changed"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_launchd_unknown_error_does_not_become_absence_or_retry(self):
        self.events = self.ready_events(self.event("unknown"))
        with self.assertRaisesRegex(B.BindingError, "owner_launchd_observation_unknown"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_malformed_process_output_is_diagnosed_without_raw_output(self):
        self.events = self.ready_events(self.event(process="process-secret"))
        with self.assertRaisesRegex(B.BindingError, "foreign_owner_process"):
            self.io.resume(self.tx)
        self.assertEqual(self.records[-1]["classification"], "invalid-process-identity")
        self.assert_preserved()

    def test_launcher_that_never_execs_times_out_without_any_readiness_proof(self):
        self.events = [self.event("unloaded"), self.event("unloaded")] + [self.event("launcher")] * 400
        with self.assertRaisesRegex(B.BindingError, "owner_resume_unconfirmed"):
            self.io.resume(self.tx)
        self.assertAlmostEqual(self.clock, 115)
        self.assertEqual(self.lsof_calls, 0)
        self.assert_preserved()

    def test_missing_proc_that_never_converges_is_not_success(self):
        self.events = [self.event("unloaded"), self.event("unloaded")] + [self.event("missing-proc")] * 400
        with self.assertRaisesRegex(B.BindingError, "owner_resume_unconfirmed"):
            self.io.resume(self.tx)
        self.assertAlmostEqual(self.clock, 115)
        self.assert_preserved()

    def test_bootstrap_failure_has_no_ack_and_is_never_reissued(self):
        self.events = self.ready_events()
        self.bootstrap_error = B.BindingError("native_binding_command_failed")
        with self.assertRaisesRegex(B.BindingError, "native_binding_command_failed"):
            self.io.resume(self.tx)
        self.assertFalse(any(r["bootstrap_acknowledged"] for r in self.records))
        with self.assertRaisesRegex(B.BindingError, "owner_resume_once_claim_exists"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_ambiguous_bootstrap_timeout_is_not_an_ack(self):
        self.events = self.ready_events()
        self.bootstrap_error = subprocess.TimeoutExpired("private synthetic native command", 20)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.io.resume(self.tx)
        self.assertFalse(any(r["bootstrap_acknowledged"] for r in self.records))
        self.assert_preserved()

    def test_diagnostic_write_failure_before_bootstrap_prevents_the_command(self):
        self.events = self.ready_events()
        self.write_error_phase = "bootstrap-requested"
        with self.assertRaises(OSError):
            self.io.resume(self.tx)
        self.assertEqual(self.bootstraps, 0)
        self.assert_preserved()

    def test_diagnostic_write_failure_after_ack_prevents_success_without_retry(self):
        self.events = self.ready_events()
        self.write_error_phase = "bootstrap-acknowledged"
        with self.assertRaises(OSError):
            self.io.resume(self.tx)
        self.assertEqual(self.bootstraps, 1)
        self.assert_preserved()

    def test_verification_diagnostic_write_failure_does_not_return_ready(self):
        self.events = self.ready_events()
        self.write_error_phase = "verification-observed"
        with self.assertRaises(OSError):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_time_spent_persisting_ack_consumes_success_eligibility_deadline(self):
        self.events = self.ready_events()
        def delay(record):
            if record["phase"] == "bootstrap-acknowledged": self.clock += 15
        self.record_hook = delay
        with self.assertRaisesRegex(B.BindingError, "owner_resume_unconfirmed"):
            self.io.resume(self.tx)
        self.assertEqual(self.lsof_calls, 0)
        self.assert_preserved()

    def test_native_print_wait_is_limited_by_remaining_deadline(self):
        self.events = self.ready_events(self.event("missing-pid", print_duration=4.99),
                                        self.event("missing-pid", print_duration=4.99))
        self.events[-2] = self.event(print_duration=6)
        with self.assertRaisesRegex(B.BindingError, "owner_resume_unconfirmed"):
            self.io.resume(self.tx)
        limits = [value for argv, value in self.calls if argv[:2] == ["/bin/launchctl", "print"]]
        self.assertLess(limits[-1], 5)
        self.assertAlmostEqual(self.clock, 115)
        self.assert_preserved()

    def test_native_ps_wait_is_limited_by_remaining_deadline(self):
        self.events = self.ready_events(self.event(print_duration=4, ps_duration=20))
        with self.assertRaisesRegex(B.BindingError, "owner_resume_unconfirmed"):
            self.io.resume(self.tx)
        limits = [value for argv, value in self.calls if argv[0] == "/bin/ps"]
        self.assertEqual(limits[-1], 11)
        self.assertAlmostEqual(self.clock, 115)
        self.assert_preserved()

    def test_native_listener_wait_cannot_cross_deadline_and_return_success(self):
        self.events = self.ready_events(self.event(listener_duration=16))
        with self.assertRaisesRegex(B.BindingError, "owner_resume_unconfirmed"):
            self.io.resume(self.tx)
        self.assert_preserved()

    def test_late_native_success_and_slow_journal_proof_cannot_return_ready(self):
        for values in ({"print_duration": 16, "late_response": True}, {"journal_duration": 16}):
            with self.subTest(values=values):
                self.tx._resume_bootstrap_submitted = False
                self.bootstraps, self.clock = 0, 100
                self.events = self.ready_events(self.event(**values))
                with self.assertRaisesRegex(B.BindingError, "owner_resume_unconfirmed"):
                    self.io.resume(self.tx)
                self.assert_preserved()

    def test_final_diagnostic_delay_cannot_turn_stale_readiness_into_success(self):
        self.events = self.ready_events()
        def delay(record):
            if record["phase"] == "verification-observed": self.clock += 15
        self.record_hook = delay
        with self.assertRaisesRegex(B.BindingError, "owner_resume_unconfirmed"):
            self.io.resume(self.tx)
        self.assert_preserved()


if __name__ == "__main__":
    unittest.main()
