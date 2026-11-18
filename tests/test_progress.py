import tempfile
import unittest
from pathlib import Path

from iwork_collector.archives import process_file
from iwork_collector.dws import DwsClient
from iwork_collector.service import Service
from test_archives import zip_bytes


class ProgressTests(unittest.TestCase):
    def test_download_callback_precedes_blocking_call_and_returns_to_search_stage(self):
        events = []
        client = DwsClient(runner=lambda args, cwd: events.append('execute') or {'ok': True})
        client.progress = lambda **data: events.append(data['phase'])
        client._call(['chat', '+messages-mget', '--download-resources'], Path('/tmp'))
        self.assertEqual(events[:2], ['downloading', 'execute'])
        client._call(['chat', '+messages-mget'], Path('/tmp'))
        self.assertEqual(events[-2:], ['searching', 'execute'])

    def test_archive_callback_reports_actual_expanded_bytes_and_members(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(Path(temp) / 'workspace')
            self.addCleanup(service.close)
            folder = service.workspace.bug_dir('1')
            source = service.workspace.staging_dir('job', 'item') / 'logs.zip'
            source.write_bytes(zip_bytes([('a.log', b'hello'), ('b.log', b'world')]))
            events = []
            result = process_file(service.workspace, folder, source, source.name, 'source', progress=events.append)
            self.assertEqual(result['gaps'], [])
            self.assertEqual(events[-1]['expanded_bytes'], 10)
            self.assertEqual(events[-1]['files_done'], 2)

    def test_native_summary_reads_live_progress_without_rewriting_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'workspace'
            service = Service(root)
            self.addCleanup(service.close)
            captured = service.capture({'bug_id': '1', 'title': 'test', 'description':
                'logs.zip 见：【日志基地】钉钉群', 'coverage': {'complete': True, 'gaps': []}})
            job = service.state.get(captured['job_id'])
            job['status'] = 'processing'
            service.state.put(job)
            report = Path(job['folder']) / 'bugreport.md'
            before = report.stat().st_mtime_ns
            service._progress(job, phase='downloading', name='logs.zip')
            observer = Service(root)
            self.addCleanup(observer.close)
            summary = observer.summary(observer.state.get(job['job_id']))
            self.assertEqual(summary['progress']['phase'], 'downloading')
            self.assertEqual(summary['progress']['name'], 'logs.zip')
            self.assertEqual(summary['progress']['total'], 1)  # P1/P2 are one material
            self.assertEqual(summary['progress']['completed'], 0)
            self.assertEqual(report.stat().st_mtime_ns, before)
            job['candidates'][0]['state'] = 'done'
            job['status'] = 'ready'
            summary = observer.summary(job)
            self.assertEqual(summary['progress']['phase'], 'complete')
            self.assertEqual(summary['progress']['completed'], 1)


if __name__ == '__main__':
    unittest.main()
