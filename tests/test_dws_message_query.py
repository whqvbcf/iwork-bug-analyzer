import json
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from iwork_collector.dws import DwsClient


class MessageQueryDownloadTests(unittest.TestCase):
    def test_exact_file_resource_downloads_through_query_and_accepts_empty_file_message_id(self):
        calls = []
        def runner(args, cwd):
            calls.append(args)
            self.assertEqual(args[1:3], ['chat', '+messages-mget'])
            message = {'messageId': 'original', 'conversationId': 'cid', 'resourceRefs': [
                {'name': 'log.zip', 'resourceId': 'file-current', 'type': 'fileId'}]}
            result = {'messages': [message]}
            if '--download-resources' in args:
                self.assertEqual(args[args.index('--msg-ids') + 1], 'original')
                output = args[args.index('--output-dir') + 1]
                self.assertFalse(Path(output).is_absolute())
                path = cwd / output / 'server-name.zip'
                path.write_bytes(b'original file data')
                result['resourceDownloads'] = {'ok': True, 'downloads': [{
                    'resourceType': 'fileId', 'resourceId': 'file-current', 'messageId': '',
                    'localPath': str(path.relative_to(cwd)), 'sizeBytes': path.stat().st_size}],
                    'downloadedCount': 1, 'failedCount': 0, 'failures': []}
            return result
        with tempfile.TemporaryDirectory() as temp:
            path = DwsClient(runner=runner)._download(
                {'name': 'log.zip', 'file_id': 'stale', 'message_id': 'original', 'source_kind': 'chat-message'},
                {'open_conversation_id': 'cid', 'space_id': 'group-space'}, Path(temp))
            self.assertEqual(path.name, 'log.zip')
            self.assertEqual(path.read_bytes(), b'original file data')
        self.assertEqual(len(calls), 2)
        self.assertIn('--download-resources', calls[-1])

    def test_per_item_download_failure_is_preserved_without_retrying_drive_or_media(self):
        calls = []
        def runner(args, cwd):
            calls.append(args)
            self.assertEqual(args[1:3], ['chat', '+messages-mget'])
            body = {'messages': [{'messageId': 'm', 'conversationId': 'cid', 'resourceRefs': [
                {'name': 'log.zip', 'resourceId': 'file', 'type': 'fileId'}]}]}
            if '--download-resources' in args:
                body['resourceDownloads'] = {'ok': False, 'downloads': [], 'failedCount': 1,
                    'failures': [{'resourceType': 'fileId', 'resourceId': 'file', 'messageId': '',
                        'error': 'invalidRequest.resource.notFound', 'trace_id': 'trace-item'}]}
            return body
        with tempfile.TemporaryDirectory() as temp:
            client = DwsClient(runner=runner)
            with self.assertRaisesRegex(ValueError, 'trace-item'):
                client._download({'name': 'log.zip', 'file_id': 'file', 'message_id': 'm',
                    'source_kind': 'chat-message'}, {'open_conversation_id': 'cid'}, Path(temp))
        self.assertEqual(len(calls), 2)
        self.assertIn('resourceDownloads', client.last_ledger[-1]['response'])

    def test_explicit_profile_and_complete_cli_error_are_kept_without_credentials(self):
        body = {'error': {'message': 'server error', 'reason': 'business_error',
            'details': {'trace_id': 'nested-trace', 'retryable': False, 'token': 'do-not-save'},
            'server_error_code': 'invalidRequest.resource.notFound'}}
        with patch('iwork_collector.dws.subprocess.run', return_value=SimpleNamespace(
                returncode=4, stdout='', stderr=json.dumps(body))) as execute:
            client = DwsClient(executable='/fixed/dws', profile='fixed-org:fixed-user')
            with self.assertRaises(ValueError):
                client._call(['chat', '+messages-mget', '--msg-ids', 'm'], Path('/tmp'))
        args = execute.call_args.args[0]
        self.assertIn('--profile', args)
        self.assertEqual(args[args.index('--profile') + 1], 'fixed-org:fixed-user')
        saved = json.dumps(client.last_ledger)
        self.assertIn('nested-trace', saved)
        self.assertNotIn('do-not-save', saved)

    def test_verbose_json_lines_keep_error_trace_and_redact_embedded_credentials(self):
        text = '\n'.join([json.dumps({'level': 'DEBUG', 'access_token': 'verbose-secret'}),
            json.dumps({'error': {'message': 'failed', 'trace_id': 'verbose-trace'}})])
        with patch('iwork_collector.dws.subprocess.run', return_value=SimpleNamespace(
                returncode=4, stdout='', stderr=text)):
            client = DwsClient(executable='/fixed/dws', verbose=True)
            with self.assertRaises(ValueError):
                client._call(['chat', '+messages-mget', '--msg-ids', 'm'], Path('/tmp'))
        saved = json.dumps(client.last_ledger)
        self.assertIn('verbose-trace', saved)
        self.assertNotIn('verbose-secret', saved)

    def test_timeout_keeps_partial_output_error_trace_and_redacts_credentials(self):
        failure = subprocess.TimeoutExpired('dws', 630, output=b'before timeout',
            stderr=b'context before\n{"trace_id":"timeout-trace","access_token":"timeout-secret"}\ncontext after')
        with patch('iwork_collector.dws.subprocess.run', side_effect=failure):
            client = DwsClient(executable='/fixed/dws')
            with self.assertRaises(subprocess.TimeoutExpired):
                client._call(['chat', '+messages-mget', '--msg-ids', 'm'], Path('/tmp'))
        saved = json.dumps(client.last_ledger)
        for value in ('before timeout', 'timeout-trace', 'context before', 'context after'):
            self.assertIn(value, saved)
        self.assertNotIn('timeout-secret', saved)

    def test_probe_preflight_timeout_also_persists_partial_error(self):
        scripts = sorted((Path(__file__).resolve().parent.parent / 'scripts').glob('diagnose-*.py'))
        if not scripts:
            self.skipTest('一次性诊断脚本不随仓库分发')
        spec = importlib.util.spec_from_file_location('exact_probe', scripts[0])
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        failure = subprocess.TimeoutExpired('dws', 90, output=b'partial output',
            stderr=b'{"trace_id":"preflight-trace","token":"probe-secret"}')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / 'dws'; binary.write_bytes(b'fixed binary')
            with patch.object(probe, 'PROJECT', root), patch.object(probe, 'BINARY', binary), \
                    patch.object(probe, 'BINARY_SHA256', probe.hashlib.sha256(binary.read_bytes()).hexdigest()), \
                    patch.object(probe.subprocess, 'run', side_effect=failure), patch('builtins.print'):
                self.assertEqual(probe.main(), 1)
            saved = json.loads(next(root.rglob('diagnostic.json')).read_text())
        self.assertEqual(len(saved['calls']), 1)
        self.assertIn('preflight-trace', saved['trace_ids'])
        self.assertIn('partial output', json.dumps(saved))
        self.assertNotIn('probe-secret', json.dumps(saved))

    def test_query_file_ledger_rejects_changed_identity_other_resources_and_untrusted_outputs(self):
        problems = ('other-resource', 'missing-refs', 'changed-resource', 'changed-name', 'wrong-cid',
                    'fresh-missing-cid', 'fresh-other-resource', 'fresh-extra-message', 'fresh-incomplete',
                    'fresh-failures', 'fresh-partial',
                    'wrong-type', 'wrong-receipt-id', 'wrong-receipt-message', 'absolute', 'outside',
                    'symlink', 'empty', 'size', 'duplicate-downloads', 'missing-ledger')
        for problem in problems:
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                outside = root / 'outside.zip';outside.write_bytes(b'unchanged')
                calls = []
                def runner(args, cwd):
                    calls.append(args)
                    self.assertEqual(args[1:3], ['chat', '+messages-mget'])
                    downloading = '--download-resources' in args
                    ref = {'name': 'other.zip' if downloading and problem == 'changed-name' else 'log.zip',
                        'resourceId': 'changed' if downloading and problem == 'changed-resource' else 'file',
                        'type': 'fileId'}
                    refs = [] if problem == 'missing-refs' else [ref]
                    if problem == 'other-resource':
                        refs.append({'name': 'unrequested.zip', 'resourceId': 'other', 'type': 'fileId'})
                    body = {'messages': [{'messageId': 'm', 'conversationId': 'wrong' if downloading and problem == 'wrong-cid' else 'cid',
                        'resourceRefs': refs}]}
                    if downloading:
                        if problem == 'fresh-missing-cid': del body['messages'][0]['conversationId']
                        if problem == 'fresh-other-resource':
                            refs.append({'name': 'unrequested.zip', 'resourceId': 'other', 'type': 'fileId'})
                        if problem == 'fresh-extra-message': body['messages'].append({'messageId': 'unrequested'})
                        if problem == 'fresh-incomplete': body['complete'] = False
                        if problem == 'fresh-failures': body['failures'] = [{'error': 'partial extraction'}]
                        if problem == 'fresh-partial': body['partial'] = True
                    if downloading and problem != 'missing-ledger':
                        path = cwd / args[args.index('--output-dir') + 1] / 'server.zip'
                        if problem == 'symlink':path.symlink_to(outside)
                        else:path.write_bytes(b'' if problem == 'empty' else b'file data')
                        item = {'resourceType': 'mediaId' if problem == 'wrong-type' else 'fileId',
                            'resourceId': 'other' if problem == 'wrong-receipt-id' else 'file',
                            'messageId': 'other' if problem == 'wrong-receipt-message' else '',
                            'localPath': str(path) if problem == 'absolute' else str(outside.relative_to(cwd)) if problem == 'outside' else str(path.relative_to(cwd)),
                            'sizeBytes': 999 if problem == 'size' else path.stat().st_size}
                        body['resourceDownloads'] = {'ok': True, 'failures': [],
                            'downloads': [item, dict(item)] if problem == 'duplicate-downloads' else [item]}
                    return body
                with self.assertRaises(ValueError):
                    DwsClient(runner=runner)._download({'name': 'log.zip', 'file_id': 'file',
                        'message_id': 'm', 'source_kind': 'chat-message'}, {'open_conversation_id': 'cid'}, root)
                self.assertFalse((root / 'log.zip').exists())
                self.assertEqual(outside.read_bytes(), b'unchanged')
                if problem in ('other-resource', 'missing-refs'):
                    self.assertEqual(len(calls), 1)

    def test_pinned_original_resource_never_downloads_a_changed_same_name_resource(self):
        calls = []
        def runner(args, cwd):
            calls.append(args)
            self.assertNotIn('--download-resources', args)
            return {'messages': [{'messageId': 'm', 'conversationId': 'cid', 'resourceRefs': [
                {'name': 'log.zip', 'resourceId': 'changed', 'type': 'fileId'}]}]}
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, '固定'):
                DwsClient(runner=runner)._download({'name': 'log.zip', 'file_id': 'fixed',
                    'expected_resource_id': 'fixed', 'expected_resource_type': 'fileid',
                    'message_id': 'm', 'source_kind': 'chat-message'}, {'open_conversation_id': 'cid'}, Path(temp))
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
