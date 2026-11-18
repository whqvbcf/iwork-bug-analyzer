"""Run the prepared executable with real Chromium framing, without a browser."""

import json
import struct
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from iwork_collector.install import EXTENSION_ID, prepare_native


class NativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='iwork-native-integration-')
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.root = self.work / 'space'
        self.downloads = self.work / 'Downloads'
        self.downloads.mkdir()
        self.prepared = prepare_native(self.root, self.work / 'native', downloads_root=self.downloads)

    def execute(self, method, params=None, origin=None, prepared=None):
        request = {'id': uuid.uuid4().hex, 'method': method, 'params': params or {}}
        payload = json.dumps(request, ensure_ascii=False).encode()
        command = [(prepared or self.prepared)['wrapper']]
        if origin:
            command.append(origin)
        completed = subprocess.run(command, input=struct.pack('=I', len(payload)) + payload,
                                   capture_output=True, timeout=20)
        return request, completed

    def call(self, method, params=None, origin=None, prepared=None):
        request, completed = self.execute(method, params, origin, prepared)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertGreaterEqual(len(completed.stdout), 4)
        length = struct.unpack('=I', completed.stdout[:4])[0]
        self.assertEqual(len(completed.stdout), 4 + length)
        response = json.loads(completed.stdout[4:])
        self.assertEqual(response['id'], request['id'])
        self.assertTrue(response['ok'], response.get('error'))
        return response['result']

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
        self.fail('Native worker did not reach the expected durable state within 10 seconds')

    def test_installed_capture_download_resume_and_exact_name_handoff(self):
        manifest = json.loads(Path(self.prepared['manifest']).read_text())
        self.assertEqual(manifest['allowed_origins'], ['chrome-extension://' + EXTENSION_ID + '/'])
        self.assertIn('version', self.call('ping', origin=manifest['allowed_origins'][0]))
        snapshot = {
            'schema_version': 1, 'bug_id': '123456',
            'url': 'https://iwork.example/app#/bug/123456',
            'title': '2026 年启动问题', 'description': '问题正文', 'fields': {}, 'comments': [],
            'attachments': [{'id': 'a', 'name': 'token=diagnostic.log',
                             'url': 'https://iwork.example/a?token=runtime-secret', 'transport': 'browser'}],
            'images': [], 'groups': [], 'file_mentions': [],
            'coverage': {'complete': False, 'gaps': ['仅采集已显示内容']},
        }
        job = self.call('capture', {'snapshot': snapshot})
        tasks = self.wait_for(lambda: self.call('browser_poll')['tasks'])
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task['name'], 'token=diagnostic.log')
        self.assertTrue(task['relative_path'].endswith('/token=diagnostic.log'))
        self.assertIn('runtime-secret', task['url'])
        self.call('browser_result', {'task_id': task['task_id'], 'download_id': 41, 'status': 'started'})
        self.call('browser_result', {'task_id': task['task_id'], 'download_id': 41,
                                     'status': 'failed', 'error': 'network interrupted'})
        self.call('resume', {'job_id': job['job_id']})
        self.call('browser_result', {'task_id': task['task_id'], 'download_id': 42, 'status': 'started'})
        path = self.downloads / task['relative_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('exact source payload', encoding='utf-8')
        self.call('browser_result', {'task_id': task['task_id'], 'download_id': 42,
                                     'status': 'complete', 'path': str(path)})
        published = Path(job['folder']) / 'token=diagnostic.log'
        self.wait_for(published.is_file)
        self.assertEqual(published.read_text(), 'exact source payload')
        self.assertFalse(path.exists())
        source = Path(job['folder']) / '.iwork/source' / job['job_id'] / 'ticket.json'
        raw = source.read_text()
        self.assertNotIn('runtime-secret', raw)
        self.assertIn('token=diagnostic.log', raw)
        self.assertIn('#/bug/123456', raw)
        self.wait_for(lambda: self.call('status', {'job_id': job['job_id']})['status'] in ('ready', 'partial'))

    def test_custom_origin_is_forwarded_and_mismatched_origin_is_rejected(self):
        custom = 'a' * 32
        prepared = prepare_native(self.root, self.work / 'custom', custom, self.downloads)
        self.assertIn('version', self.call('ping', origin='chrome-extension://' + custom + '/', prepared=prepared))
        _, completed = self.execute('ping', origin='chrome-extension://' + EXTENSION_ID + '/', prepared=prepared)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b'')
        self.assertIn('未注册的扩展来源', completed.stderr.decode())


if __name__ == '__main__':
    unittest.main()
