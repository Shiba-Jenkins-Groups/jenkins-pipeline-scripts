#!/usr/bin/env python3
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('runtime_verify', Path(__file__).with_name('runtime-image-verify.py'))
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class RuntimeVerification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.labels = {}
        self.product = verify.APP
        self.fail = None

    def fake(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == 'run':
            name = args[args.index('--name')+1]
            label = args[args.index('--label')+1].split('=',1)[1]
            self.labels[name] = {'shiba.ci.verification': label}
            if self.fail == 'bootstrap' and name.endswith('-bootstrap'):
                raise RuntimeError('bootstrap failure')
        if args[0] == 'inspect':
            if args[2] == '{{json .Config.Labels}}':
                return subprocess.CompletedProcess(args,0,json.dumps(self.labels[args[-1]]),'')
            stopped = any(a[0] == 'stop' for a in self.calls)
            return subprocess.CompletedProcess(args,0,json.dumps({'Running':not stopped,
                'ExitCode':137 if self.fail == 'shutdown' else 0, 'OOMKilled':False}),'')
        if self.fail == 'cleanup' and args[:2] == ('volume','rm'):
            return subprocess.CompletedProcess(args,1,'','busy')
        if self.fail == 'health' and args[0] == 'exec':
            raise RuntimeError('health failure')
        return subprocess.CompletedProcess(args,0,'','')

    def run_case(self):
        with patch.object(verify,'identity',return_value=(self.product,'registry/app@sha256:'+'a'*64,'b'*40)), patch.object(verify,'docker',side_effect=self.fake), patch('builtins.print'):
            return verify.verify(self.root)

    def test_app_bootstrap_uses_only_isolated_volume_and_exact_image(self):
        self.assertEqual(self.run_case()['cleanup'], 'PASS')
        runs = [a for a in self.calls if a[0]=='run']
        self.assertEqual(len(runs),2)
        for args in runs:
            self.assertIn('none',args)
            self.assertIn('65532:65532',args)
            self.assertIn('registry/app@sha256:'+'a'*64,args)
            self.assertFalse(any('type=bind' in a for a in args))
        self.assertIn('bootstrap-empty',runs[0])
        self.assertTrue(any(a[:2]==('volume','rm') for a in self.calls))

    def test_recognition_never_creates_database_or_volume(self):
        self.product = verify.RECOGNITION
        self.run_case()
        self.assertFalse(any(a[0]=='volume' for a in self.calls))
        self.assertFalse(any('bootstrap-empty' in a for a in self.calls))

    def test_each_failure_cleans_up_and_cannot_issue_pass_receipt(self):
        for point in ['bootstrap','health','shutdown','cleanup']:
            with self.subTest(point=point):
                self.fail,self.calls,self.labels = point,[],{}
                with self.assertRaises(RuntimeError): self.run_case()
                self.assertFalse((self.root/'reports/runtime-image.json').exists())
                self.assertTrue(any(a[0]=='rm' for a in self.calls))
                self.assertTrue(any(a[:2]==('volume','rm') for a in self.calls))

    def test_wrong_branch_or_build_never_starts_container(self):
        (self.root/'image-ref.txt').write_text('APP_NAME='+verify.APP+'\nBRANCH=develop\nBUILD_NUMBER=1\n')
        with patch.object(verify,'docker') as docker, self.assertRaises(ValueError):
            verify.identity(self.root)
        docker.assert_not_called()


if __name__=='__main__': unittest.main()
