"""最小災難恢復契約：純離線簽章與暫存檔，不連 Docker／產品 DB。"""
import copy
import datetime as dt
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('fixtures', Path(__file__).with_name('release-deploy.test.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
runner = fixtures.runner


class Rebuild(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Deployment()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        original = copy.deepcopy(f.request)
        self.request = {**original, 'schema_version': 3, 'mode': 'published-prod-disaster-rebuild',
            'published': {'coordinator_build': 74, 'promotion': original['promotion'],
                'finalization': original['finalization'], 'request': runner.control.sign(original, f.key)},
            'prod_build': 104, 'authorized_by': 'reviewer', 'restore_missing_runtime': True,
            'target_engine_id': '9f2f05b6-4637-45c7-ab13-cf5a35e2a539',
            'artifact_sha256': 'd'*64, 'deployment_script_revision': f.commit, 'library_revision': 'f'*40}
        self.request.pop('promotion')
        self.request.pop('finalization')

    def validate(self, request):
        return runner.request_identity(runner.control.sign(request, self.fixture.key), self.fixture.key, self.fixture.now)

    def test_valid_and_old_publication_tampering(self):
        self.assertEqual(self.validate(self.request)['prod_build'], 104)
        self.request['published']['finalization']['payload']['commit'] = 'c'*40
        with self.assertRaises(ValueError): self.validate(self.request)

    def test_expiry_old_build_and_missing_authorization_rejected(self):
        for changes in [dict(prod_build=103), dict(restore_missing_runtime=False),
                        dict(expires_at=(self.fixture.now-dt.timedelta(seconds=1)).isoformat())]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.validate({**self.request, **changes})

    def test_cold_restore_needs_original_data_and_exclusive_owner(self):
        f = self.fixture
        with self.assertRaises(ValueError): runner.missing_runtime([], f.root)
        (f.root/'data/db/app/shiba-go-ditch-api.db').write_bytes(b'offline fixture only')
        self.assertIsNone(runner.missing_runtime([], f.root)['Id'])
        with self.assertRaises(ValueError): runner.missing_runtime([f.container], f.root)
        other = copy.deepcopy(f.container)
        other['Config']['Labels'] = {}
        with self.assertRaises(ValueError): runner.missing_runtime([other], f.root)


if __name__ == '__main__': unittest.main()
