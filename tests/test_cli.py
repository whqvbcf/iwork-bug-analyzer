import json
import subprocess
import sys
import tempfile
import unittest
import io
from pathlib import Path


class CliTests(unittest.TestCase):
    def command(self, *args):
        result = subprocess.run([sys.executable, '-m', 'iwork_collector', *args],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_demo_creates_portable_original_name_evidence_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.command('--root', temp, 'demo')
            folder = Path(result['folder'])
            self.assertRegex(folder.name, r'^bug_\d{6}_123456$')
            self.assertTrue((folder/'复现说明.txt').exists())
            self.assertTrue((folder/'设备日志/system.log').exists())
            self.assertTrue((folder/'bugreport.md').exists())
            self.assertFalse(list(folder.rglob('*.zip')))

    def test_doctor_reports_missing_tools_instead_of_requesting_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.command('--root', temp, 'doctor')
            self.assertIn('dws', result['capabilities'])
            self.assertIn('workspace_root', result)

    def test_prepare_native_registration_writes_only_explicit_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'prepared'
            result = self.command('--root', str(Path(temp)/'runtime'), 'install-native', '--output', str(output))
            manifest = json.loads(Path(result['manifest']).read_text())
            self.assertEqual(manifest['name'], 'com.iwork.collector')
            self.assertIn('chrome-extension://oblbdhfcbmklhaebkonhfdidglfhinac/', manifest['allowed_origins'])
            self.assertTrue(Path(manifest['path']).is_file())
            self.assertIn('"$@"', Path(manifest['path']).read_text())

    def test_real_native_process_framed_ping(self):
        from iwork_collector.native import read_message, write_message
        with tempfile.TemporaryDirectory() as temp:
            request = io.BytesIO()
            write_message(request, {'id':'ping-1', 'method':'ping', 'params':{}})
            result = subprocess.run([sys.executable, '-m', 'iwork_collector', '--root', temp,
                'native', 'chrome-extension://oblbdhfcbmklhaebkonhfdidglfhinac/'],
                input=request.getvalue(), capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            response = read_message(io.BytesIO(result.stdout))
            self.assertTrue(response['ok'])
            self.assertEqual(response['id'], 'ping-1')
            self.assertEqual(response['result']['workspace_root'], temp)


if __name__ == '__main__':
    unittest.main()
