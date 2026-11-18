"""Regressions discovered by the independent backend review."""
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from iwork_collector.service import Service


class ServiceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'work'
        self.downloads = Path(self.temp.name) / 'Downloads'
        self.downloads.mkdir()
        self.service = Service(self.root, downloads_root=self.downloads)
        self.addCleanup(self.service.close)

    def snapshot(self, **values):
        return {'bug_id': '123456', 'title': 'Review', 'description': 'facts',
                'coverage': {'complete': True, 'gaps': []}, **values}

    def local_capture(self, name, data=b'log', **snapshot):
        path = Path(self.temp.name) / name
        path.write_bytes(data)
        return self.service.capture(self.snapshot(attachments=[
            {'id': name, 'name': name, 'transport': 'local', 'local_path': str(path)}], **snapshot), trusted=True)

    def browser_complete(self, name, content):
        summary = self.service.capture(self.snapshot(attachments=[
            {'id': name, 'name': name, 'url': 'https://example.test/download/' + name}]))
        self.service.run(summary['job_id'])
        task = next(t for t in self.service.browser_poll()['tasks'] if t['job_id'] == summary['job_id'])
        path = self.downloads / task['relative_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.service.browser_result(task['task_id'], 'complete', download_id=1, path=str(path))
        return summary, task

    def test_browser_login_html_and_error_json_do_not_become_log_evidence(self):
        for name, content in [('html.txt', b'<html><form>Sign in</form></html>'),
                              ('json.txt', b'{"error":"login required","status":401}')]:
            summary, task = self.browser_complete(name, content)
            result = self.service.run(summary['job_id'])
            self.assertEqual(result['status'], 'partial')
            self.assertFalse((Path(result['folder']) / name).exists())
            self.assertTrue(result['gaps'])
            # Duplicate terminal completion must still be harmless after ingestion failure.
            self.service.browser_result(task['task_id'], 'complete', download_id=1, path='/does/not/exist')

    def test_resume_keeps_missing_ocr_and_observation_gaps(self):
        summary = self.local_capture('screen.png', b'fake-image')
        recognition = {'text': '', 'observations': '', 'clues': {'urls': [], 'files': []},
                       'gaps': ['OCR unavailable', 'Observation unavailable'], 'engine': 'fixture'}
        with patch('iwork_collector.service.inspect_image', return_value=recognition):
            first = self.service.run(summary['job_id'])
        self.assertEqual(first['status'], 'partial')
        self.service.resume(summary['job_id'])
        second = self.service.run(summary['job_id'])
        self.assertEqual(second['status'], 'partial')
        self.assertIn('OCR unavailable', second['gaps'])

    def test_resume_keeps_archive_cleanup_gap(self):
        summary = self.local_capture('cleanup.log')
        self.service.run(summary['job_id'])
        job = self.service.state.get(summary['job_id'])
        job['archives'].append({'name': 'old.zip', 'sha256': 'a' * 64, 'deleted': False, 'members': []})
        job['gaps'].append('Archive cleanup pending')
        self.service._save(job)
        self.service.resume(summary['job_id'])
        result = self.service.run(summary['job_id'])
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any('清理' in gap or 'cleanup' in gap for gap in result['gaps']))

    def test_two_queued_snapshots_merge_shared_artifacts(self):
        first = self.local_capture('a.log', description='first')
        second = self.local_capture('b.log', description='second')
        self.service.run(first['job_id'])
        self.service.run(second['job_id'])
        manifest = json.loads((Path(second['folder']) / '.iwork/manifest.json').read_text())
        self.assertEqual({a['path'] for a in manifest['artifacts']}, {'a.log', 'b.log'})

    def test_missing_or_modified_done_evidence_cannot_report_ready(self):
        for name, change in [('missing.log', None), ('modified.log', b'user edit')]:
            summary = self.local_capture(name, description=name)
            self.service.run(summary['job_id'])
            output = Path(summary['folder']) / name
            if change is None:
                output.unlink()
            else:
                output.write_bytes(change)
            self.service.resume(summary['job_id'])
            result = self.service.run(summary['job_id'])
            self.assertEqual(result['status'], 'partial')
            self.assertTrue(result['gaps'])
            if change is not None:
                self.assertEqual(output.read_bytes(), change)

    def test_successful_browser_archive_is_checkpointed_before_later_crash(self):
        archive_path = Path(self.temp.name) / 'input.zip'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('log.txt', b'archive evidence')
        summary, task = self.browser_complete('input.zip', archive_path.read_bytes())
        original_save = self.service._save
        def interrupt_after_ingest(job):
            if any(c['state'] == 'done' for c in job['candidates']):
                raise KeyboardInterrupt('simulated process loss before checkpoint')
            return original_save(job)
        with patch.object(self.service, '_save', side_effect=interrupt_after_ingest):
            with self.assertRaises(KeyboardInterrupt):
                self.service.run(summary['job_id'])
        # Restart/resume has enough staged material to replay successful extraction.
        result = self.service.run(summary['job_id'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual((Path(result['folder']) / 'log.txt').read_bytes(), b'archive evidence')

    def test_cancel_reports_busy_during_worker_and_cancels_afterwards(self):
        summary = self.local_capture('slow.log')
        entered, release = threading.Event(), threading.Event()
        failures = []
        def worker():
            service = Service(self.root, downloads_root=self.downloads)
            original = service._ingest
            def slow(*args):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('review test barrier timeout')
                return original(*args)
            try:
                with patch.object(service, '_ingest', side_effect=slow):
                    service.run(summary['job_id'])
            except BaseException as error:
                failures.append(error)
            finally:
                service.close()
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(5))
        try:
            # Cancel must not block the extension queue behind a long worker.
            with self.assertRaises(ValueError):
                self.service.cancel(summary['job_id'])
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.service.cancel(summary['job_id'])
        self.assertEqual(self.service.state.get(summary['job_id'])['status'], 'cancelled')

    def test_retry_browser_task_accepts_new_download_id(self):
        summary = self.service.capture(self.snapshot(attachments=[
            {'id': 'retry', 'name': 'retry.log', 'url': 'https://example.test/retry'}]))
        self.service.run(summary['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        self.service.browser_result(task['task_id'], 'started', download_id=1)
        self.service.browser_result(task['task_id'], 'failed', download_id=1, error='interrupted')
        self.service.run(summary['job_id'])
        self.service.resume(summary['job_id'])
        self.service.browser_result(task['task_id'], 'started', download_id=2)
        path = self.downloads / task['relative_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'retried log')
        self.service.browser_result(task['task_id'], 'complete', download_id=2, path=str(path))
        result = self.service.run(summary['job_id'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual((Path(result['folder']) / 'retry.log').read_bytes(), b'retried log')

    def test_ready_job_inherits_unresolved_processing_gaps_from_shared_manifest(self):
        clean = self.local_capture('clean.log')
        self.service.run(clean['job_id'])
        image = self.local_capture('screen.png', b'fake image', description='new image')
        recognition = {'text': '', 'observations': '', 'clues': {'urls': [], 'files': []},
                       'gaps': ['Image observation unavailable'], 'engine': 'fixture'}
        with patch('iwork_collector.service.inspect_image', return_value=recognition):
            self.service.run(image['job_id'])
        # Re-capturing the older snapshot must not erase the current folder's gap.
        old_snapshot = self.service.state.get(clean['job_id'])['snapshot']
        result = self.service.capture(old_snapshot, trusted=True)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('Image observation unavailable', result['gaps'])

    def test_changed_evidence_with_separator_in_original_name_is_removed_from_manifest(self):
        summary = self.local_capture('name；part.log')
        self.service.run(summary['job_id'])
        (Path(summary['folder']) / 'name；part.log').write_bytes(b'user modified')
        self.service.resume(summary['job_id'])
        self.service.run(summary['job_id'])
        manifest = json.loads((Path(summary['folder']) / '.iwork/manifest.json').read_text())
        self.assertNotIn('name；part.log', {a['path'] for a in manifest['artifacts']})

    def test_idempotent_capture_refreshes_private_signed_urls_without_new_job(self):
        first_snapshot = self.snapshot(attachments=[{'id': 'signed', 'name': 'signed.log',
            'url': 'https://example.test/file?token=expired&version=1'}])
        first = self.service.capture(first_snapshot)
        self.service.run(first['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        fresh_snapshot = self.snapshot(attachments=[{'id': 'signed', 'name': 'signed.log',
            'url': 'https://example.test/file?token=fresh&version=1'}])
        fresh = self.service.capture(fresh_snapshot)
        self.assertEqual(first['job_id'], fresh['job_id'])
        candidate = self.service.state.get(first['job_id'])['candidates'][0]
        self.assertIn('token=fresh', candidate['url'])
        self.assertIn('token=fresh', self.service.state.task_get(task['task_id'])['url'])
        persisted = Path(first['folder']) / '.iwork/source' / first['job_id'] / 'ticket.json'
        self.assertNotIn('fresh', persisted.read_text())
        self.assertNotIn('expired', persisted.read_text())

    def test_signed_url_refresh_also_handles_candidates_without_source_ids(self):
        old = self.service.capture(self.snapshot(attachments=[{'name': 'signed.log',
            'url': 'https://example.test/file?token=old'}]))
        candidate_id = self.service.state.get(old['job_id'])['candidates'][0]['id']
        for secret in ('new', 'newest'):
            new = self.service.capture(self.snapshot(attachments=[{'name': 'signed.log',
                'url': 'https://example.test/file?token=' + secret}]))
            self.assertEqual(new['job_id'], old['job_id'])
            candidate = self.service.state.get(old['job_id'])['candidates'][0]
            self.assertEqual(candidate['id'], candidate_id)
            self.assertIn('token=' + secret, candidate['url'])

    def test_worker_cleans_browser_copy_after_interrupted_handoff_cleanup(self):
        summary = self.service.capture(self.snapshot(attachments=[
            {'id': 'handoff', 'name': 'handoff.log', 'url': 'https://example.test/handoff'}]))
        self.service.run(summary['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        browser_file = self.downloads / task['relative_path']
        browser_file.parent.mkdir(parents=True)
        browser_file.write_bytes(b'durable browser log')
        real_unlink = Path.unlink
        def fail_browser(target, *args, **kwargs):
            if target == browser_file:
                raise PermissionError('interrupted browser cleanup')
            return real_unlink(target, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_browser):
            with self.assertRaises(PermissionError):
                self.service.browser_result(task['task_id'], 'complete', download_id=1, path=str(browser_file))
        result = self.service.run(summary['job_id'])
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(browser_file.exists())
        self.assertFalse((self.downloads / 'iwork-collector').exists())
