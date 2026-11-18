import io
import json
import struct
import tempfile
import unittest
import zipfile
import subprocess
import sys
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

from iwork_collector.service import Service
from iwork_collector.dws import DwsClient
from iwork_collector.native import read_message, write_message
from iwork_collector.discovery import key
from iwork_collector.storage import sha256_file


class ServiceTests(unittest.TestCase):
    def test_resumed_partial_platform_materials_keep_acquisition_gaps(self):
        self.service.config['archive_limits'] = {'max_expanded_size': 3}
        def search(group, name, start, end, directory, **options):
            with zipfile.ZipFile(directory / name, 'w') as archive:
                archive.writestr('system.log', 'parsed subset')
            return {'candidates': [{'name': name, 'local_path': str(directory / name),
                        'source': {'mode': 'parsed-member'}}], 'coverage': {'complete': False},
                    'ledger': [], 'gaps': ['未取得原包，仅取得部分解析材料']}
        self.service.dws.search_files = search
        captured = self.service.capture(self.snapshot(description='log.zip 见：【日志基地】钉钉群'))
        self.service.run(captured['job_id'])
        self.service.config['archive_limits'] = {'max_expanded_size': 100}
        self.service.resume(captured['job_id'])
        with patch.object(self.service.dws, 'search_files', side_effect=AssertionError('no network needed')):
            result = self.service.run(captured['job_id'])
        self.assertEqual(result['status'], 'partial')
        self.assertIn('未取得原包，仅取得部分解析材料', result['gaps'])

    def test_legacy_same_target_duplicate_originals_are_cleaned_after_recovery(self):
        captured = self.service.capture(self.snapshot(description='log.zip 见：【日志基地】钉钉群'))
        job = self.service.state.get(captured['job_id'])
        job['ingestions'] = {}
        copies = []
        for parent in job['candidates']:
            path = self.service._stage(job, parent) / parent['name']
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('system.log', 'same evidence')
            identity = parent['id'] + '-0'
            job['ingestions'][identity] = {'path': str(path), 'name': path.name, 'state': 'processing',
                'processing_path': str(self.service.workspace.staging_dir(job['job_id'],
                    'processing-' + key(identity)) / path.name), 'archive_hashes': [sha256_file(path)]}
            parent['state'] = 'failed'
            copies.append(path)
        self.service.state.put(job)
        self.service.resume(job['job_id'])
        real_unlink = Path.unlink
        interrupted = False
        def crash_after_duplicate_delete(path, *args, **kwargs):
            nonlocal interrupted
            result = real_unlink(path, *args, **kwargs)
            if path == copies[-1] and not interrupted:
                interrupted = True
                raise SystemExit('power interrupted after duplicate unlink')
            return result
        with patch.object(self.service.dws, 'search_files', side_effect=AssertionError('no network needed')):
            with patch.object(Path, 'unlink', crash_after_duplicate_delete):
                with self.assertRaises(SystemExit):
                    self.service.run(job['job_id'])
            result = self.service.run(job['job_id'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(self.service.state.get(job['job_id']).get('ingestions'), {})
        self.assertTrue(all(not path.exists() for path in copies))
        self.assertNotIn('暂存路径', (Path(result['folder']) / 'bugreport.md').read_text())

    def test_processing_failure_does_not_download_same_target_through_fallback(self):
        calls = []
        self.service.config['archive_limits'] = {'max_expanded_size': 3}
        def search(group, name, start, end, directory, **options):
            calls.append(options['prefer_chat'])
            with zipfile.ZipFile(directory / name, 'w') as archive:
                archive.writestr('system.log', 'actual logs')
            return {'candidates': [{'name': name, 'local_path': str(directory / name)}],
                    'coverage': {'complete': True}, 'ledger': [], 'gaps': []}
        self.service.dws.search_files = search
        captured = self.service.capture(self.snapshot(description='log.zip 见：【日志基地】钉钉群',
            fields={'created_at': '2026-11-01T00:00:00+08:00'}))
        first = self.service.run(captured['job_id'])
        self.assertEqual(first['status'], 'partial')
        self.assertEqual(calls, [True])
        self.service.run(captured['job_id'])
        self.assertEqual(calls, [True])
        self.service.config['archive_limits'] = {'max_expanded_size': 100}
        self.service.resume(captured['job_id'])
        result = self.service.run(captured['job_id'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(calls, [True])
        self.assertEqual(self.service.state.get(captured['job_id']).get('ingestions'), {})

    def test_low_disk_fails_before_creating_processing_copy(self):
        captured = self.service.capture(self.snapshot())
        job = self.service.state.get(captured['job_id'])
        path = self.service.workspace.staging_dir(job['job_id'], 'local') / 'log.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('log', 'actual logs')
        from shutil import _ntuple_diskusage
        with patch('iwork_collector.service.shutil.disk_usage', return_value=_ntuple_diskusage(100, 99, 1)):
            with self.assertRaisesRegex(ValueError, '磁盘空间不足'):
                self.service._ingest(job, {'id': 'local'}, path, path.name)
        self.assertTrue(path.exists())
        self.assertEqual(list((self.root / '.iwork/staging').glob('*/processing-*/*')), [])

    def test_retry_recovers_legacy_dws_child_archive_before_searching_again(self):
        name = 'log_E001-10_20261031-012103_20261109094859237.zip'
        captured = self.service.capture(self.snapshot(
            description=f'[视频/日志信息*]：{name}\n日志及视频统一见：【日志基地】钉钉群'))
        job = self.service.state.get(captured['job_id'])
        parent = next(c for c in job['candidates'] if c['priority'] == 2)
        staged = self.service._stage(job, parent) / name
        with zipfile.ZipFile(staged, 'w') as archive:
            archive.writestr('system.log', 'already downloaded from DingTalk')
        job['ingestions'] = {parent['id'] + '-0': {
            'path': str(staged), 'processing_path': str(self.service.workspace.staging_dir(
                job['job_id'], 'processing-' + key(parent['id'] + '-0')) / name),
            'name': name, 'state': 'processing', 'archive_hashes': [sha256_file(staged)]}}
        for candidate in job['candidates']:
            candidate['state'] = 'failed'
        self.service.state.put(job)
        self.service.resume(job['job_id'])
        with patch.object(self.service.dws, 'search_files', side_effect=AssertionError('must reuse local ZIP')):
            result = self.service.run(job['job_id'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual((Path(result['folder']) / 'system.log').read_text(),
                         'already downloaded from DingTalk')
        self.assertFalse(staged.exists())
        self.assertEqual(self.service.state.get(job['job_id']).get('ingestions'), {})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'work'
        self.downloads = Path(self.temp.name) / 'Downloads'
        self.downloads.mkdir()
        self.service = Service(self.root, downloads_root=self.downloads)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def snapshot(self, **values):
        return {'bug_id': '123456', 'title': '测试 Bug', 'description': '黑屏复现',
                'coverage': {'complete': True, 'gaps': []}, **values}

    def test_explicit_recapture_retries_failed_exact_group_file_and_extracts_into_same_bug_folder(self):
        name = 'log_E001-10_20260727-020100_20260727170510433.zip'
        group = '数字座舱统一日志基地'
        snapshot = self.snapshot(bug_id='10000001', fields={'created_at': '2026-07-24T00:00:00+08:00'}, description=
            f'[视频/日志信息*]：{name}\n日志及视频统一见：【{group}】钉钉群')
        calls, available = [], False
        def runner(args, cwd):
            calls.append(args)
            if args[1:3] == ['chat', 'search']:
                return {'result': {'groups': [{'title': group, 'openConversationId': 'cid-fixture',
                    'extension': {'newCSpaceIdIM': '27569621904'}}]}}
            if args[1:4] == ['chat', 'message', 'search']:
                return {'result': {'conversationMessagesList': [{'title': group, 'messages': [{
                    'openMessageId': 'message-fixture', 'content': f'[文件] {name} fileId:file-fixture',
                    'resourceRefs': [{'name': name, 'resourceId': 'file-fixture', 'type': 'fileId'}]}]}]}}
            if args[1:3] == ['drive', 'list']:
                return {'result': {'items': [], 'nextToken': ''}}
            if args[1:3] == ['chat', '+messages-mget']:
                self.assertEqual(args[args.index('--msg-ids') + 1], 'message-fixture')
                response = {'messages': [{'messageId': 'message-fixture', 'conversationId': 'cid-fixture',
                    'resourceRefs': [{'name': name, 'resourceId': 'fresh-file-fixture', 'type': 'fileId'}]}]}
                if '--download-resources' in args:
                    if not available:
                        raise ValueError('DWS 调用失败：临时下载故障')
                    output = cwd / args[args.index('--output-dir') + 1] / name
                    with zipfile.ZipFile(output, 'w') as archive:
                        archive.writestr('android/system.log', 'regression fixture: actual ZIP file contents')
                    response['resourceDownloads'] = {'ok': True, 'failures': [], 'downloads': [{
                        'resourceType': 'fileId', 'resourceId': 'fresh-file-fixture', 'messageId': '',
                        'localPath': str(output.relative_to(cwd)), 'sizeBytes': output.stat().st_size}]}
                return response
            self.fail(f'unexpected command {args}')
        self.service.dws = DwsClient(runner=runner)
        with patch('iwork_collector.service.collect_vehicle_logs') as portal:
            first = self.service.capture(snapshot)
            self.assertEqual(self.service.run(first['job_id'])['status'], 'partial')
            available = True
            quiet = self.service.dispatch('capture', {'snapshot': snapshot})
            self.assertEqual(quiet['status'], 'partial')
            captured = self.service.dispatch('capture', {'snapshot': snapshot, 'retry_failed': True})
            self.assertEqual(captured['status'], 'captured')
            self.assertEqual(captured['job_id'], first['job_id'])
            result = self.service.run(captured['job_id'])
            portal.assert_not_called()
        self.assertEqual(result['status'], 'ready')
        folder = Path(result['folder'])
        self.assertEqual(result['folder'], first['folder'])
        self.assertRegex(folder.name, r'^bug_\d{6}_10000001$')
        extracted = folder / name.removesuffix('.zip') / 'android/system.log'
        self.assertEqual(extracted.read_text(), 'regression fixture: actual ZIP file contents')
        self.assertFalse((folder / name).exists())
        manifest = json.loads((folder / '.iwork/manifest.json').read_text())
        self.assertEqual(manifest['archives'][0]['name'], name)
        self.assertEqual(manifest['artifacts'][0]['source']['kind'], 'dws')
        self.assertIn(name, (folder / 'bugreport.md').read_text())

    def test_extensionless_body_log_is_downloaded_with_actual_name_and_extracted(self):
        identity = 'log_E001-10_20260830-012100_20260830153917485'
        original = identity + '.zip'
        group = '数字座舱统一日志基地'
        def runner(args, cwd):
            if args[1:3] == ['chat', 'search']:
                self.assertEqual(args[args.index('--query') + 1], group)
                return {'result': {'groups': [{'title': group, 'openConversationId': 'cid'}]}}
            if args[1:4] == ['chat', 'message', 'search']:
                self.assertEqual(args[args.index('--query') + 1], identity)
                return {'result': {'conversationMessagesList': [{'messages': [{
                    'openMessageId': 'msg', 'content': f'[文件] {original} fileId:file',
                    'resourceRefs': [{'name': original, 'resourceId': 'file', 'type': 'fileId'}]}]}]}}
            self.assertEqual(args[1:3], ['chat', '+messages-mget'])
            response = {'messages': [{'messageId': 'msg', 'conversationId': 'cid',
                'resourceRefs': [{'name': original, 'resourceId': 'fresh', 'type': 'fileId'}]}]}
            if '--download-resources' in args:
                output = cwd / args[args.index('--output-dir') + 1] / original
                with zipfile.ZipFile(output, 'w') as z:
                    z.writestr('android/system.log', 'verified original archive contents')
                response['resourceDownloads'] = {'ok': True, 'failures': [], 'downloads': [{
                    'resourceType': 'fileId', 'resourceId': 'fresh', 'messageId': '',
                    'localPath': str(output.relative_to(cwd)), 'sizeBytes': output.stat().st_size}]}
            return response
        self.service.dws = DwsClient(runner=runner)
        summary = self.service.capture(self.snapshot(bug_id='10000004', description=
            f'[缺陷描述*]：\n日志群：\n{identity}\n日志及视频统一见：【{group}】钉钉群',
            groups=[{'name': identity}, {'name': group}]))
        result = self.service.run(summary['job_id'])
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(all('覆盖有界' in gap for gap in result['gaps']), result['gaps'])
        folder = Path(result['folder'])
        self.assertEqual((folder / identity / 'android/system.log').read_text(), 'verified original archive contents')
        self.assertFalse((folder / original).exists())
        manifest = json.loads((folder / '.iwork/manifest.json').read_text())
        self.assertEqual(manifest['archives'][0]['name'], original)
        self.assertTrue(manifest['archives'][0]['deleted'])
        self.assertIn(original, (folder / 'bugreport.md').read_text())

    def test_resume_rebuilds_failed_legacy_group_tasks_that_missed_the_extensionless_log(self):
        identity = 'log_E001-10_20260830-012100_20260830153917485'
        group = '数字座舱统一日志基地'
        summary = self.service.capture(self.snapshot(description=
            f'日志群：\n{identity}\n日志及视频统一见：【{group}】钉钉群', groups=[{'name':identity}, {'name':group}]))
        job = self.service.state.get(summary['job_id'])
        job['candidates'] = [{'id':'group-' + str(index), 'target_id':'group-' + str(index), 'state':'failed',
            'name':'', 'priority':2, 'transport':'dws', 'group':{'name':name}, 'source':{'kind':'group_files'}}
            for index, name in enumerate((identity,group))]
        job['source_candidate_ids'] = [candidate['id'] for candidate in job['candidates']]
        self.service.state.put(job)
        self.service.resume(job['job_id'])
        updated = self.service.state.get(job['job_id'])
        self.assertEqual(len(updated['candidates']), 2)
        self.assertEqual({c['name'] for c in updated['candidates']}, {identity})
        self.assertEqual({c['group']['name'] for c in updated['candidates']}, {group})
        self.assertEqual({c['state'] for c in updated['candidates']}, {'pending'})

    def test_explicit_recapture_clears_retired_false_group_failure_before_retrying_new_log_tasks(self):
        identity = 'log_E001-10_20260830-012100_20260830153917485'
        group = '数字座舱统一日志基地'
        snapshot = self.snapshot(description=f'日志群：\n{identity}\n日志及视频统一见：【{group}】钉钉群',
            fields={'created_at':'2026-08-30T00:00:00+08:00'}, groups=[{'name':identity}, {'name':group}])
        summary = self.service.capture(snapshot)
        job = self.service.state.get(summary['job_id'])
        job['candidates'] = [{'id':'group-' + str(index), 'target_id':'group-' + str(index), 'state':'failed',
            'name':'', 'priority':2, 'transport':'dws', 'group':{'name':name}, 'source':{'kind':'group_files'}}
            for index, name in enumerate((identity,group))]
        job.update(status='partial', gaps=['DWS 未找到同名钉钉群：' + identity],
            source_candidate_ids=[candidate['id'] for candidate in job['candidates']])
        self.service.state.put(job)
        refreshed = self.service.capture(snapshot, retry_failed=True)
        self.assertEqual(refreshed['status'], 'captured')
        self.assertFalse(refreshed['gaps'])
        def search(group, name, start, end, directory, **options):
            original = name + '.zip'
            path = directory / original
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('system.log','restored log')
            return {'candidates':[{'name':original,'local_path':str(path),'transport':'local'}],
                    'gaps':[],'ledger':[],'coverage':{'complete':True}}
        self.service.dws.search_files = search
        result = self.service.run(job['job_id'])
        self.assertEqual(result['status'], 'ready', result['gaps'])

    def test_group_original_file_is_attempted_before_log_platform_fallback(self):
        calls = []
        def search(group, name, start, end, directory, **options):
            calls.append(options['prefer_chat'])
            if options['prefer_chat']:
                return {'candidates': [], 'gaps': ['消息附件下载失败'], 'ledger': [],
                        'coverage': {'complete': False}, 'log_links': [{
                            'url': 'https://fawivi-prod-k8-vehiclelogweb.faw.cn/?id=300001'}]}
            archive = directory / name
            archive.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('system.log', 'original group archive fixture')
            return {'candidates': [{'name': name, 'local_path': str(archive), 'transport': 'local',
                                    'source': {'kind': 'dws'}}], 'gaps': [], 'ledger': [],
                    'coverage': {'complete': True}}
        self.service.dws.search_files = search
        summary = self.service.capture(self.snapshot(description='日志 log.zip，见：【日志群】钉钉群'))
        with patch('iwork_collector.service.collect_vehicle_logs') as portal:
            result = self.service.run(summary['job_id'])
            portal.assert_not_called()
        self.assertEqual(calls, [True, False])
        self.assertEqual((Path(result['folder']) / 'system.log').read_text(), 'original group archive fixture')

    def test_recapture_retries_failed_browser_receipt_before_worker_consumes_it(self):
        snapshot = self.snapshot(attachments=[{'id': 'a', 'name': 'retry.log',
                                              'url': 'https://example.test/retry'}])
        summary = self.service.capture(snapshot)
        self.service.run(summary['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        self.service.browser_result(task['task_id'], 'started', download_id=1)
        self.service.browser_result(task['task_id'], 'failed', download_id=1, error='interrupted')
        # The failure worker has not run: the candidate is still waiting.
        result = self.service.capture(snapshot, retry_failed=True)
        self.assertEqual(result['status'], 'captured')
        retried = self.service.state.task_get(task['task_id'])
        self.assertEqual(retried['status'], 'pending')
        self.assertNotIn('download_id', retried)
        self.service.browser_result(task['task_id'], 'started', download_id=2)
        path = self.downloads / task['relative_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('recovered payload')
        self.service.browser_result(task['task_id'], 'complete', download_id=2, path=str(path))
        self.assertEqual(self.service.run(summary['job_id'])['status'], 'ready')

    def test_retry_of_group_source_defers_failed_browser_fallback_until_needed(self):
        available = False
        def search(group, name, start, end, directory, **options):
            candidates = []
            if available:
                archive = directory / name
                archive.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(archive, 'w') as z:
                    z.writestr('system.log', 'original group archive')
                candidates = [{'name': name, 'local_path': str(archive), 'transport': 'local',
                               'source': {'kind': 'dws'}}]
            return {'candidates': candidates, 'gaps': [], 'ledger': [], 'coverage': {'complete': True}}
        self.service.dws.search_files = search
        snapshot = self.snapshot(description='log.zip 见：【日志群】钉钉群',
            fields={'created_at': '2026-07-24T00:00:00+08:00'},
            attachments=[{'id': 'a', 'name': 'log.zip', 'url': 'https://example.test/log.zip'}])
        summary = self.service.capture(snapshot)
        self.service.run(summary['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        self.service.browser_result(task['task_id'], 'started', download_id=1)
        self.service.browser_result(task['task_id'], 'failed', download_id=1, error='interrupted')
        self.service.run(summary['job_id'])
        # All sources still fail: only the worker may requeue the browser fallback.
        self.service.capture(snapshot, retry_failed=True)
        self.assertEqual(self.service.browser_poll()['tasks'], [])
        self.assertEqual(self.service.run(summary['job_id'])['status'], 'waiting_browser')
        self.assertEqual(len(self.service.browser_poll()['tasks']), 1)
        self.service.browser_result(task['task_id'], 'started', download_id=2)
        self.service.browser_result(task['task_id'], 'failed', download_id=2, error='interrupted')
        self.service.run(summary['job_id'])
        # Now the group source recovers, so no browser fallback should be queued.
        available = True
        self.service.capture(snapshot, retry_failed=True)
        self.assertEqual(self.service.browser_poll()['tasks'], [])
        result = self.service.run(summary['job_id'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['waiting_count'], 0)
        self.assertEqual(self.service.browser_poll()['tasks'], [])
        self.assertEqual((Path(result['folder']) / 'system.log').read_text(), 'original group archive')

    def test_recapture_keeps_existing_browser_handoff_ahead_of_recovered_group_source(self):
        for index, status in enumerate(('pending', 'started', 'complete')):
            with self.subTest(status=status):
                calls, available = [], False
                def search(group, name, start, end, directory, **options):
                    calls.append(options['prefer_chat'])
                    if available:
                        archive = directory / name
                        archive.parent.mkdir(parents=True, exist_ok=True)
                        with zipfile.ZipFile(archive, 'w') as z:
                            z.writestr('system.log', 'recovered group archive')
                        return {'candidates': [{'name': name, 'local_path': str(archive),
                            'transport': 'local', 'source': {'kind': 'dws'}}],
                            'gaps': [], 'ledger': [], 'coverage': {'complete': True}}
                    return {'candidates': [], 'gaps': [], 'ledger': [], 'coverage': {'complete': True}}
                self.service.dws.search_files = search
                snapshot = self.snapshot(bug_id=str(100 + index),
                    description='log.zip 见：【日志群】钉钉群',
                    fields={'created_at': '2026-07-24T00:00:00+08:00'},
                    attachments=[{'id': 'a', 'name': 'log.zip', 'url': 'https://example.test/log.zip'}])
                summary = self.service.capture(snapshot)
                self.service.run(summary['job_id'])
                task = next(t for t in self.service.browser_poll()['tasks'] if t['job_id'] == summary['job_id'])
                path = self.downloads / task['relative_path']
                path.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('system.log', 'completed browser archive')
                if status != 'pending':
                    self.service.browser_result(task['task_id'], 'started', download_id=1)
                if status == 'complete':
                    self.service.browser_result(task['task_id'], 'complete', download_id=1, path=str(path))
                available = True
                self.service.capture(snapshot, retry_failed=True)
                if status == 'pending':
                    self.service.browser_result(task['task_id'], 'started', download_id=1)
                if status != 'complete':
                    self.service.browser_result(task['task_id'], 'complete', download_id=1, path=str(path))
                # The completed fallback must be ingested even if the old higher
                # priority source could recover on a new search.
                result = self.service.run(summary['job_id'])
                self.assertEqual(calls, [True, False])
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(result['waiting_count'], 0)
                self.assertEqual((Path(result['folder']) / 'system.log').read_text(), 'completed browser archive')

    def test_local_collect_has_original_names_report_links_and_no_archive(self):
        image = Path(self.temp.name) / '原始日志 #1.txt'
        image.write_text('log line\n')
        summary = self.service.capture(self.snapshot(attachments=[
            {'id':'one','name':image.name,'local_path':str(image),'transport':'local'}]), trusted=True)
        result = self.service.run(summary['job_id'])
        folder = Path(result['folder'])
        self.assertEqual(result['status'], 'ready')
        self.assertTrue((folder / image.name).exists())
        report = (folder / 'bugreport.md').read_text()
        self.assertIn('%20%231.txt', report)
        self.assertTrue((folder / '.iwork/manifest.json').exists())
        self.assertTrue(image.exists())

    def test_browser_roundtrip_and_restart_restore(self):
        summary = self.service.capture(self.snapshot(attachments=[
            {'id':'a','name':'日志.txt','url':'https://example.test/a','transport':'browser'}]))
        self.service.run(summary['job_id'])
        tasks = self.service.browser_poll()['tasks']
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        path = self.downloads / task['relative_path']
        path.parent.mkdir(parents=True)
        path.write_text('browser log')
        self.service.browser_result(task['task_id'], status='started', download_id=45)
        self.service.close()
        self.service = Service(self.root, downloads_root=self.downloads)
        self.service.browser_result(task['task_id'], status='complete', download_id=45, path=str(path))
        result = self.service.run(summary['job_id'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual((Path(result['folder']) / '日志.txt').read_text(), 'browser log')
        self.assertFalse(path.exists())
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_service_upgrade_migrates_all_jobs_and_resumes_existing_browser_task(self):
        snapshot = self.snapshot(attachments=[{'id': 'a', 'name': '原名.log',
                                              'url': 'https://example.test/a'}])
        first = self.service.capture(snapshot)
        second = self.service.capture(dict(snapshot, description='second capture'))
        self.service.run(second['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        folder = Path(second['folder'])
        date = folder.name.split('_')[1]
        # Convert a current workspace into the shape of a 0.1.4 installation.
        legacy = self.root / f'bug_123456_{date}'
        if folder != legacy:
            folder.rename(legacy)
        for job in self.service.state.jobs():
            job['folder'] = str(legacy)
            self.service.state.put(job)
        self.service.close()
        self.service = Service(self.root, downloads_root=self.downloads)
        expected = self.root / f'bug_{date}_123456'
        for job_id in (first['job_id'], second['job_id']):
            status = self.service.dispatch('status', {'job_id': job_id})
            self.assertEqual(status['folder'], str(expected))
            self.assertTrue(Path(status['report_path']).is_file())
        self.assertFalse(legacy.exists())
        self.assertTrue((expected / '.iwork/source' / first['job_id'] / 'ticket.json').is_file())
        path = self.downloads / task['relative_path']
        path.parent.mkdir(parents=True)
        path.write_text('resumed evidence')
        self.service.browser_result(task['task_id'], 'complete', download_id=1, path=str(path))
        self.assertEqual(self.service.run(second['job_id'])['status'], 'ready')
        self.assertEqual((expected / '原名.log').read_text(), 'resumed evidence')

    def test_startup_cleans_only_empty_managed_download_directories(self):
        managed = self.downloads / 'iwork-collector'
        empty = managed / ('job_' + 'a' * 32) / ('task_' + 'b' * 32)
        empty.mkdir(parents=True)
        active = managed / ('job_' + 'c' * 32) / ('task_' + 'd' * 32)
        active.mkdir(parents=True)
        (active / 'log.zip.crdownload').write_bytes(b'in progress')
        other = self.downloads / 'personal'
        other.mkdir()
        self.service.close()
        self.service = Service(self.root, downloads_root=self.downloads)
        self.assertFalse(empty.parent.exists())
        self.assertEqual((active / 'log.zip.crdownload').read_bytes(), b'in progress')
        self.assertTrue(other.is_dir())

    def test_completed_handoff_does_not_remove_unrelated_download_file(self):
        snapshot = self.snapshot(attachments=[{'id': 'a', 'name': 'log.txt', 'url': 'https://example.test/a'}])
        summary = self.service.capture(snapshot)
        self.service.run(summary['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        path = self.downloads / task['relative_path']
        path.parent.mkdir(parents=True)
        path.write_text('evidence')
        other = path.parent / 'user-file.txt'
        other.write_text('user data')
        self.service.browser_result(task['task_id'], 'complete', download_id=1, path=str(path))
        self.assertEqual(other.read_text(), 'user data')
        self.assertFalse(path.exists())

    def test_download_cleanup_leaves_active_empty_task_and_symlink_targets_alone(self):
        summary = self.service.capture(self.snapshot(attachments=[
            {'id': 'a', 'name': 'log.txt', 'url': 'https://example.test/a'}]))
        self.service.run(summary['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        active = (self.downloads / task['relative_path']).parent
        active.mkdir(parents=True)
        outside = Path(self.temp.name) / 'unrelated'
        outside.mkdir()
        (outside / ('task_' + 'e' * 32)).mkdir()
        link = self.downloads / 'iwork-collector' / ('job_' + 'f' * 32)
        link.symlink_to(outside, target_is_directory=True)
        self.service._prune_browser_directories()
        self.assertTrue(active.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertTrue((outside / ('task_' + 'e' * 32)).is_dir())

    def test_comment_image_tasks_keep_page_context_and_comment_provenance(self):
        summary = self.service.capture(self.snapshot(page_tab_id=17,
            url='https://iwork.faw.cn/development/project/defect?entityId=123456',
            comments=[{'id': 'c1', 'author': '张三', 'text': '看图', 'image_ids': ['img1']}],
            images=[{'id': 'img1', 'name': '原图.png', 'url': 'https://devops.faw.cn/image/1',
                     'page_fetch': True, 'comment_ids': ['c1']}]))
        self.service.run(summary['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        self.assertTrue(task['page_fetch'])
        self.assertEqual(task['page_tab_id'], 17)
        candidate = self.service.state.get(summary['job_id'])['candidates'][0]
        self.assertEqual(candidate['source']['comment_ids'], ['c1'])

    def test_recapturing_same_images_refreshes_the_page_tab_for_pending_tasks(self):
        snapshot = self.snapshot(page_tab_id=17, url='https://iwork.faw.cn/bug/123456',
            images=[{'id': 'img1', 'name': '原图.png', 'url': 'https://devops.faw.cn/image/1', 'page_fetch': True}])
        summary = self.service.capture(snapshot)
        self.service.run(summary['job_id'])
        recaptured = self.service.capture(dict(snapshot, page_tab_id=42))
        self.assertEqual(recaptured['job_id'], summary['job_id'])
        self.assertEqual(self.service.browser_poll()['tasks'][0]['page_tab_id'], 42)


    def test_log_portal_fallback_preserves_tree_and_extracts_nested_archives(self):
        path = self.service.workspace.staging_dir('job_fixture', 'portal') / 'android.log.zip'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('system.log', 'actual log evidence')
        self.service.dws.search_files = lambda *a, **k: {
            'candidates': [], 'gaps': ['DWS 下载失败'], 'ledger': [], 'coverage': {'complete': False},
            'log_links': [{'url': 'https://fawivi-prod-k8-vehiclelogweb.faw.cn/?id=300001'}]}
        portal_result = {'candidates': [{'name': path.name, 'local_path': str(path), 'transport': 'local',
            'relative_parent': 'log/android', 'source': {'kind': 'vehicle-log-platform', 'mode': 'parsed-member'}}],
            'gaps': ['已取得平台解析文件，未取得原包'], 'ledger': [], 'coverage': {'complete': False}}
        summary = self.service.capture(self.snapshot(groups=[{'name': '日志群'}], file_mentions=[{'name': 'log.zip'}]))
        with patch('iwork_collector.service.collect_vehicle_logs', return_value=portal_result):
            result = self.service.run(summary['job_id'])
        folder = Path(result['folder'])
        self.assertEqual((folder / 'log/android/system.log').read_text(), 'actual log evidence')
        self.assertFalse(list(folder.rglob('*.zip')))
        manifest = json.loads((folder / '.iwork/manifest.json').read_text())
        self.assertEqual(manifest['artifacts'][0]['path'], 'log/android/system.log')
        self.assertTrue(any('已取得平台解析文件，未取得原始压缩包' in source['status']
                            for source in manifest['sources']))
        self.assertEqual(result['status'], 'partial')
        self.assertNotIn('采集缺口', (folder / 'bugreport.md').read_text())

    def test_browser_cannot_claim_arbitrary_local_file(self):
        summary = self.service.capture(self.snapshot(attachments=[
            {'name':'log.txt','url':'https://example.test/a'}]))
        self.service.run(summary['job_id'])
        task = self.service.browser_poll()['tasks'][0]
        private = Path(self.temp.name) / 'log.txt'
        private.write_text('private')
        with self.assertRaises(ValueError):
            self.service.browser_result(task['task_id'], status='complete', download_id=2, path=str(private))

    def test_same_snapshot_is_idempotent_and_source_redacts_credentials(self):
        snapshot = self.snapshot(description='地址 https://example.test/log?token=secret')
        a = self.service.capture(snapshot)
        b = self.service.capture(snapshot)
        self.assertEqual(a['job_id'], b['job_id'])
        persisted = Path(a['folder']) / '.iwork/source' / a['job_id'] / 'ticket.json'
        self.assertNotIn('secret', persisted.read_text())

    def test_nested_archive_deleted_from_managed_staging(self):
        archive = Path(self.temp.name) / '设备日志.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            stream.writestr('system.log', 'actual evidence')
        summary = self.service.capture(self.snapshot(attachments=[
            {'name':archive.name,'local_path':str(archive),'transport':'local'}]), trusted=True)
        result = self.service.run(summary['job_id'])
        folder = Path(result['folder'])
        self.assertTrue((folder/'system.log').exists())
        self.assertFalse(list(folder.rglob('*.zip')))
        self.assertTrue(archive.exists())
        manifest = json.loads((folder/'.iwork/manifest.json').read_text())
        self.assertTrue(manifest['archives'][0]['deleted'])

    def test_missing_dws_is_partial_and_other_attachment_succeeds(self):
        path = Path(self.temp.name) / 'log.txt'
        path.write_text('ok')
        summary = self.service.capture(self.snapshot(groups=[{'name':'测试群'}], attachments=[
            {'name':'log.txt','local_path':str(path),'transport':'local'}]), trusted=True)
        result = self.service.run(summary['job_id'])
        self.assertEqual(result['status'], 'partial')
        self.assertTrue((Path(result['folder'])/'log.txt').exists())
        self.assertIn('DWS', ' '.join(result['gaps']))

    def test_cancel_creates_honest_report(self):
        summary = self.service.capture(self.snapshot())
        result = self.service.cancel(summary['job_id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(Path(result['report_path']).exists())

    def test_priority_waiting_does_not_spin_on_lower_source(self):
        snapshot = self.snapshot(attachments=[
            {'id':'first','target_id':'same','name':'log.txt','url':'https://example.test/a'},
            {'id':'second','target_id':'same','name':'log.txt','url':'https://example.test/b'}])
        summary = self.service.capture(snapshot)
        try:
            proc = subprocess.run([sys.executable, '-m', 'iwork_collector', '--root', str(self.root),
                                   'worker', summary['job_id']], capture_output=True, timeout=2)
            finished = proc.returncode == 0
        except subprocess.TimeoutExpired:
            finished = False
        self.assertTrue(finished, '等待高优先级来源时不能在较低来源上无限循环')

    def test_dws_window_uses_date_in_explicit_log_filename(self):
        snapshot = self.snapshot(description='视频/日志信息：log_E001-10_20260727-020100_20260727170510433.zip')
        job = {'snapshot': snapshot}
        candidate = {'name': 'log_E001-10_20260727-020100_20260727170510433.zip'}
        start, end, basis = self.service._dws_window(job, candidate,
            datetime.fromisoformat('2026-11-12T05:00:00+00:00'))
        self.assertEqual(start, '2026-07-24T00:00:00+00:00')
        self.assertEqual(end, '2026-07-31T00:00:00+00:00')
        self.assertIn('文件名日期', basis)

    def test_recapture_with_refreshed_one_time_image_token_reuses_same_job(self):
        base = 'https://devops.faw.cn/api/document/fileRelation/getRelatedFile/' + 'a' * 32 + '/'
        first = self.service.capture(self.snapshot(images=[{'id': 'api-image-1', 'name': 'image.png', 'url': base + 'b' * 32}]))
        second = self.service.capture(self.snapshot(images=[{'id': 'api-image-1', 'name': 'image.png', 'url': base + 'c' * 32}]))
        self.assertEqual(first['job_id'], second['job_id'])

    def test_capture_resume_cancel_do_not_block_while_worker_holds_lock(self):
        captured = self.service.capture(self.snapshot())
        with self.service._bug_lock('123456'):
            again = self.service.capture(self.snapshot(description='刷新后的新描述'))
            resumed = self.service.resume(captured['job_id'])
            with self.assertRaises(ValueError):
                self.service.cancel(captured['job_id'])
        self.assertEqual(again['job_id'], captured['job_id'])
        self.assertEqual(resumed['job_id'], captured['job_id'])

    def test_browser_result_accepts_handoff_while_worker_holds_lock(self):
        captured = self.service.capture(self.snapshot(attachments=[
            {'id': 'a1', 'name': '附件.txt', 'url': 'https://example.com/a.txt'}]))
        summary = self.service.run(captured['job_id'])
        self.assertEqual(summary['status'], 'waiting_browser')
        task = self.service.state.tasks(captured['job_id'])[0]
        target = self.downloads / task['relative_path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('evidence')
        with self.service._bug_lock('123456'):
            result = self.service.browser_result(task['task_id'], 'complete', download_id=7, path=str(target))
        self.assertEqual(result['job_id'], captured['job_id'])
        self.assertEqual(self.service.state.task_get(task['task_id'])['status'], 'complete')

    def test_resume_request_during_worker_is_applied_by_next_run(self):
        captured = self.service.capture(self.snapshot(attachments=[
            {'id': 'a1', 'name': '附件.txt', 'url': 'https://example.com/a.txt'}]))
        self.service.run(captured['job_id'])
        task = self.service.state.tasks(captured['job_id'])[0]
        self.service.browser_result(task['task_id'], 'started', download_id=1)
        self.service.browser_result(task['task_id'], 'failed', download_id=1, error='interrupted')
        with self.service._bug_lock('123456'):
            self.service.resume(captured['job_id'])
            self.assertTrue(self.service._pending_request(captured['job_id']).exists())
        result = self.service.run(captured['job_id'])
        self.assertEqual(result['status'], 'waiting_browser')
        self.assertEqual(self.service.state.task_get(task['task_id'])['status'], 'pending')
        self.assertFalse(self.service._pending_request(captured['job_id']).exists())


class NativeTests(unittest.TestCase):
    def test_framing_roundtrip_and_truncated_message(self):
        data = {'id':'1','method':'ping','params':{}}
        output = io.BytesIO()
        write_message(output, data)
        self.assertEqual(read_message(io.BytesIO(output.getvalue())), data)
        with self.assertRaises(ValueError):
            read_message(io.BytesIO(struct.pack('=I', 40) + b'{}'))


if __name__ == '__main__':
    unittest.main()
