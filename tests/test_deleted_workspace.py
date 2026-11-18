"""Recollecting a deleted Bug folder must not reuse stale completion state."""
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from iwork_collector.service import Service


class DeletedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'
        self.service = Service(self.root)
        self.addCleanup(self.service.close)
        source = Path(self.temp.name) / 'original.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('android/system.log', 'original evidence')
        self.snapshot = {'bug_id': '10000002', 'title': 'Deleted folder', 'description': 'facts',
            'coverage': {'complete': True, 'gaps': []}, 'attachments': [
                {'id': 'log', 'name': source.name, 'transport': 'local', 'local_path': str(source)}]}
        self.first = self.service.capture(self.snapshot, trusted=True)
        self.service.run(self.first['job_id'])
        self.folder = Path(self.first['folder'])

    def remove_folder(self):
        old = self.root / 'bug_261113_10000002'
        if old != self.folder:
            self.folder.rename(old)
            job = self.service.state.get(self.first['job_id'])
            job['folder'] = str(old)
            self.service.state.put(job)
        self.folder = old
        shutil.rmtree(self.folder)

    def assert_recollected(self, result):
        self.assertNotEqual(result['job_id'], self.first['job_id'])
        result = self.service.run(result['job_id'])
        folder = Path(result['folder'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual((folder / 'original/android/system.log').read_text(), 'original evidence')
        identity = json.loads((folder / '.iwork/workspace.json').read_text())
        self.assertEqual(identity, {'bug_id': '10000002', 'source': 'iwork'})
        self.assertTrue((folder / '.iwork/source' / result['job_id'] / 'ticket.json').is_file())
        self.assertEqual(len(self.service.state.get(result['job_id'])['artifacts']), 1)
        if folder != self.folder:
            self.assertFalse(self.folder.exists(), 'old dated folder must not be recreated')

    def test_identical_recapture_after_deleting_folder_creates_fresh_job(self):
        self.remove_folder()
        result = self.service.capture(self.snapshot, trusted=True, retry_failed=True)
        self.assert_recollected(result)

    def test_automatic_capture_also_replaces_deleted_workspace(self):
        self.remove_folder()
        self.assert_recollected(self.service.capture(self.snapshot, trusted=True))

    def test_resume_after_deleting_folder_returns_replacement_job(self):
        self.remove_folder()
        self.assert_recollected(self.service.resume(self.first['job_id']))

    def test_cli_resume_runs_returned_replacement_job(self):
        self.remove_folder()
        result = subprocess.run([sys.executable, '-m', 'iwork_collector', '--root', str(self.root),
            'resume', self.first['job_id']], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['status'], 'ready')
        self.assertNotEqual(summary['job_id'], self.first['job_id'])
        self.assertEqual((Path(summary['folder']) / 'original/android/system.log').read_text(),
                         'original evidence')

    def test_worker_after_deleting_folder_collects_into_new_workspace(self):
        self.remove_folder()
        self.assert_recollected(self.service.run(self.first['job_id']))

    def test_known_worker_recreated_folder_recovers_identity_and_snapshot(self):
        (self.folder / '.iwork/workspace.json').unlink()
        shutil.rmtree(self.folder / '.iwork/source')
        result = self.service.capture(self.snapshot, trusted=True)
        self.assertEqual(result['job_id'], self.first['job_id'])
        self.assertTrue((self.folder / '.iwork/workspace.json').is_file())
        ticket = self.folder / '.iwork/source' / result['job_id'] / 'ticket.json'
        self.assertEqual(json.loads(ticket.read_text())['bug_id'], '10000002')
        self.assertEqual((self.folder / 'original/android/system.log').read_text(), 'original evidence')

    def test_unrelated_folder_without_matching_manifest_is_not_adopted(self):
        (self.folder / '.iwork/workspace.json').unlink()
        manifest = self.folder / '.iwork/manifest.json'
        data = json.loads(manifest.read_text())
        data['bug_id'] = '99999999'
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'identity|身份'):
            self.service.capture(self.snapshot, trusted=True)
        self.assertFalse((self.folder / '.iwork/workspace.json').exists())

    def test_malformed_manifest_cannot_restore_missing_identity(self):
        (self.folder / '.iwork/workspace.json').unlink()
        (self.folder / '.iwork/manifest.json').write_text('[]')
        with self.assertRaisesRegex(ValueError, 'identity|身份'):
            self.service.capture(self.snapshot, trusted=True)

    def test_changed_snapshot_can_reuse_known_recreated_folder(self):
        (self.folder / '.iwork/workspace.json').unlink()
        shutil.rmtree(self.folder / '.iwork/source')
        output = self.folder / 'original/android/system.log'
        inode = output.stat().st_ino
        result = self.service.capture({**self.snapshot, 'description': 'updated facts'}, trusted=True)
        self.assertEqual(result['folder'], str(self.folder))
        self.assertEqual(output.stat().st_ino, inode)
        self.assertEqual(len(self.service.state.get(result['job_id'])['artifacts']), 1)

    def test_old_pending_browser_task_is_stopped_after_folder_deletion(self):
        snapshot = {**self.snapshot, 'attachments': [
            {'id': 'screen', 'name': 'screen.png', 'url': 'https://example.test/screen.png'}]}
        first = self.service.capture(snapshot)
        self.service.run(first['job_id'])
        task = next(t for t in self.service.browser_poll()['tasks'] if t['job_id'] == first['job_id'])
        shutil.rmtree(first['folder'])
        replacement = self.service.capture(snapshot, retry_failed=True)
        self.assertNotEqual(replacement['job_id'], first['job_id'])
        self.assertEqual(self.service.state.task_get(task['task_id'])['status'], 'failed')
        self.service.browser_result(task['task_id'], 'complete', download_id=5, path='/does/not/exist')
        self.assertEqual(self.service.state.get(first['job_id'])['status'], 'cancelled')
        self.assertEqual(self.service.state.get(replacement['job_id'])['status'], 'captured')

    def test_changed_snapshot_retires_all_deleted_workspace_browser_tasks(self):
        snapshot = {**self.snapshot, 'attachments': [
            {'id': 'screen', 'name': 'screen.png', 'url': 'https://example.test/screen.png'}]}
        first = self.service.capture(snapshot)
        self.service.run(first['job_id'])
        task = next(t for t in self.service.browser_poll()['tasks'] if t['job_id'] == first['job_id'])
        shutil.rmtree(first['folder'])
        replacement = self.service.capture({**snapshot, 'description': 'new facts'}, retry_failed=True)
        self.assertNotEqual(replacement['job_id'], first['job_id'])
        self.assertEqual(self.service.state.task_get(task['task_id'])['status'], 'failed')
        self.assertEqual(self.service.state.get(first['job_id'])['status'], 'cancelled')
        self.assertEqual(self.service.run(first['job_id'])['status'], 'cancelled')
        self.assertNotIn(task['task_id'], {t['task_id'] for t in self.service.browser_poll()['tasks']})

    def test_retrying_retired_job_uses_replacement_without_reviving_old_tasks(self):
        snapshot = {**self.snapshot, 'attachments': [
            {'id': 'screen', 'name': 'screen.png', 'url': 'https://example.test/screen.png'}]}
        first = self.service.capture(snapshot)
        self.service.run(first['job_id'])
        old_task = next(t for t in self.service.browser_poll()['tasks'] if t['job_id'] == first['job_id'])
        shutil.rmtree(first['folder'])
        replacement = self.service.capture(snapshot, retry_failed=True)
        result = self.service.resume(first['job_id'])
        self.assertEqual(result['job_id'], replacement['job_id'])
        self.assertEqual(self.service.state.task_get(old_task['task_id'])['status'], 'failed')
        self.assertEqual(self.service.state.get(first['job_id'])['status'], 'cancelled')

    def test_cancel_retired_job_does_not_overwrite_new_report(self):
        shutil.rmtree(self.folder)
        replacement = self.service.capture(self.snapshot, trusted=True)
        report = Path(replacement['folder']) / 'bugreport.md'
        before = report.read_bytes()
        self.service.cancel(self.first['job_id'])
        self.assertEqual(report.read_bytes(), before)

    def test_retired_job_cannot_rewrite_new_report_when_opened(self):
        shutil.rmtree(self.folder)
        replacement = self.service.capture(self.snapshot, trusted=True)
        report = Path(replacement['folder']) / 'bugreport.md'
        before = report.read_bytes()
        with self.assertRaisesRegex(ValueError, '重新采集|新任务|当前任务'):
            self.service.refresh_report(self.first['job_id'])
        self.assertEqual(report.read_bytes(), before)

    def test_report_reloads_retirement_state_after_acquiring_bug_lock(self):
        real_lock = self.service._bug_lock
        replacement = {}
        @contextlib.contextmanager
        def replace_before_lock(*args, **kwargs):
            shutil.rmtree(self.folder)
            other = Service(self.root)
            try:
                replacement.update(other.capture({**self.snapshot, 'description': 'new facts'}, trusted=True))
            finally:
                other.close()
            with real_lock(*args, **kwargs) as acquired:
                yield acquired
        with patch.object(self.service, '_bug_lock', side_effect=replace_before_lock):
            with self.assertRaisesRegex(ValueError, '新任务'):
                self.service.refresh_report(self.first['job_id'])
        self.assertTrue(self.service.state.get(self.first['job_id'])['workspace_deleted'])
        report = (Path(replacement['folder']) / 'bugreport.md').read_text()
        self.assertIn('new facts', report)
        self.assertEqual(self.service.state.get(replacement['job_id'])['status'], 'captured')

    def test_open_report_for_deleted_folder_requests_recapture(self):
        self.remove_folder()
        with self.assertRaisesRegex(ValueError, '目录已删除'):
            self.service.refresh_report(self.first['job_id'])
        self.assertFalse(self.folder.exists())

    def test_cancel_after_deleting_folder_does_not_recreate_it(self):
        self.remove_folder()
        result = self.service.cancel(self.first['job_id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(self.folder.exists())


if __name__ == '__main__':
    unittest.main()
