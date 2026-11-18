import io
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

from iwork_collector.archives import process_file
from iwork_collector.storage import Workspace


SEVEN_ZIP = shutil.which('7z')
UNRAR = shutil.which('unrar')


def rar_bytes(members, flags=0):
    """A stored RAR 4 fixture, with genuine header and payload CRCs."""
    def header(kind, options, body):
        body = struct.pack('<BHH', kind, options, len(body) + 7) + body
        return struct.pack('<H', zlib.crc32(body) & 0xffff) + body
    result = b'Rar!\x1a\x07\x00' + header(0x73, flags, b'\0' * 6)
    for name, data, mode in members:
        name = name.encode('ascii')
        body = struct.pack('<IIBIIBBHI', len(data), len(data), 3,
                           zlib.crc32(data), 0, 20, 0x30, len(name), mode)
        result += header(0x74, 0x8000, body + name) + data
    return result + header(0x7b, 0, b'')


class ExternalArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = Workspace(self.root / 'workspace')
        self.folder = self.ws.bug_dir('123')
        self.stage = self.ws.staging_dir('job', 'item')

    def process(self, name, data, **kwargs):
        path = self.stage / name
        path.write_bytes(data)
        result = process_file(self.ws, self.folder, path, name, 'attachment-1', **kwargs)
        return path, result

    def seven_zip(self, members, *options):
        if not SEVEN_ZIP:
            self.skipTest('7z is unavailable')
        source = self.root / 'source'
        source.mkdir(exist_ok=True)
        for name, data in members:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        archive = self.root / 'fixture.7z'
        subprocess.run([SEVEN_ZIP, 'a', *options, str(archive), '.'], cwd=source,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return archive.read_bytes()

    def assert_retained(self, path, result, message=None):
        self.assertTrue(path.exists())
        self.assertEqual(result['artifacts'], [])
        self.assertTrue(result['gaps'])
        self.assertTrue(all(not item['deleted'] for item in result['archives']))
        if message:
            self.assertIn(message.lower(), ' '.join(result['gaps']).lower())

    def test_real_7z_preserves_names_and_recursively_expands_zip(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as archive:
            archive.writestr('设备.txt', b'inner')
        data = self.seven_zip([('logs/设备 #1.log', b'hello'),
                               ('logs/nested.zip', nested.getvalue()),
                               ('-switch.txt', b'literal'), ('literal*.txt', b'wildcard'),
                               ('literalOTHER.txt', b'unrelated')])
        path, result = self.process('原始 logs.7z', data)
        self.assertEqual(result['gaps'], [])
        self.assertFalse(path.exists())
        self.assertEqual((self.folder / '原始 logs/logs/设备 #1.log').read_bytes(), b'hello')
        self.assertEqual((self.folder / '原始 logs/logs/设备.txt').read_bytes(), b'inner')
        self.assertEqual((self.folder / '原始 logs/-switch.txt').read_bytes(), b'literal')
        self.assertEqual((self.folder / '原始 logs/literal*.txt').read_bytes(), b'wildcard')
        self.assertEqual(len(result['archives']), 2)
        self.assertTrue(all(item['deleted'] for item in result['archives']))

    @unittest.skipUnless(SEVEN_ZIP and UNRAR, '7z and unrar are required')
    def test_real_rar_stored_member_is_extracted_and_archive_deleted(self):
        path, result = self.process('original.rar', rar_bytes([
            ('logs/a #1.txt', b'hello', 0o100644), ('-switch', b'safe', 0o100644),
            (' spaces ', b'spaces', 0o100644)]))
        self.assertEqual(result['gaps'], [])
        self.assertFalse(path.exists())
        self.assertEqual((self.folder / 'original/logs/a #1.txt').read_bytes(), b'hello')
        self.assertEqual((self.folder / 'original/-switch').read_bytes(), b'safe')
        self.assertEqual((self.folder / 'original/ spaces ').read_bytes(), b'spaces')
        self.assertTrue(result['archives'][0]['deleted'])

    @unittest.skipUnless(SEVEN_ZIP and UNRAR, '7z and unrar are required')
    def test_rar_link_traversal_duplicate_and_multivolume_are_retained(self):
        cases = [([('link', b'/tmp', 0o120777)], 0, 'link'),
                 ([('../outside', b'bad', 0o100644)], 0, 'path'),
                 ([('same', b'a', 0o100644), ('same', b'b', 0o100644)], 0, 'duplicate'),
                 ([('file', b'hello', 0o100644)], 1, 'volume')]
        for members, flags, message in cases:
            with self.subTest(message=message):
                path, result = self.process('unsafe.rar', rar_bytes(members, flags),
                                            limits={'max_seconds': 3})
                self.assert_retained(path, result, message)
        self.assertFalse((self.ws.root / 'outside').exists())

    def test_7z_password_and_header_password_are_retained_without_prompt(self):
        for options in [('-psecret',), ('-psecret', '-mhe=on')]:
            with self.subTest(options=options):
                archive = self.root / 'fixture.7z'
                archive.unlink(missing_ok=True)
                data = self.seven_zip([('secret.log', b'secret')], *options)
                path, result = self.process('protected.7z', data, limits={'max_seconds': 3})
                self.assert_retained(path, result)

    def test_7z_count_size_and_nesting_budgets(self):
        data = self.seven_zip([('a', b'1234'), ('b', b'other')])
        for limits, message in [({'max_expanded_size': 3}, 'size'),
                                ({'max_files': 1}, 'count')]:
            with self.subTest(limits=limits):
                path, result = self.process('limit.7z', data, limits=limits)
                self.assert_retained(path, result, message)
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, 'w') as archive:
            archive.writestr('inner.7z', data)
        path, result = self.process('outer.zip', outer.getvalue(), limits={'max_nesting': 1})
        self.assert_retained(path, result, 'depth')
        path, result = self.process('outer.zip', outer.getvalue(),
                                    limits={'max_expanded_size': len(data) + 3})
        self.assert_retained(path, result, 'size')

    @unittest.skipUnless(SEVEN_ZIP and UNRAR, '7z and unrar are required')
    def test_real_rar_crc_failure_retains_archive_and_publishes_nothing(self):
        data = bytearray(rar_bytes([('log.txt', b'known-payload', 0o100644)]))
        data[data.index(b'known-payload')] ^= 1
        path, result = self.process('damaged.rar', data)
        self.assert_retained(path, result, 'exit status')

    def test_missing_tool_retains_original_with_actionable_gap(self):
        with patch('iwork_collector.archives.shutil.which', return_value=None):
            path, result = self.process('logs.7z', b'7z\xbc\xaf\x27\x1c')
        self.assert_retained(path, result, 'unavailable')

    def fake_tool(self, listing, payload=b'data', status=0, delay=0):
        tool = self.root / 'fake-7z'
        marker = self.root / 'member-was-extracted'
        marker.unlink(missing_ok=True)
        tool.write_text('#!/usr/bin/python3\nimport sys, time\n'
                        f'listing = {listing!r}\n'
                        'if sys.argv[1] == "l":\n'
                        '    sys.stdout.write(listing)\n'
                        'else:\n'
                        f'    open({str(marker)!r}, "w").close()\n'
                        f'    time.sleep({delay!r})\n'
                        f'    sys.stdout.buffer.write({payload!r})\n'
                        f'    sys.stdout.flush()\n    sys.exit({status})\n')
        tool.chmod(0o700)
        return patch('iwork_collector.archives.shutil.which', return_value=str(tool))

    @staticmethod
    def listing(*members, header='Type = 7z'):
        return '--\nPath = archive\n' + header + '\n\n----------\n' + '\n\n'.join(members) + '\n'

    @staticmethod
    def member(name='safe', size=4, extra=''):
        return f'Path = {name}\nSize = {size}\nAttributes = A_ -rw-r--r--\nEncrypted = -\n' + extra

    def test_preflight_rejects_unsafe_listing_before_any_member_extraction(self):
        cases = [(self.listing(self.member('../outside')), 'path'),
                 (self.listing(self.member('/outside')), 'path'),
                 (self.listing(self.member('C:/outside')), 'path'),
                 (self.listing(self.member('a\\outside')), 'path'),
                 (self.listing(self.member('@listfile')), 'path'),
                 (self.listing(self.member('same'), self.member('./same')), 'duplicate'),
                 (self.listing(self.member(extra='Symbolic Link = /tmp')), 'link'),
                 (self.listing(self.member().replace('-rw-r--r--', 'lrwxrwxrwx')), 'link'),
                 (self.listing(self.member().replace('Encrypted = -', 'Encrypted = +')), 'password'),
                 (self.listing(self.member(), header='Type = 7z\nVolumes = 2'), 'volume'),
                 (self.listing(self.member(extra='Size = 4')), 'listing'),
                 (self.listing(self.member(size=-1)), 'size'),
                 (self.listing(self.member('safe'), self.member('../outside')), 'path'),
                 (self.listing(self.member('a'), self.member('a/child')), 'conflict')]
        for listing, message in cases:
            with self.subTest(message=message, listing=listing):
                with self.fake_tool(listing, payload=b'data'):
                    path, result = self.process('unsafe.7z', b'7z\xbc\xaf\x27\x1c')
                self.assert_retained(path, result, message)
                self.assertFalse((self.root / 'member-was-extracted').exists())

    def test_nonzero_exit_short_stream_and_overlong_stream_cannot_delete_original(self):
        for payload, status in [(b'data', 2), (b'dat', 0), (b'dataX', 0)]:
            with self.subTest(payload=payload, status=status):
                with self.fake_tool(self.listing(self.member()), payload, status):
                    path, result = self.process('failure.7z', b'7z\xbc\xaf\x27\x1c')
                self.assert_retained(path, result)

    def test_hung_member_stream_is_killed_within_budget(self):
        with self.fake_tool(self.listing(self.member()), delay=10):
            path, result = self.process('timeout.7z', b'7z\xbc\xaf\x27\x1c',
                                        limits={'max_seconds': 0.2})
        self.assert_retained(path, result, 'time')

    def test_listing_output_is_bounded(self):
        with self.fake_tool('x' * (17 * 1024 * 1024)):
            path, result = self.process('oversized-list.7z', b'7z\xbc\xaf\x27\x1c')
        self.assert_retained(path, result, 'listing')
