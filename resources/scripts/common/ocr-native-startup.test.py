#!/usr/bin/env python3
"""Owned startup contracts using fake children and clocks; no native execution."""
import copy
from contextlib import ExitStack
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_startup_under_test', Path(__file__).with_name('ocr-native-release.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Clock:
    def __init__(self, step=42):
        self.now, self.step, self.on_sleep = 0.0, step, None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += self.step
        if self.on_sleep:
            self.on_sleep()


class Child:
    def __init__(self, clock, finish=42, returncode=0, pid=701):
        self.clock, self.finish, self.final_code = clock, finish, returncode
        self.pid, self.returncode = pid, None

    def poll(self):
        if self.finish is not None and self.clock.now >= self.finish:
            self.returncode = self.final_code
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self.final_code
        return self.returncode


class Handlers:
    def __init__(self):
        self.original = {signal.SIGINT: object(), signal.SIGTERM: object()}
        self.values = dict(self.original)

    def install(self, signum, handler):
        previous = self.values[signum]
        self.values[signum] = handler
        return previous

    def interrupt(self):
        self.values[signal.SIGTERM](signal.SIGTERM, None)


class NativeStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.output = self.root / 'output'
        self.output.mkdir()
        native = self.output / 'native'
        native.mkdir()
        binary_bytes = b'fake native bytes; this file must never execute'
        digest = m.sha(binary_bytes)
        self.binary = native / ('shiba_ocr_prod_' + digest[:12])
        self.binary.write_bytes(binary_bytes)
        self.binary.chmod(0o700)
        (native / 'artifact.json').write_text(json.dumps({'binary': self.binary.name, 'binary_sha256': digest}))
        self.smoke = {'ready': True, 'initialization': {'phase': 'ready', 'duration_ms': 42000.0}}
        self.payload = json.dumps(self.smoke).encode()
        self.log = io.BytesIO()
        self.clock, self.handlers = Clock(), Handlers()
        self.identity = {'pid': 701, 'pgid': 701, 'start_sec': 100, 'start_usec': 42,
                         'executable': str(self.binary)}
        self.process = Child(self.clock)
        self.launches, self.cleanup_calls = [], []

    def tearDown(self):
        self.temporary.cleanup()

    def popen(self, command, **kwargs):
        self.launches.append((command, kwargs))
        self.assertEqual(command, [str(self.binary), '-check-compatibility'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['env']['OCR_RUNTIME_LANE'], 'prod')
        self.assertEqual(kwargs['env']['OCR_COLD_START_TIMEOUT_SECONDS'], '600')
        self.assertEqual(kwargs['env']['UNCHANGED_FIXTURE'], 'kept')
        kwargs['stdout'].write(self.payload)
        kwargs['stdout'].flush()
        kwargs['stderr'].write(b'private native fixture stderr\n')
        kwargs['stderr'].flush()
        logs = Path(kwargs['env']['OCRSVC_LOG_DIR'])
        (logs / 'ocrsvc-fixture.log').write_text('phase=ocr_recognition fixture\n')
        return self.process

    def cleanup(self, process, deadline):
        self.cleanup_calls.append(process.pid)
        if process.poll() is None:
            process.returncode = -signal.SIGTERM
        return True

    def patches(self, *, sample=None, cleanup=None):
        stack = ExitStack()
        stack.enter_context(patch.object(m.pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(self.root))))
        stack.enter_context(patch.object(m.time, 'monotonic', side_effect=self.clock.monotonic))
        stack.enter_context(patch.object(m.time, 'sleep', side_effect=self.clock.sleep))
        stack.enter_context(patch.object(m.signal, 'signal', side_effect=self.handlers.install))
        stack.enter_context(patch.object(m, 'startup_process_identity', return_value=self.identity))
        stack.enter_context(patch.object(m, 'cleanup_owned_process', side_effect=cleanup or self.cleanup))
        stack.enter_context(patch.object(m.subprocess, 'Popen', side_effect=self.popen))
        stack.enter_context(patch.object(m.subprocess, 'run', side_effect=AssertionError('unmocked subprocess forbidden')))
        if sample is not None:
            stack.enter_context(patch.object(m, 'sample_startup_process', side_effect=sample))
        return stack

    def report(self):
        return json.loads((self.output / 'startup-diagnostic/diagnostic.json').read_text())

    def call(self):
        return m.startup_compatibility(self.binary, self.output, {'UNCHANGED_FIXTURE': 'kept'}, self.log)

    def assert_preserved(self):
        self.assertEqual(self.handlers.values, self.handlers.original)
        self.assertEqual(self.binary.read_bytes(), b'fake native bytes; this file must never execute')
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.report()['execution_attempts'], 1)
        self.assertTrue(self.report()['cleanup_complete'])
        self.assertIn(b'phase=ocr_recognition fixture', self.log.getvalue())

    def test_success_returns_unchanged_bytes_and_preserves_existing_cache(self):
        cache = self.root / 'Library/Caches' / self.binary.name
        cache.mkdir(parents=True)
        marker = cache / 'partial-model-fixture'
        marker.write_bytes(b'existing partial assets')
        before = marker.stat()
        with self.patches():
            actual = self.call()
        self.assertEqual(actual, self.payload)
        self.assertEqual(marker.read_bytes(), b'existing partial assets')
        self.assertEqual(marker.stat().st_ino, before.st_ino)
        report = self.report()
        self.assertEqual(report['cache_state'], 'existing_namespace_preserved')
        self.assertTrue(report['cache_before']['exists'])
        self.assertEqual(report['release_budget'], 'PASS')
        self.assertEqual(report['elapsed_seconds'], 42)
        self.assertFalse(report['sample_attempted'])
        self.assertEqual(self.cleanup_calls, [701])
        self.assert_preserved()

    def test_numeric_exit_and_partial_native_logs_survive_failure(self):
        self.process.final_code = 124
        self.payload = b''
        with self.patches(), self.assertRaisesRegex(ValueError, 'returncode=124'):
            self.call()
        self.assertEqual(self.report()['returncode'], 124)
        self.assertEqual(self.report()['release_budget'], 'FAIL')
        self.assertIn(b'private native fixture stderr', self.log.getvalue())
        self.assert_preserved()

    def test_outer_limit_rejects_pass_even_if_native_claims_under_limit(self):
        self.clock.step, self.process.finish = 615.001, 615.001
        with self.patches(), self.assertRaisesRegex(ValueError, 'wall time'):
            self.call()
        report = self.report()
        self.assertEqual(report['returncode'], 0)
        self.assertEqual(report['release_budget'], 'FAIL')
        self.assertEqual((self.output / 'startup-diagnostic/smoke.json').read_bytes(), self.payload)
        self.assert_preserved()

    def test_native_limit_rejects_pass_even_if_wall_under_limit(self):
        self.smoke['initialization']['duration_ms'] = 600001
        self.payload = json.dumps(self.smoke).encode()
        with self.patches(), self.assertRaisesRegex(ValueError, 'native startup duration'):
            self.call()
        self.assertEqual(self.report()['release_budget'], 'FAIL')
        self.assert_preserved()

    def test_155_second_preparation_passes_with_one_observational_sample(self):
        self.clock.step, self.process.finish = 31, 155
        self.smoke['initialization']['duration_ms'] = 155000
        self.payload = json.dumps(self.smoke).encode()
        samples = []
        def observe(process, identity, evidence, report, log):
            samples.append((process.pid, self.clock.now))
            report.update(sample_attempted=True, sample_returncode=0, sample_cleanup_complete=True)
        with self.patches(sample=observe):
            actual = self.call()
        report = self.report()
        self.assertEqual(actual, self.payload)
        self.assertEqual(samples, [(701, 124)])
        self.assertEqual(report['release_budget'], 'PASS')
        self.assertEqual(report['release_limit_seconds'], 600)
        self.assertEqual(report['sample_after_seconds'], 120)
        self.assertEqual(report['elapsed_seconds'], 155)
        self.assert_preserved()

    def test_outer_timeout_uses_one_sample_and_cleans_owned_native(self):
        self.clock.step, self.process.finish = 123, None
        samples = []
        def unavailable(process, identity, evidence, report, log):
            samples.append(process.pid)
            report.update(sample_attempted=True, sample_error='fixture unavailable')
        with self.patches(sample=unavailable), self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.call()
        self.assertEqual(caught.exception.timeout, 615)
        self.assertEqual(samples, [701])
        self.assertEqual(self.cleanup_calls, [701])
        self.assertEqual(self.report()['sample_error'], 'fixture unavailable')
        self.assert_preserved()

    def test_cancellation_and_second_signal_cleanup_restore_handlers(self):
        self.process.finish = None
        self.clock.on_sleep = self.handlers.interrupt
        def cleanup(process, deadline):
            self.handlers.interrupt()  # Must latch while cleaning, not interrupt reaping.
            return self.cleanup(process, deadline)
        with self.patches(cleanup=cleanup), self.assertRaises(InterruptedError):
            self.call()
        self.assertEqual(self.cleanup_calls, [701])
        self.assertEqual(self.report()['interrupted_signal'], signal.SIGTERM)
        self.assert_preserved()

    def test_unconfirmed_cleanup_never_returns_success(self):
        with self.patches(cleanup=lambda process, deadline: False), \
                self.assertRaisesRegex(ValueError, 'cleanup is unconfirmed'):
            self.call()
        self.assertFalse(self.report()['cleanup_complete'])
        self.assertEqual(self.report()['release_budget'], 'FAIL')
        self.assertEqual(self.handlers.values, self.handlers.original)

    def test_cleanup_exception_retains_diagnostic_and_partial_output(self):
        def failed_cleanup(process, deadline):
            raise subprocess.TimeoutExpired(['owned-fixture'], 10)
        with self.patches(cleanup=failed_cleanup), self.assertRaises((ValueError, subprocess.TimeoutExpired)):
            self.call()
        report = self.report()
        self.assertFalse(report['cleanup_complete'])
        self.assertEqual(report['release_budget'], 'FAIL')
        self.assertEqual((self.output / 'startup-diagnostic/stdout.log').read_bytes(), self.payload)
        self.assertIn(b'private native fixture stderr', self.log.getvalue())
        self.assertEqual(self.handlers.values, self.handlers.original)

    def test_sampler_rejects_drift_and_never_targets_another_process(self):
        evidence = self.root / 'sample'
        evidence.mkdir()
        binary_identity = m.executable_identity(self.binary)
        report = {'process_identity': self.identity, 'sample_attempted': False}
        drift = dict(self.identity, start_sec=101)
        with patch.object(m, 'startup_process_identity', return_value=drift), \
                patch.object(m.subprocess, 'Popen', side_effect=AssertionError('sampling drift forbidden')) as launch:
            m.sample_startup_process(self.process, binary_identity, evidence, report, self.log)
        self.assertTrue(report['sample_attempted'])
        self.assertIn('changed before sampling', report['sample_error'])
        launch.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'already consumed'):
            m.sample_startup_process(self.process, binary_identity, evidence, report, self.log)

    def test_sampler_timeout_has_one_bounded_command_and_reaps_sampler(self):
        evidence = self.root / 'sample'
        evidence.mkdir()
        binary_identity = m.executable_identity(self.binary)
        report = {'process_identity': self.identity, 'sample_attempted': False}
        sampler = Child(self.clock, finish=None, returncode=124, pid=702)
        waits = []
        def wait(timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired(['/usr/bin/sample'], timeout)
        sampler.wait = wait
        with patch.object(m, 'startup_process_identity', return_value=self.identity), \
                patch.object(m.subprocess, 'Popen', return_value=sampler) as launch, \
                patch.object(m, 'cleanup_owned_process', side_effect=self.cleanup):
            m.sample_startup_process(self.process, binary_identity, evidence, report, self.log)
        self.assertEqual(launch.call_count, 1)
        command = launch.call_args.args[0]
        self.assertEqual(command[:5], ['/usr/bin/sample', '701', '5', '10', '-file'])
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual(waits, [20])
        self.assertTrue(report['sample_timed_out'])
        self.assertTrue(report['sample_cleanup_complete'])
        self.assertEqual(self.cleanup_calls, [702])

    def test_cancellation_inside_sampler_cleans_both_children(self):
        self.clock.step, self.process.finish = 120, None
        sampler = Child(self.clock, finish=None, pid=702)
        def wait(timeout=None):
            self.handlers.interrupt()
        sampler.wait = wait
        original_popen = self.popen
        launches = []
        def popen(command, **kwargs):
            if command[0] == '/usr/bin/sample':
                launches.append(command)
                return sampler
            return original_popen(command, **kwargs)
        cleanup_windows = []
        def cleanup(process, deadline):
            cleanup_windows.append((process.pid, self.clock.now, deadline))
            self.handlers.interrupt()  # A repeated TERM cannot interrupt cleanup of either child.
            self.clock.now += 4 if process.pid == 702 else 2
            return self.cleanup(process, deadline)
        with self.patches(cleanup=cleanup), patch.object(m.subprocess, 'Popen', side_effect=popen), \
                self.assertRaises(InterruptedError):
            self.call()
        self.assertEqual(len(launches), 1)
        self.assertEqual(self.cleanup_calls, [702, 701])
        self.assertEqual(cleanup_windows, [(702, 120, 125), (701, 124, 130)])
        self.assertEqual(self.report()['cleanup_remaining_seconds'], 4)
        self.assertTrue(self.report()['sample_cleanup_complete'])
        self.assert_preserved()


if __name__ == '__main__':
    unittest.main()
