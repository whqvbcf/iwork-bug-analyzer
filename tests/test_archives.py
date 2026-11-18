import gzip
import hashlib
import io
import tarfile
import tempfile
import unittest
import warnings
import zipfile
import shutil
from pathlib import Path

from iwork_collector.archives import process_file, _Budget
from iwork_collector.storage import Workspace


def zip_bytes(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, data in members:
            archive.writestr(name, data)
    return stream.getvalue()


def tar_bytes(name, content=b'data', kind=tarfile.REGTYPE, linkname=''):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = linkname
        member.size = len(content) if kind == tarfile.REGTYPE else 0
        archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


class ArchiveTests(unittest.TestCase):
    def test_default_budget_accepts_large_vehicle_log_and_longer_processing(self):
        from unittest.mock import patch
        with patch('iwork_collector.archives.time.monotonic', return_value=0):
            budget = _Budget(None)
            budget.member(10390004678)
            budget.bytes = 10390004678
            budget.member(15833097999)
        with patch('iwork_collector.archives.time.monotonic', return_value=180):
            budget.check()

    def test_low_disk_keeps_archive_without_publishing_partial_logs(self):
        from unittest.mock import patch
        with patch('iwork_collector.archives.shutil.disk_usage',
                   return_value=shutil._ntuple_diskusage(100, 99, 1)):
            path, result = self.process('low-disk.zip', zip_bytes([('log', b'hello')]))
        self.assertTrue(path.exists())
        self.assertEqual(result['artifacts'], [])
        self.assertTrue(any('磁盘空间不足' in gap for gap in result['gaps']))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws = Workspace(Path(self.temp.name) / 'workspace')
        self.folder = self.ws.bug_dir('123')
        self.stage = self.ws.staging_dir('job', 'item')

    def process(self, name, content, **kwargs):
        path = self.stage / name
        path.write_bytes(content)
        return path, process_file(self.ws, self.folder, path, name, 'attachment-1', **kwargs)

    def test_plain_file_published_without_mutating_source(self):
        path, result = self.process('中文 log.txt', b'abc')
        self.assertEqual(result['gaps'], [])
        self.assertEqual(result['archives'], [])
        self.assertEqual(result['artifacts'][0]['path'], '中文 log.txt')
        self.assertEqual(path.read_bytes(), b'abc')

    def test_nested_zip_tar_gzip_success_keeps_members_and_deletes_originals(self):
        nested = gzip.compress(tar_bytes('设备/log #1.txt', b'hello'))
        outer = zip_bytes([('sub/log.tar.gz', nested), ('readme.txt', b'notes')])
        path, result = self.process('设备 logs.zip', outer)
        self.assertFalse(path.exists())
        self.assertEqual(result['gaps'], [])
        self.assertEqual(len(result['archives']), 2)
        self.assertTrue(all(r['deleted'] for r in result['archives']))
        self.assertEqual(result['archives'][0]['sha256'], hashlib.sha256(outer).hexdigest())
        self.assertEqual((self.folder / '设备 logs/sub/log/设备/log #1.txt').read_bytes(), b'hello')
        self.assertEqual((self.folder / '设备 logs/readme.txt').read_bytes(), b'notes')
        self.assertEqual(set(r['path'] for r in result['artifacts']), {
            '设备 logs/sub/log/设备/log #1.txt', '设备 logs/readme.txt'})
        self.assertEqual(list(self.folder.rglob('*.zip')), [])
        self.assertEqual(list(self.folder.rglob('*.gz')), [])

    def test_gzip_single_member_uses_header_name(self):
        stream = io.BytesIO()
        with gzip.GzipFile(filename='原始日志.txt', mode='wb', fileobj=stream) as member:
            member.write(b'hello')
        # gzip filename is Latin-1 only; Python deliberately omits this header.
        path, result = self.process('logcat.txt.gz', stream.getvalue())
        self.assertFalse(path.exists())
        self.assertEqual(result['artifacts'][0]['path'], 'logcat.txt')
        stream = io.BytesIO()
        with gzip.GzipFile(filename='actual.log', mode='wb', fileobj=stream) as member:
            member.write(b'data')
        _, result = self.process('download.gz', stream.getvalue())
        self.assertEqual(result['artifacts'][0]['path'], 'actual.log')

    def test_corruption_and_nested_failure_publish_nothing_keep_staging(self):
        for name, data in [('corrupt.zip', b'PK\x03\x04bad'),
                           ('outer.zip', zip_bytes([('readme.txt', b'ok'), ('nested.zip', b'bad')]))]:
            with self.subTest(name=name):
                path, result = self.process(name, data)
                self.assertTrue(path.exists())
                self.assertTrue(result['gaps'])
                self.assertEqual(result['artifacts'], [])
                self.assertFalse((self.folder / name.removesuffix('.zip')).exists())
                self.assertTrue(all(not item['deleted'] for item in result['archives']))

    def test_archive_traversal_absolute_windows_and_duplicate_names_are_rejected(self):
        for member in ('../outside', '/outside', 'C:/outside', '..\\outside', 'a/../../outside'):
            with self.subTest(member=member):
                path, result = self.process('unsafe.zip', zip_bytes([(member, b'bad')]))
                self.assertTrue(path.exists())
                self.assertTrue(result['gaps'])
                self.assertEqual(result['artifacts'], [])
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            _, result = self.process('duplicate.zip', zip_bytes([('same', b'one'), ('same', b'two')]))
        self.assertTrue(result['gaps'])
        self.assertEqual(result['artifacts'], [])
        self.assertFalse((self.ws.root / 'outside').exists())

    def test_tar_symlink_and_zip_symlink_rejected(self):
        _, result = self.process('link.tar', tar_bytes('link', kind=tarfile.SYMTYPE, linkname='/tmp'))
        self.assertTrue(result['gaps'])
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            member = zipfile.ZipInfo('link')
            member.create_system = 3
            member.external_attr = (0o120777 << 16)
            archive.writestr(member, '/tmp')
        _, result = self.process('link.zip', stream.getvalue())
        self.assertTrue(result['gaps'])
        self.assertEqual(result['artifacts'], [])

    def test_expansion_count_size_and_nesting_budgets(self):
        cases = [({'max_expanded_size': 3}, zip_bytes([('large', b'1234')])),
                 ({'max_files': 1}, zip_bytes([('a', b'a'), ('b', b'b')])),
                 ({'max_nesting': 1}, zip_bytes([('inner.zip', zip_bytes([('a', b'a')]))]))]
        for limits, archive in cases:
            with self.subTest(limits=limits):
                path, result = self.process('limit.zip', archive, limits=limits)
                self.assertTrue(path.exists())
                self.assertTrue(result['gaps'])
                self.assertEqual(result['artifacts'], [])
        _, result = self.process('large.log.gz', gzip.compress(b'1234'), limits={'max_expanded_size': 3})
        self.assertTrue(result['gaps'])

    def test_expansion_directory_collision_isolated_as_whole_tree(self):
        (self.folder / 'logs').mkdir()
        (self.folder / 'logs/a').write_bytes(b'user')
        _, result = self.process('logs.zip', zip_bytes([('a', b'new'), ('sub/b', b'other')]))
        self.assertEqual(result['gaps'], [])
        self.assertEqual((self.folder / 'logs/a').read_bytes(), b'user')
        self.assertEqual((self.folder / 'files/attachment-1/logs/a').read_bytes(), b'new')
        self.assertEqual((self.folder / 'files/attachment-1/logs/sub/b').read_bytes(), b'other')

    def test_success_repeat_reuses_tree_and_reserved_directory_isolated(self):
        data = zip_bytes([('log.txt', b'a'), ('note.txt', b'b')])
        _, first = self.process('logs.zip', data)
        _, second = self.process('logs.zip', data)
        self.assertEqual(first['artifacts'][0]['path'], second['artifacts'][0]['path'])
        self.assertTrue(second['artifacts'][0]['reused'])
        _, result = self.process('files.zip', data)
        self.assertEqual(result['artifacts'][0]['path'], 'files/attachment-1/files/log.txt')

    def test_unsupported_archive_is_gap_and_stays_staging(self):
        path, result = self.process('logs.7z', b'7z\xbc\xaf\x27\x1c')
        self.assertTrue(path.exists())
        self.assertTrue(result['gaps'])
        self.assertEqual(result['artifacts'], [])

    def test_empty_archive_is_success_and_provenance_retained(self):
        path, result = self.process('empty.zip', zip_bytes([]))
        self.assertFalse(path.exists())
        self.assertEqual(result['gaps'], [])
        self.assertTrue((self.folder / 'empty').is_dir())
        self.assertEqual(result['archives'][0]['members'], [])

    def test_invalid_source_key_rejected_even_for_failed_archive(self):
        path = self.stage / 'bad.zip'
        path.write_bytes(b'bad')
        with self.assertRaises(ValueError):
            process_file(self.ws, self.folder, path, path.name, '../source')

    def test_gzip_crc_failure_and_unsafe_header_remain_staged(self):
        damaged = bytearray(gzip.compress(b'hello'))
        damaged[-8] ^= 1
        for name, content in [('crc.gz', damaged),
                              ('unsafe.gz', gzip.compress(b'hello')[:3] + b'\x08' +
                               gzip.compress(b'hello')[4:10] + b'../outside\0' +
                               gzip.compress(b'hello')[10:])]:
            with self.subTest(name=name):
                path, result = self.process(name, content)
                self.assertTrue(path.exists())
                self.assertTrue(result['gaps'])
                self.assertEqual(result['artifacts'], [])

    def test_local_original_is_preserved_after_managed_copy_deleted(self):
        path = Path(self.temp.name) / 'user-original.zip'
        path.write_bytes(zip_bytes([('log.txt', b'original')]))
        result = process_file(self.ws, self.folder, path, path.name, 'local-1')
        self.assertTrue(path.exists())
        self.assertEqual(result['gaps'], [])
        self.assertTrue(result['archives'][0]['deleted'])
        self.assertTrue(result['archives'][0]['original_preserved'])
        self.assertEqual(list((self.ws.root / '.iwork/staging').iterdir()), [self.stage.parent])

    def test_signature_detection_without_archive_extension(self):
        path, result = self.process('download.bin', zip_bytes([('actual.log', b'hello')]))
        self.assertFalse(path.exists())
        self.assertEqual(result['artifacts'][0]['path'], 'actual.log')

    def test_nested_single_file_log_archive_has_no_generated_wrapper_directory(self):
        inner = zip_bytes([('AND_0000_20261028_154315_1640.log', b'original log')])
        path, result = self.process('logs.zip', zip_bytes([
            ('logd/AND_0000_20261028_154315_1640_8c542b81.log', inner), ('readme.txt', b'notes')]))
        self.assertEqual(result['gaps'], [])
        expected = self.folder / 'logs/logd/AND_0000_20261028_154315_1640.log'
        self.assertTrue(expected.is_file())
        self.assertEqual(expected.read_bytes(), b'original log')
        self.assertFalse((expected.parent / 'AND_0000_20261028_154315_1640_8c542b81.log').exists())
        self.assertFalse(path.exists())

    def test_nested_derived_name_conflict_does_not_overwrite_member(self):
        path, result = self.process('conflict.zip', zip_bytes([
            ('inner.zip', zip_bytes([('a', b'x')])), ('a', b'user')]))
        self.assertTrue(path.exists())
        self.assertTrue(result['gaps'])
        self.assertEqual(result['artifacts'], [])
        self.assertFalse((self.folder / 'conflict').exists())

    def test_all_expansion_bytes_count_toward_recursive_budget(self):
        nested = zip_bytes([('data', b'hello')])
        path, result = self.process('budget.zip', zip_bytes([('inner.zip', nested)]),
                                    limits={'max_expanded_size': len(nested) + 4})
        self.assertTrue(path.exists())
        self.assertTrue(result['gaps'])
        self.assertEqual(result['artifacts'], [])

    def test_zip_crc_corruption_fails_before_publication(self):
        data = bytearray(zip_bytes([('log.txt', b'known-payload')]))
        data[data.index(b'known-payload')] ^= 1
        path, result = self.process('bad-crc.zip', data)
        self.assertTrue(path.exists())
        self.assertTrue(result['gaps'])
        self.assertEqual(result['artifacts'], [])

    def test_archive_cleanup_failure_is_reported_without_false_deleted_status(self):
        from unittest.mock import patch
        path = self.stage / 'cleanup.zip'
        path.write_bytes(zip_bytes([('log.txt', b'hello')]))
        real_unlink = Path.unlink
        def fail_original(target, *args, **kwargs):
            if target == path:
                raise PermissionError('test cleanup failure')
            return real_unlink(target, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_original):
            result = process_file(self.ws, self.folder, path, path.name, 'a')
        self.assertTrue(path.exists())
        self.assertTrue(result['gaps'])
        self.assertFalse(result['archives'][0]['deleted'])
        self.assertEqual((self.folder / result['artifacts'][0]['path']).read_bytes(), b'hello')

    def test_truncated_tar_member_cannot_publish(self):
        data = tar_bytes('log.txt', b'hello')[:514]
        path, result = self.process('truncated.tar', data)
        self.assertTrue(path.exists())
        self.assertTrue(result['gaps'])
        self.assertEqual(result['artifacts'], [])

    def test_common_tar_dot_prefix_preserves_safe_member_layout(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            root = tarfile.TarInfo('.')
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            member = tarfile.TarInfo('./logs/actual.log')
            member.size = 5
            archive.addfile(member, io.BytesIO(b'hello'))
        path, result = self.process('common.tar', stream.getvalue())
        self.assertFalse(path.exists())
        self.assertEqual(result['gaps'], [])
        self.assertEqual((self.folder / 'common/logs/actual.log').read_bytes(), b'hello')

    def test_canonical_duplicate_member_paths_are_rejected(self):
        path, result = self.process('canonical.zip', zip_bytes([('logs/a', b'one'), ('./logs/a', b'two')]))
        self.assertTrue(path.exists())
        self.assertTrue(result['gaps'])
        self.assertEqual(result['artifacts'], [])
