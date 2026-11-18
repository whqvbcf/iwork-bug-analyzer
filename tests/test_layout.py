import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iwork_collector.service import Service
from iwork_collector.storage import sha256_file


class ExistingLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(Path(self.temp.name) / 'workspace')
        self.addCleanup(self.service.close)
        result = self.service.capture({'bug_id': '10000002', 'coverage': {'complete': True, 'gaps': []}})
        self.job = self.service.state.get(result['job_id'])
        self.folder = Path(result['folder'])
        self.old = self.folder / 'logs/logd/AND_hash.log/AND.log'
        self.old.parent.mkdir(parents=True)
        self.old.write_bytes(b'original evidence')
        self.job.update(status='ready', artifacts=[{'path': self.old.relative_to(self.folder).as_posix(),
            'name': 'AND.log', 'sha256': sha256_file(self.old), 'size': self.old.stat().st_size}],
            archives=[{'name': 'logs.zip', 'sha256': 'b' * 64, 'deleted': True,
                       'members': ['logd/AND_hash.log']},
                      {'name': 'AND_hash.log', 'sha256': 'a' * 64, 'deleted': True,
                       'parent': 'logs.zip', 'members': ['AND.log']}])
        self.service._save(self.job)

    def test_open_report_compacts_only_verified_generated_wrapper_and_updates_both_records(self):
        inode = self.old.stat().st_ino
        with patch('iwork_collector.service.subprocess.Popen'), \
                patch.object(self.service.dws, 'search_files', side_effect=AssertionError('no download')):
            self.service.dispatch('open', {'job_id': self.job['job_id'], 'kind': 'report'})
        target = self.old.parent.parent / self.old.name
        self.assertTrue(target.is_file())
        self.assertEqual(target.stat().st_ino, inode)
        self.assertFalse(self.old.parent.exists())
        job = self.service.state.get(self.job['job_id'])
        self.assertEqual(job['artifacts'][0]['path'], 'logs/logd/AND.log')
        self.service._merge_current(job)
        self.assertEqual(len(job['artifacts']), 1)
        self.assertNotIn('证据导航', (self.folder / 'bugreport.md').read_text())
        self.service.refresh_report(job['job_id'])
        self.assertEqual(target.read_bytes(), b'original evidence')

    def test_conflicting_or_extra_user_files_prevent_old_folder_cleanup(self):
        target = self.old.parent.parent / self.old.name
        target.write_bytes(b'user content')
        self.service.refresh_report(self.job['job_id'])
        self.assertEqual(target.read_bytes(), b'user content')
        self.assertTrue(self.old.exists())
        target.unlink()
        (self.old.parent / 'user.txt').write_bytes(b'user content')
        self.service.refresh_report(self.job['job_id'])
        self.assertTrue(self.old.exists())
        self.assertFalse(target.exists())

    def test_cleanup_interruption_keeps_persisted_new_path_and_can_resume(self):
        real_unlink = Path.unlink
        def interrupted(path, *args, **kwargs):
            if path == self.old:
                raise PermissionError('simulated interruption')
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', interrupted):
            self.service.refresh_report(self.job['job_id'])
        target = self.old.parent.parent / self.old.name
        self.assertTrue(target.is_file())
        self.assertTrue(self.old.is_file())
        self.assertEqual(self.service.state.get(self.job['job_id'])['artifacts'][0]['path'], 'logs/logd/AND.log')
        self.service.refresh_report(self.job['job_id'])
        self.assertFalse(self.old.parent.exists())

    def test_matching_directory_name_elsewhere_is_not_an_archive_wrapper(self):
        real = self.folder / 'outer/x/log.txt'
        real.parent.mkdir(parents=True)
        real.write_bytes(b'real directory')
        self.job['artifacts'] = [{'path': 'outer/x/log.txt', 'name': 'log.txt',
            'sha256': sha256_file(real), 'size': real.stat().st_size}]
        self.job['archives'] = [
            {'name': 'outer.zip', 'deleted': True, 'members': ['x/log.txt', 'other/x.zip'], 'sha256': 'c' * 64},
            {'name': 'x.zip', 'parent': 'outer.zip', 'deleted': True, 'members': ['log.txt'], 'sha256': 'd' * 64}]
        self.service._save(self.job)
        self.service.refresh_report(self.job['job_id'])
        self.assertTrue(real.is_file())
        self.assertFalse((self.folder / 'outer/log.txt').exists())

    def test_reused_file_keeps_both_sources_and_updates_image_and_ocr_paths(self):
        target = self.old.parent.parent / self.old.name
        target.write_bytes(self.old.read_bytes())
        old_path = self.old.relative_to(self.folder).as_posix()
        new_path = target.relative_to(self.folder).as_posix()
        self.job['artifacts'][0].update(source_key='attachment', source={'kind': 'attachment'})
        self.job['artifacts'].append(dict(self.job['artifacts'][0], path=new_path,
            source_key='images-img1', source={'id': 'img1'}))
        self.job['images'] = [{'path': old_path, 'sha256': 'e' * 64, 'name': 'AND.log'}]
        self.job['snapshot']['comments'] = [{'id': 'c1', 'author': '作者', 'text': '见图', 'image_ids': ['img1']}]
        self.job['snapshot']['images'] = [{'id': 'img1', 'name': 'AND.log', 'comment_ids': ['c1']}]
        self.job['candidates'] = [{'id': 'ocr', 'source': {'image': old_path}, 'state': 'done'}]
        self.service._save(self.job)
        self.service.refresh_report(self.job['job_id'])
        job = self.service.state.get(self.job['job_id'])
        keys = {a.get('source_key') for a in job['artifacts']}
        keys.update(k for a in job['artifacts'] for k in a.get('additional_source_keys', []))
        self.assertIn('images-img1', keys)
        self.assertIn('attachment', keys)
        self.assertEqual(job['images'][0]['path'], new_path)
        self.assertEqual(job['candidates'][0]['source']['image'], new_path)
        self.assertNotIn('评论图片未取得', (self.folder / 'bugreport.md').read_text())

    def test_chained_moves_are_resolved_before_merging_old_job(self):
        self.job['path_moves'] = {self.job['artifacts'][0]['path']: 'middle.log', 'middle.log': 'final.log'}
        self.service._merge_current(self.job)
        self.assertEqual({a['path'] for a in self.job['artifacts']}, {'final.log'})

    def test_legacy_wrapper_overlapping_real_parent_member_is_left_intact(self):
        real = self.folder / 'outer/x/log.txt'
        real.parent.mkdir(parents=True)
        real.write_bytes(b'same content')
        (self.folder / 'outer/log.txt').write_bytes(b'same content')
        self.job['artifacts'] = [{'path': 'outer/x/log.txt', 'name': 'log.txt',
            'sha256': sha256_file(real), 'size': real.stat().st_size}]
        self.job['archives'] = [
            {'name': 'outer.zip', 'deleted': True, 'members': ['x/log.txt', 'x.zip'], 'sha256': 'c' * 64},
            {'name': 'x.zip', 'parent': 'outer.zip', 'deleted': True, 'members': ['log.txt'], 'sha256': 'd' * 64}]
        self.service._save(self.job)
        self.service.refresh_report(self.job['job_id'])
        self.assertTrue(real.is_file())

    def test_state_commit_failure_after_manifest_preserves_cleanup_for_next_open(self):
        with patch.object(self.service.state, 'put', side_effect=OSError('state unavailable')):
            with self.assertRaises(OSError):
                self.service.refresh_report(self.job['job_id'])
        self.assertTrue(self.old.is_file())
        self.service.refresh_report(self.job['job_id'])
        self.assertFalse(self.old.parent.exists())


if __name__ == '__main__':
    unittest.main()
