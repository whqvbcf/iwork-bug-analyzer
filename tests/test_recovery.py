import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from iwork_collector.archives import process_file
from iwork_collector.service import Service
from iwork_collector.storage import Workspace, atomic_json, sha256_file


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cache = Workspace(self.base / 'cache')
        self.cache_folder = self.cache.bug_dir('123')
        self.original = self.base / 'logs.zip'
        with zipfile.ZipFile(self.original, 'w') as archive:
            archive.writestr('device/system.log', 'verified log data')
        self.digest = sha256_file(self.original)
        result = process_file(self.cache, self.cache_folder, self.original, 'logs.zip', 'cached')
        self.manifest = self.cache_folder / '.iwork/manifest.json'
        atomic_json(self.manifest, dict(result, bug_id='123'))
        self.catalog = self.base / 'recovery-catalog.json'
        self.register()
        self.service = Service(self.base / 'work', downloads_root=self.base / 'Downloads')
        self.service.recovery_catalog = self.catalog
        self.addCleanup(self.service.close)

    def register(self):
        atomic_json(self.catalog, {'schema_version': 1, 'entries': [{
            'bug_id': '123', 'archive_name': 'logs.zip', 'archive_sha256': self.digest,
            'folder': str(self.cache_folder), 'tree': 'logs',
            'manifest_sha256': sha256_file(self.manifest)}]})

    def capture(self):
        return self.service.capture({'bug_id': '123', 'title': 'confirmed',
            'coverage': {'complete': True, 'gaps': []},
            'attachments': [{'name': 'logs.zip', 'local_path': str(self.original), 'transport': 'local'}]}, trusted=True)

    def test_reuses_verified_files_without_extracting_or_allocating_another_copy(self):
        captured = self.capture()
        with patch('iwork_collector.service.process_file', side_effect=AssertionError('must not extract again')):
            with patch('iwork_collector.service.shutil.disk_usage',
                       return_value=shutil._ntuple_diskusage(2 * 1024**3, 0, 1024**3 + 65536)):
                result = self.service.run(captured['job_id'])
        self.assertEqual(result['status'], 'ready')
        target = Path(result['folder']) / 'logs/device/system.log'
        source = self.cache_folder / 'logs/device/system.log'
        self.assertEqual(target.read_text(), 'verified log data')
        self.assertEqual(target.stat().st_ino, source.stat().st_ino)
        self.assertEqual(target.stat().st_dev, source.stat().st_dev)
        manifest = json.loads((Path(result['folder']) / '.iwork/manifest.json').read_text())
        self.assertTrue(all(a['deleted'] for a in manifest['archives']))
        self.assertEqual(self.service.state.get(captured['job_id']).get('ingestions'), {})
        self.assertFalse(list((self.service.root / '.iwork/staging').rglob('*.zip')))

    def recover(self, bug_id='123', digest=None):
        from iwork_collector.recovery import recover_archive
        return recover_archive(self.service.workspace, self.service.workspace.bug_dir('123'),
            'logs.zip', digest or self.digest, 'target', bug_id, self.catalog)

    def test_changed_cached_file_is_rejected_without_publishing(self):
        (self.cache_folder / 'logs/device/system.log').write_text('corrupted content')
        with self.assertRaisesRegex(ValueError, '恢复.*校验'):
            self.recover()
        self.assertFalse((self.service.workspace.bug_dir('123') / 'logs').exists())
        self.assertTrue(self.original.exists())

    def test_wrong_bug_or_archive_digest_does_not_reuse_cache(self):
        self.assertIsNone(self.recover(bug_id='456'))
        self.assertIsNone(self.recover(digest='a' * 64))

    def test_symlink_and_unlisted_file_are_rejected(self):
        source = self.cache_folder / 'logs/device/system.log'
        source.unlink()
        source.symlink_to(self.original)
        with self.assertRaisesRegex(ValueError, '恢复'):
            self.recover()
        source.unlink()
        source.write_text('verified log data')
        (source.parent / 'unlisted.log').write_text('other evidence')
        with self.assertRaisesRegex(ValueError, '恢复'):
            self.recover()

    def test_crash_after_publication_replays_from_intact_cache(self):
        from iwork_collector import recovery
        real_replace = os.replace
        def crash_after_publish(source, target):
            result = real_replace(source, target)
            if Path(target).name == 'logs':
                raise SystemExit('power interrupted')
            return result
        with patch.object(recovery.os, 'replace', crash_after_publish):
            with self.assertRaises(SystemExit):
                self.recover()
        result = self.recover()
        self.assertTrue(result['artifacts'][0]['reused'])
        self.assertEqual(len(list(self.service.workspace.bug_dir('123').rglob('system.log'))), 1)
        self.assertTrue((self.cache_folder / 'logs/device/system.log').is_file())

