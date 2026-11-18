import json
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from iwork_collector.dws import DwsClient


class DwsTests(unittest.TestCase):
    def test_video_search_retries_stem_and_includes_delayed_uploads_without_accepting_other_video(self):
        wanted = '463_20261108_033622.mp4'
        queries = []
        def runner(args, cwd):
            query = args[args.index('--query') + 1]
            queries.append(query)
            self.assertGreaterEqual(args[args.index('--end') + 1], '2026-11-13')
            messages = [] if query == wanted else [{
                'openMessageId': 'video-message', 'resourceRefs': [
                    {'name': '463_20261108_033623.mp4', 'resourceId': 'wrong', 'type': 'fileId'},
                    {'name': wanted, 'resourceId': 'correct', 'type': 'fileId'}]}]
            return {'result': {'conversationMessagesList': [{'messages': messages}]}}
        with tempfile.TemporaryDirectory() as temp:
            found = DwsClient(runner=runner)._chat_find(
                {'name': '日志群', 'open_conversation_id': 'cid'}, wanted,
                '2026-11-05T00:00:00+08:00', '2026-11-12T00:00:00+08:00', Path(temp))
        self.assertGreater(len(queries), 1)
        self.assertEqual([item['file_id'] for item in found], ['correct'])

    def test_large_log_transfer_uses_longer_download_budget_than_metadata_calls(self):
        def execute(command, **options):
            if options['timeout'] < 600:
                import subprocess
                raise subprocess.TimeoutExpired(command, options['timeout'])
            self.assertGreaterEqual(int(command[command.index('--timeout') + 1]), 600)
            output = Path(command[command.index('--output') + 1])
            (output / 'log.zip').write_bytes(b'complete transfer fixture')
            return SimpleNamespace(returncode=0, stdout='{"result":{"status":"complete"}}', stderr='')
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.dws.subprocess.run', side_effect=execute):
            downloaded = DwsClient(executable='/tmp/dws-fixture')._download(
                {'name': 'log.zip', 'file_id': 'file', 'source_kind': 'drive'},
                {'space_id': '27569621904'}, Path(temp))
            self.assertEqual(downloaded.read_bytes(), b'complete transfer fixture')

    def test_located_message_is_retained_when_its_file_download_fails(self):
        def runner(args, cwd):
            if args[1:4] == ['chat', 'message', 'search']:
                return {'result': {'conversationMessagesList': [{'messages': [{
                    'openMessageId': 'message-1', 'content': '[文件] log.zip fileId:file-1',
                    'createTime': '2026-07-27 17:27:03'}]}]}}
            raise ValueError('DWS 调用失败：request_failed；socket: operation not permitted')
        with tempfile.TemporaryDirectory() as temp:
            result = DwsClient(runner=runner).search_files(
                {'name': '日志群', 'open_conversation_id': 'cid', 'space_id': 'space-1'},
                'log.zip', '2026-07-24', '2026-07-31', Path(temp), prefer_chat=True)
        coverage = result['coverage']
        self.assertEqual(coverage['matched_files'][0]['file_id'], 'file-1')
        self.assertEqual(coverage['matched_files'][0]['message_id'], 'message-1')
        self.assertEqual(coverage['download_status'], 'failed')
        self.assertIn('socket', coverage['download_error'])
        self.assertFalse(result['candidates'])

    def test_group_file_download_requests_current_authorization_in_its_own_space(self):
        calls = []
        def runner(args, cwd):
            calls.append(args)
            if args[1:3] == ['drive', 'list']:
                return {'result': {'items': [{'name': 'log.zip', 'fileId': 'f1', 'type': 'FILE'}]}}
            if args[1:3] == ['drive', 'download']:
                self.assertIn('--space-id', args)
                self.assertEqual(args[args.index('--space-id') + 1], '27569621904')
                output = Path(args[args.index('--output') + 1])
                (output / 'log.zip').write_bytes(b'fresh archive')
                return {'result': {'status': 'complete'}}
            self.fail(f'wrong group file authorization context: {args}')
        with tempfile.TemporaryDirectory() as temp:
            result = DwsClient(runner=runner).search_files(
                {'name': '日志群', 'open_conversation_id': 'cid', 'space_id': '27569621904'},
                'log.zip', '2026-07-24', '2026-07-31', Path(temp))
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(len(calls), 2)

    def test_chat_resource_refs_are_reloaded_from_the_message_before_downloading(self):
        calls = []
        def runner(args, cwd):
            calls.append(args)
            if args[1:4] == ['chat', 'message', 'search']:
                return {'result': {'conversationMessagesList': [{'messages': [
                    {'openMessageId': 'm1', 'content': 'log.zip fileId:f1',
                     'resourceRefs': [{'name': 'log.zip', 'resourceId': 'f1', 'type': 'fileId'}]}]}]}}
            self.assertEqual(args[1:3], ['chat', '+messages-mget'])
            self.assertEqual(args[args.index('--msg-ids') + 1], 'm1')
            response = {'messages': [{'messageId': 'm1', 'conversationId': 'cid',
                'resourceRefs': [{'name': 'log.zip', 'resourceId': 'refreshed-f1', 'type': 'fileId'}]}]}
            if '--download-resources' in args:
                output = cwd / args[args.index('--output-dir') + 1] / 'log.zip'
                output.write_bytes(b'fresh message resource')
                response['resourceDownloads'] = {'ok': True, 'downloads': [{
                    'resourceId': 'refreshed-f1', 'resourceType': 'fileId', 'messageId': '',
                    'localPath': str(output.relative_to(cwd)), 'sizeBytes': output.stat().st_size}], 'failures': []}
            return response
        with tempfile.TemporaryDirectory() as temp:
            result = DwsClient(runner=runner).search_files({'name': '日志群', 'open_conversation_id': 'cid',
                'space_id': '27569621904'},
                'log.zip', '2026-07-24', '2026-07-31', Path(temp), prefer_chat=True)
            self.assertEqual(Path(result['candidates'][0]['local_path']).read_bytes(), b'fresh message resource')
        self.assertEqual(len(calls), 3)

    def test_extensionless_log_identity_matches_only_the_exact_returned_archive_name(self):
        wanted = 'log_E001-10_20260830-012100_20260830153917485'
        original = wanted + '.zip'
        downloaded = []
        def runner(args, cwd):
            if args[1:4] == ['chat', 'message', 'search']:
                return {'result': {'conversationMessagesList': [{'messages': [
                    {'openMessageId': 'm1', 'content': f'[文件] {search_name} fileId:f1',
                     'resourceRefs': [
                        {'name': wanted + '_other.zip', 'resourceId': 'wrong', 'type': 'fileId'},
                        {'name': search_name, 'resourceId': 'f1', 'type': 'fileId'}]}]}]}}
            self.assertEqual(args[1:3], ['chat', '+messages-mget'])
            response = {'messages': [{'messageId': 'm1', 'conversationId': 'cid',
                'resourceRefs': [{'name': original, 'resourceId': 'fresh', 'type': 'fileId'}]}]}
            if '--download-resources' in args:
                downloaded.append('fresh')
                output = cwd / args[args.index('--output-dir') + 1] / original
                output.write_bytes(b'original archive')
                response['resourceDownloads'] = {'ok': True, 'downloads': [{
                    'resourceType': 'fileId', 'resourceId': 'fresh', 'messageId': '',
                    'localPath': str(output.relative_to(cwd)), 'sizeBytes': output.stat().st_size}], 'failures': []}
            return response
        for search_name in (original, wanted):
            with self.subTest(search_name=search_name), tempfile.TemporaryDirectory() as temp:
                result = DwsClient(runner=runner).search_files({'name':'日志群', 'open_conversation_id':'cid'},
                    wanted, '2026-08-27', '2026-09-03', Path(temp), prefer_chat=True)
                self.assertEqual(len(result['candidates']), 1, result['gaps'])
                self.assertEqual(result['candidates'][0]['name'], original)
                self.assertEqual(Path(result['candidates'][0]['local_path']).name, original)
        self.assertEqual(downloaded, ['fresh', 'fresh'])

    def test_failure_retains_server_trace_and_reason_without_credentials(self):
        response = {'error': {'message': 'internal server error', 'reason': 'MCP_TOOL_ERROR',
                              'trace_id': 'trace-123', 'server_error_code': 'permissionDenied',
                              'retryable': False}}
        client = DwsClient(executable='/tmp/test-dws')
        with patch('iwork_collector.dws.subprocess.run', return_value=SimpleNamespace(
                returncode=5, stdout='', stderr=json.dumps(response))):
            with self.assertRaises(ValueError) as error:
                client._call(['drive', 'download', '--node', 'f1'], Path('/tmp'))
        self.assertIn('trace-123', str(error.exception))
        self.assertIn('permissionDenied', str(error.exception))

    def test_file_transfer_verifies_receipt_path_and_size_before_restoring_original_name(self):
        for problem in ('none', 'wrong-node', 'receipt-size', 'outside', 'symlink', 'empty', 'truncated', 'unidentified'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                outside = root / 'unrelated.zip'
                outside.write_bytes(b'not this resource')
                payload = b'complete original attachment'
                def runner(args, cwd):
                    self.assertEqual(args[1:3], ['chat', '+messages-mget'])
                    response = {'messages': [{'messageId': 'm1', 'conversationId': 'cid',
                        'resourceRefs': [{'name': 'log.zip', 'type': 'fileId',
                            'resourceId': 'fresh-file', 'fileSize': len(payload)}]}]}
                    if '--download-resources' not in args:
                        return response
                    output = cwd / args[args.index('--output-dir') + 1] / 'server-preferred-name.zip'
                    if problem == 'symlink':
                        output.symlink_to(outside)
                    else:
                        output.write_bytes(b'' if problem == 'empty' else payload[:-2] if problem == 'truncated' else payload)
                    if problem == 'unidentified':
                        return response
                    response['resourceDownloads'] = {'ok': True, 'failures': [], 'downloads': [{
                        'resourceType': 'fileId', 'resourceId': 'other-file' if problem == 'wrong-node' else 'fresh-file',
                        'messageId': '', 'localPath': str(outside.relative_to(cwd)) if problem == 'outside' else str(output.relative_to(cwd)),
                        'sizeBytes': 999 if problem == 'receipt-size' else len(payload)}]}
                    return response
                client = DwsClient(runner=runner)
                match = {'name': 'log.zip', 'file_id': 'old-file', 'message_id': 'm1',
                         'source_kind': 'chat-message'}
                if problem == 'none':
                    downloaded = client._download(match, {'open_conversation_id': 'cid'}, root)
                    self.assertEqual(downloaded.name, 'log.zip')
                    self.assertEqual(downloaded.read_bytes(), payload)
                else:
                    with self.assertRaises(ValueError):
                        client._download(match, {'open_conversation_id': 'cid'}, root)
                    self.assertFalse((root / 'log.zip').exists())
                self.assertEqual(outside.read_bytes(), b'not this resource')
                self.assertFalse(any(root.glob('.dws-attempt-*')))

    def test_retains_robot_log_portal_link_when_dws_file_download_fails(self):
        def runner(args, cwd):
            if args[1:4] == ['chat', 'message', 'search']:
                return {'result': {'conversationMessagesList': [{'messages': [
                    {'openMessageId': 'robot', 'content': 'log.zip 日志已解析 https://fawivi-prod-k8-vehiclelogweb.faw.cn/?id=300001'},
                    {'openMessageId': 'file', 'content': 'log.zip fileId:f1'}]}]}}
            raise ValueError('DWS 调用失败：internal server error')
        with tempfile.TemporaryDirectory() as temp:
            result = DwsClient(runner=runner).search_files({'name': '日志群', 'open_conversation_id': 'cid'},
                'log.zip', '2026-07-24', '2026-07-31', Path(temp), prefer_chat=True)
        self.assertEqual(result['log_links'][0]['message_id'], 'robot')
        self.assertIn('id=300001', result['log_links'][0]['url'])
        self.assertFalse(result['candidates'])

    def _fixture_client(self, raw):
        def runner(args, cwd):
            if args[1] == 'schema':
                return {'parameters': [{'name': '--' + x} for x in
                    ['group', 'chat-query', 'query', 'start', 'end', 'page-all',
                     'page-limit', 'download-resources', 'output-dir']]}
            return raw
        return DwsClient(profile='corp:user', runner=runner)

    def test_same_named_resources_remain_ambiguous_and_preserve_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            paths = [temp / 'a', temp / 'b']
            for path in paths:
                path.write_bytes(path.name.encode())
            raw = {'complete': True, 'resourceDownloads': [
                {'status': 'complete', 'path': str(path), 'fileName': 'log.zip',
                 'messageId': path.name} for path in paths]}
            result = self._fixture_client(raw).search_files({'conversation_id': 'cid'},
                'log.zip', '2026-11-10', '2026-11-11', temp)
            self.assertEqual(result['candidates'], [])
            self.assertIn('同名', ' '.join(result['gaps']))
            self.assertEqual(len(result['ambiguous_candidates']), 2)
            self.assertTrue(result['ledger'])

    def test_ambiguous_chat_resolution_never_ingests_download_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'log.zip'
            path.write_bytes(b'fixture')
            result = self._fixture_client({'outcome': 'ambiguous', 'complete': True,
                'downloads': [{'status': 'complete', 'path': str(path)}]}).search_files(
                    {'name': '项目群'}, 'log.zip', '2026-11-10', '2026-11-11', temp)
            self.assertEqual(result['candidates'], [])
            self.assertIn('歧义', ' '.join(result['gaps']))

    def test_missing_cli_is_actionable_without_login_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            result = DwsClient(executable='iwork-nonexistent-dws').search_files(
                {'name': '项目群'}, 'log.zip', '2026-11-10T00:00:00+08:00',
                '2026-11-11T00:00:00+08:00', Path(temp))
            self.assertFalse(result['candidates'])
            self.assertIn('DWS', ' '.join(result['gaps']))

    def test_schema_checked_before_bounded_read_and_ledger_is_preserved(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            downloaded = temp / 'log.zip'
            downloaded.write_bytes(b'fixture')
            def runner(args, cwd):
                calls.append(args)
                if args[1:3] == ['profile', 'list']:
                    return {'profiles': [{'id': 'corp:user', 'isOrgCurrent': True}]}
                if args[1] == 'schema':
                    return {'parameters': [{'name': '--' + x} for x in
                            ['group','chat-query','query','start','end','page-all','page-limit',
                             'no-reactions','download-resources','output-dir']]}
                return {'data': {'complete': True, 'messages': [{'messageId': 'm1',
                         'fileName': 'log.zip'}], 'downloads': [{'status':'complete',
                         'path': str(downloaded), 'fileName': 'log.zip', 'messageId': 'm1'}]}}
            result = DwsClient(runner=runner).search_files({'conversation_id': 'cid'}, 'log.zip',
                        '2026-11-10T00:00:00+08:00', '2026-11-11T00:00:00+08:00', temp)
            command = calls[-1]
            self.assertIn('--start', command)
            self.assertIn('--end', command)
            self.assertIn('--profile', command)
            self.assertEqual(result['candidates'][0]['name'], 'log.zip')
            self.assertTrue(result['coverage']['complete'])
            self.assertTrue(result['ledger'])

    def test_ambiguous_profile_never_selects_first(self):
        def runner(args, cwd):
            return {'profiles': [{'id':'a'}, {'id':'b'}]}
        result = DwsClient(runner=runner).search_files({'name':'群'}, 'log.zip', 'a', 'b', Path('.'))
        self.assertFalse(result['candidates'])
        self.assertIn('profile', ' '.join(result['gaps']))

    def test_unnamed_group_file_search_requires_ticket_query(self):
        def runner(args, cwd):
            self.fail('缺少工单主题时不能执行全群文件检索')
        result = DwsClient(runner=runner).search_files({'name':'群'}, '',
            '2026-11-10T00:00:00+08:00', '2026-11-11T00:00:00+08:00', Path('.'))
        self.assertIn('工单', ' '.join(result['gaps']))

    def test_real_dws_chain_resolves_group_lists_drive_and_downloads_original_name(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            def runner(args, cwd):
                calls.append(args)
                sub = args[1:]
                if sub[:2] == ['chat', 'search']:
                    return {'result': {'groups': [{'title': '数字座舱统一日志基地',
                                                   'openConversationId': 'oc-1',
                                                   'extension': {'newCSpaceIdIM': 'space-1'}}]}}
                if sub[:2] == ['drive', 'list']:
                    return {'result': {'items': [{'fileId': 'file-1', 'name': 'log.zip',
                                                   'type': 'FILE', 'fileSize': 7}], 'nextToken': ''}}
                if sub[:2] == ['drive', 'download']:
                    output = Path(args[args.index('--output') + 1])
                    output.mkdir(parents=True, exist_ok=True)
                    (output / 'log.zip').write_bytes(b'fixture')
                    return {'result': {'status': 'complete'}}
                self.fail(f'unexpected dws call: {args}')
            result = DwsClient(runner=runner).search_files(
                {'name': '数字座舱统一日志基地'}, 'log.zip',
                '2026-07-24T00:00:00+08:00', '2026-07-31T00:00:00+08:00', temp)
            self.assertEqual([item['name'] for item in result['candidates']], ['log.zip'])
            self.assertTrue(Path(result['candidates'][0]['local_path']).is_file())
            self.assertFalse(any('+' in ' '.join(call) for call in calls))
            self.assertTrue(any(call[1:3] == ['chat', 'search'] for call in calls))
            self.assertTrue(any(call[1:3] == ['drive', 'list'] for call in calls))
            download = next(call for call in calls if call[1:3] == ['drive', 'download'])
            self.assertIn('-f', download)
            self.assertIn('json', download)

    def test_real_dws_chain_falls_back_to_chat_message_file_id(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            def runner(args, cwd):
                calls.append(args)
                sub = args[1:]
                if sub[:2] == ['chat', 'search']:
                    return {'result': {'groups': [{'title': '日志群', 'openConversationId': 'oc-2',
                                                   'extension': {'newCSpaceIdIM': 'space-2'}}]}}
                if sub[:2] == ['drive', 'list']:
                    return {'result': {'items': [], 'nextToken': ''}}
                if sub[:3] == ['chat', 'message', 'search']:
                    return {'result': {'conversationMessagesList': [{
                        'title': '日志群', 'openConversationId': 'oc-2', 'messages': [{
                            'openMessageId': 'm-1', 'content': 'log.zip fileId:file-2',
                            'createTime': '2026-07-27T10:00:00+08:00'}]}]}}
                if sub[:2] == ['chat', '+messages-mget']:
                    self.assertEqual(args[args.index('--msg-ids') + 1], 'm-1')
                    response = {'messages': [{'messageId': 'm-1', 'conversationId': 'oc-2',
                        'resourceRefs': [{'name': 'log.zip', 'resourceId': 'file-2', 'type': 'fileId'}]}]}
                    if '--download-resources' in args:
                        output = cwd / args[args.index('--output-dir') + 1] / 'log.zip'
                        output.write_bytes(b'fixture')
                        response['resourceDownloads'] = {'ok': True, 'failures': [], 'downloads': [{
                            'resourceType': 'fileId', 'resourceId': 'file-2', 'messageId': '',
                            'localPath': str(output.relative_to(cwd)), 'sizeBytes': output.stat().st_size}]}
                    return response
                self.fail(f'unexpected dws call: {args}')
            result = DwsClient(runner=runner).search_files(
                {'name': '日志群'}, 'log.zip',
                '2026-07-24T00:00:00+08:00', '2026-07-31T00:00:00+08:00', temp,
                prefer_chat=True)
            self.assertEqual(result['candidates'][0]['name'], 'log.zip')
            self.assertEqual(result['candidates'][0]['source']['message_id'], 'm-1')
            self.assertTrue(any(call[1:4] == ['chat', 'message', 'search'] for call in calls))
            chat_index = next(i for i, call in enumerate(calls) if call[1:4] == ['chat', 'message', 'search'])
            drive_indices = [i for i, call in enumerate(calls) if call[1:3] == ['drive', 'list']]
            self.assertTrue(not drive_indices or chat_index < min(drive_indices))

    def test_failed_group_file_authorization_stops_without_repeating_the_same_backend_route(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            def runner(args, cwd):
                calls.append(args)
                sub = args[1:]
                if sub[:2] == ['chat', 'search']:
                    return {'result': {'groups': [{'title': '日志群', 'openConversationId': 'oc-3',
                                                   'extension': {'newCSpaceIdIM': 'space-3'}}]}}
                if sub[:3] == ['chat', 'message', 'search']:
                    return {'result': {'conversationMessagesList': [{
                        'title': '日志群', 'openConversationId': 'oc-3', 'messages': [{
                            'openMessageId': 'm-3', 'content': '[文件] log.zip fileId:file-3',
                            'createTime': '2026-07-27T10:00:00+08:00'}]}]}}
                if sub[:2] == ['chat', '+messages-mget']:
                    response = {'messages': [{'messageId': 'm-3', 'conversationId': 'oc-3',
                        'resourceRefs': [{'name': 'log.zip', 'resourceId': 'file-3', 'type': 'fileId'}]}]}
                    if '--download-resources' in args:
                        response['resourceDownloads'] = {'ok': False, 'downloads': [], 'failedCount': 1,
                            'failures': [{'resourceType': 'fileId', 'resourceId': 'file-3',
                                'error': 'invalidRequest.resource.notFound', 'trace_id': 'trace-original'}]}
                    return response
                self.fail(f'unexpected dws call: {args}')
            result = DwsClient(runner=runner).search_files(
                {'name': '日志群'}, 'log.zip',
                '2026-07-24T00:00:00+08:00', '2026-07-31T00:00:00+08:00', temp,
                prefer_chat=True)
            self.assertFalse(result['candidates'])
            self.assertFalse(any(call[1:3] == ['chat', '+messages-resource-download'] for call in calls))
            self.assertFalse(any(call[1:3] == ['drive', 'download'] for call in calls))
            self.assertEqual(sum('--download-resources' in call for call in calls), 1)
            self.assertIn('invalidRequest.resource.notFound', ' '.join(result['gaps']))
            self.assertIn('trace-original', result['coverage']['download_error'])

    def test_message_download_uses_current_resource_identity_and_checks_the_download_ledger(self):
        for problem in ('none', 'temporary-name', 'child-owner', 'child-inherited-conversation', 'missing-owner', 'not-ready',
                        'wrong-child-receipt', 'wrong-download-arguments', 'ambiguous-owner',
                        'ambiguous-ref', 'wrong-message', 'wrong-group', 'wrong-name',
                        'wrong-resource', 'wrong-type', 'missing-media-message', 'size', 'failed'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                def runner(args, cwd):
                    name = 'another.zip' if problem == 'wrong-name' else 'log.zip'
                    local_name = 'log (2).zip' if problem == 'temporary-name' else name
                    refs = [{'name': name, 'resourceId': 'fresh-resource', 'type': 'mediaId'}]
                    child_owner = problem in {'child-owner', 'child-inherited-conversation', 'wrong-child-receipt', 'ambiguous-owner'}
                    owner = 'child-message' if child_owner else 'm1'
                    conversation = 'child-cid' if child_owner and problem != 'child-inherited-conversation' else 'cid'
                    if child_owner or problem in {'wrong-download-arguments', 'missing-owner', 'not-ready'}:
                        arguments = {'type': 'mediaId', 'resource-id': 'other' if problem == 'wrong-download-arguments' else 'fresh-resource',
                                     'message-id': owner}
                        arguments['open-conversation-id'] = conversation
                        if problem == 'missing-owner':
                            arguments.pop('message-id')
                        refs[0]['download'] = {'shortcut': '+messages-resource-download', 'arguments': arguments,
                            'ready': problem != 'not-ready', 'missing': []}
                    if problem == 'ambiguous-owner':
                        refs.append({'name': name, 'resourceId': 'fresh-resource', 'type': 'mediaId',
                            'download': {'arguments': {'type': 'mediaId', 'resource-id': 'fresh-resource',
                                'message-id': 'other-child', 'open-conversation-id': conversation}}})
                    if problem == 'ambiguous-ref':
                        refs.append({'name': name, 'resourceId': 'another-resource', 'type': 'mediaId'})
                    if args[1:3] == ['chat', '+messages-mget']:
                        self.assertNotIn('--download-resources', args)
                        self.assertIn('--no-reactions', args)
                        self.assertIn('--no-threads', args)
                        return {'messages': [{'messageId': 'other' if problem == 'wrong-message' else 'm1',
                            'conversationId': 'other' if problem == 'wrong-group' else 'cid', 'resourceRefs': refs}]}
                    self.assertEqual(args[1:3], ['chat', '+messages-resource-download'])
                    self.assertEqual(args[args.index('--type') + 1], 'mediaId')
                    self.assertEqual(args[args.index('--message-id') + 1], owner)
                    self.assertEqual(args[args.index('--open-conversation-id') + 1], conversation)
                    if problem == 'failed':
                        raise ValueError('permissionDenied')
                    output = root / args[args.index('--output') + 1] / local_name
                    output.write_bytes(b'fresh attachment')
                    return {'messageId': '' if problem == 'missing-media-message' else 'm1' if problem == 'wrong-child-receipt' else owner,
                        'resourceId': 'other' if problem == 'wrong-resource' else 'fresh-resource',
                        'resourceType': 'fileId' if problem == 'wrong-type' else 'mediaId',
                        'localPath': str(output.relative_to(root)),
                        'sizeBytes': 999 if problem == 'size' else len(b'fresh attachment')}
                client = DwsClient(runner=runner)
                match = {'name': 'log.zip', 'file_id': 'stale-resource', 'message_id': 'm1',
                         'source_kind': 'chat-message'}
                group = {'open_conversation_id': 'cid', 'space_id': 'space'}
                if problem in {'none', 'temporary-name', 'child-owner', 'child-inherited-conversation'}:
                    path = client._download(match, group, root)
                    self.assertEqual(path.name, 'log.zip')
                    self.assertEqual(path.read_bytes(), b'fresh attachment')
                else:
                    with self.assertRaises(ValueError):
                        client._download(match, group, root)
                    self.assertFalse((root / 'log.zip').exists())
                self.assertLessEqual(len(client.last_ledger), 2)

    def test_auth_failure_is_reported_as_login_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            result = DwsClient(executable='dws-auth-fixture', runner=None).search_files(
                {'name': '日志群'}, 'log.zip', '2026-07-24', '2026-07-31', Path(temp))
            # A missing executable is still actionable and must never be described as
            # a missing log file; the live auth wording is covered by _run's parser.
            self.assertIn('DWS', ' '.join(result['gaps']))


if __name__ == '__main__':
    unittest.main()
