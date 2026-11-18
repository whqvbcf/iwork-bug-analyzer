import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from iwork_collector.storage import Workspace, atomic_json, sha256_file, validate_name


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = Workspace(self.root / 'workspace')
        self.folder = self.ws.bug_dir('123', now=datetime(2026, 11, 11))
        self.source = self.root / 'source'
        self.source.write_bytes(b'first')

    def test_folder_reused_on_later_day_and_after_missing_index(self):
        self.assertEqual(self.folder.name, 'bug_261111_123')
        self.assertEqual(self.ws.bug_dir('123', now=datetime(2026, 11, 12)), self.folder)
        (self.ws.root / '.iwork/workspaces.json').unlink()
        self.assertEqual(Workspace(self.ws.root).bug_dir('123'), self.folder)

    def test_corrupt_cache_recovers_verified_metadata(self):
        (self.ws.root / '.iwork/workspaces.json').write_text('{')
        self.assertEqual(self.ws.bug_dir('123'), self.folder)

    def test_legacy_folder_migrates_without_changing_original_date_or_files(self):
        legacy = self.ws.root / 'bug_456_261110'
        atomic_json(legacy / '.iwork/workspace.json', {'bug_id': '456', 'source': 'iwork'})
        (legacy / '原始日志.log').write_text('preserved evidence')
        folder = self.ws.bug_dir('456', now=datetime(2026, 11, 13))
        self.assertEqual(folder.name, 'bug_261110_456')
        self.assertFalse(legacy.exists())
        self.assertEqual((folder / '原始日志.log').read_text(), 'preserved evidence')
        index = json.loads((self.ws.root / '.iwork/workspaces.json').read_text())
        self.assertEqual(index['iwork:456'], [folder.name])
        self.assertEqual(self.ws.bug_dir('456'), folder)

    def test_six_digit_bug_identity_is_not_confused_with_folder_date(self):
        first = self.ws.bug_dir('261112', now=datetime(2026, 11, 11))
        second = self.ws.bug_dir('261111', now=datetime(2026, 11, 12))
        self.assertEqual(first.name, 'bug_261111_261112')
        self.assertEqual(second.name, 'bug_261112_261111')
        self.assertEqual(self.ws.bug_dir('261112'), first)

    def test_unidentified_new_format_folder_is_not_overwritten(self):
        (self.ws.root / 'bug_261111_456').mkdir()
        with self.assertRaises(ValueError):
            self.ws.bug_dir('456', now=datetime(2026, 11, 11))

    def test_unidentified_folder_and_other_source_are_not_merged(self):
        with self.assertRaises(ValueError):
            self.ws.bug_dir('123', source='another')
        (self.ws.root / 'bug_456_261111').mkdir()
        with self.assertRaises(ValueError):
            self.ws.bug_dir('456', now=datetime(2026, 11, 11))

    def test_publish_keeps_exact_name_and_source(self):
        record = self.ws.publish(self.folder, self.source, '中文 log #1.txt', 'attachment-1')
        self.assertEqual(record['path'], '中文 log #1.txt')
        self.assertEqual(record['name'], '中文 log #1.txt')
        self.assertEqual(record['sha256'], hashlib.sha256(b'first').hexdigest())
        self.assertEqual(record['size'], 5)
        self.assertFalse(record['reused'])
        self.assertEqual(self.source.read_bytes(), b'first')

    def test_same_content_reused_and_changed_content_isolated(self):
        first = self.ws.publish(self.folder, self.source, 'log.txt', 'a')
        same = self.ws.publish(self.folder, self.source, 'log.txt', 'b')
        self.assertTrue(same['reused'])
        self.assertEqual(first['path'], same['path'])
        self.source.write_bytes(b'second')
        changed = self.ws.publish(self.folder, self.source, 'log.txt', 'b')
        self.assertEqual(changed['path'], 'files/b/log.txt')
        self.source.write_bytes(b'third')
        third = self.ws.publish(self.folder, self.source, 'log.txt', 'b')
        self.assertEqual(Path(third['path']).name, 'log.txt')
        self.assertNotEqual(third['path'], changed['path'])
        self.assertEqual((self.folder / first['path']).read_bytes(), b'first')

    def test_reserved_names_are_isolated(self):
        for name in ('bugreport.md', '.iwork', 'files'):
            record = self.ws.publish(self.folder, self.source, name, 'a')
            self.assertEqual(record['path'], f'files/a/{name}')

    def test_modified_and_missing_files_are_not_reused(self):
        record = self.ws.publish(self.folder, self.source, 'log.txt', 'a')
        (self.folder / record['path']).write_bytes(b'user content')
        replacement = self.ws.publish(self.folder, self.source, 'log.txt', 'b')
        self.assertEqual(replacement['path'], 'files/b/log.txt')
        (self.folder / replacement['path']).unlink()
        replacement = self.ws.publish(self.folder, self.source, 'log.txt', 'b')
        self.assertFalse(replacement['reused'])

    def test_name_and_id_validation_and_symlink_escape(self):
        for value in ('', '../x', '/tmp/x', 'a/b', 'a\\b', '.', '..', 'bad\x00name'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_name(value)
        for value in ('../123', 'abc', '', '123/4'):
            with self.assertRaises(ValueError):
                self.ws.bug_dir(value)
        with self.assertRaises(ValueError):
            self.ws.staging_dir('../job', 'a')
        external = self.root / 'external'
        external.mkdir()
        (self.folder / 'evil').symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.ws.publish(self.folder, self.source, 'evil', 'a')
        self.assertEqual(list(external.iterdir()), [])

    def test_atomic_json_and_staging(self):
        path = self.root / 'nested/value.json'
        atomic_json(path, {'name': '中文'})
        self.assertEqual(json.loads(path.read_text()), {'name': '中文'})
        stage = self.ws.staging_dir('job-1', 'item-1')
        self.assertEqual(stage, self.ws.root / '.iwork/staging/job-1/item-1')
        self.assertTrue(stage.is_dir())
        self.assertEqual(sha256_file(self.source), hashlib.sha256(b'first').hexdigest())

    def test_corrupt_folder_identity_is_not_reused(self):
        (self.folder / '.iwork/workspace.json').write_text('{')
        with self.assertRaises(ValueError):
            self.ws.bug_dir('123')

    def test_multiple_verified_bug_folders_are_not_silently_merged(self):
        other = self.ws.root / 'bug_123_261112'
        atomic_json(other / '.iwork/workspace.json', {'bug_id': '123', 'source': 'iwork'})
        with self.assertRaises(ValueError):
            self.ws.bug_dir('123')

    def test_concurrent_folder_creation_reuses_one_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as executor:
            folders = list(executor.map(lambda n: Workspace(self.ws.root).bug_dir('777'), range(12)))
        self.assertEqual(len(set(folders)), 1)
        self.assertEqual(len(list(self.ws.root.glob('bug_*_777'))), 1)
