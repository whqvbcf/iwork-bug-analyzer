import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from iwork_collector.report import _source_rows, render, write_report


class ReportTests(unittest.TestCase):
    def test_agent_report_does_not_enumerate_thousands_of_files_or_archive_hashes(self):
        job = {'job_id': 'job_1', 'bug_id': '1', 'status': 'ready', 'updated_at': '2026-11-13',
               'snapshot': {'title': '缺陷', 'description': '黑屏后重启', 'fields': {}, 'coverage': {'complete': True}},
               'candidates': [], 'attempts': []}
        manifest = {'revision': 1, 'artifacts': [
            {'name': f'log-{i}.txt', 'path': f'vehicle-logs/log-{i}.txt', 'size': 5} for i in range(2000)],
            'archives': [{'name': f'archive-{i}.log', 'sha256': 'a' * 64, 'deleted': True, 'members': []}
                         for i in range(300)], 'images': [], 'gaps': []}
        text = render(job, manifest)
        self.assertLess(len(text), 4000)
        self.assertIn('[vehicle-logs/](vehicle-logs/)', text)
        self.assertIn('2000', text)
        self.assertNotIn('## 证据有效性与检索结论', text)
        self.assertNotIn('## 证据导航', text)
        self.assertNotIn('a' * 64, text)
        self.assertIn('.iwork/manifest.json', text)

    def test_same_named_attachment_failure_is_retained_in_source_details(self):
        job = {'job_id': 'job_1', 'bug_id': '1', 'status': 'partial', 'updated_at': '2026-11-13',
               'snapshot': {'title': '缺陷', 'fields': {}, 'coverage': {'complete': True}},
               'candidates': [
                   {'id': 'first', 'target_id': 'attachment-url-1', 'name': '图片.png', 'state': 'done'},
                   {'id': 'second', 'target_id': 'attachment-url-2', 'name': '图片.png', 'state': 'failed'}],
               'attempts': [{'candidate': 'second', 'status': 'failed', 'error': 'image unavailable'}]}
        text = render(job, {'revision': 1, 'artifacts': [], 'archives': [], 'images': [], 'gaps': []})
        self.assertNotIn('image unavailable', text)
        self.assertIn('image unavailable', _source_rows(job)[1]['status'])

    def test_corrupt_archive_is_not_called_successfully_expanded(self):
        job = {'job_id': 'job_1', 'bug_id': '1', 'status': 'partial', 'updated_at': '2026-11-13',
               'snapshot': {'fields': {}, 'coverage': {'complete': True}}, 'candidates': [], 'attempts': []}
        text = render(job, {'revision': 1, 'artifacts': [], 'images': [], 'gaps': ['CRC error'],
                           'archives': [{'name': 'bad.zip', 'deleted': False, 'members': []}]})
        self.assertNotIn('已展开：bad.zip', text)

    def test_pending_fallback_does_not_hide_downloaded_material_processing_failure(self):
        job = {'job_id': 'job_1', 'bug_id': '1', 'status': 'partial', 'updated_at': '2026-11-13',
               'snapshot': {'fields': {}, 'coverage': {'complete': True}},
               'candidates': [
                   {'id': 'p1', 'target_id': 'log', 'name': 'logs.zip', 'state': 'failed', 'priority': 1,
                    'downloaded_files': [{'local_path': '/staging/logs.zip', 'name': 'logs.zip'}]},
                   {'id': 'p2', 'target_id': 'log', 'name': 'logs.zip', 'state': 'pending', 'priority': 2}],
               'attempts': [{'candidate': 'p1', 'status': 'failed', 'error': 'CRC mismatch'}]}
        text = render(job, {'revision': 1, 'artifacts': [], 'images': [], 'archives': [],
                           'gaps': ['logs.zip：CRC mismatch']})
        self.assertIn('已下载、待处理的文件', text)
        self.assertNotIn('CRC mismatch', text)
        self.assertIn('CRC mismatch', _source_rows(job)[0]['status'])

    def test_checkpointed_download_before_ingestion_failure_has_retained_location(self):
        job = {'job_id': 'job_1', 'bug_id': '1', 'status': 'partial', 'updated_at': '2026-11-13',
               'snapshot': {'title': '缺陷', 'fields': {}, 'coverage': {}},
               'candidates': [{'id': 'mention-0', 'state': 'failed', 'name': 'logs.zip', 'priority': 1,
                   'downloaded_files': [{'id': 'mention-0-0', 'name': 'logs.zip',
                       'local_path': '/work/.iwork/staging/job_1/logs.zip', 'size': 123, 'sha256': 'b' * 64}]}],
               'attempts': [{'candidate': 'mention-0', 'status': 'failed', 'error': '磁盘空间不足'}]}
        text = render(job, {'revision': 1, 'artifacts': [], 'archives': [], 'images': [], 'gaps': []})
        self.assertIn('已下载、待处理的文件', text)
        self.assertNotIn('未下载：磁盘空间不足', text)
        self.assertIn('/work/.iwork/staging/job_1/logs.zip', text)

    def test_downloaded_archive_with_extraction_failure_is_not_reported_as_missing(self):
        job = {'job_id': 'job_1', 'bug_id': '1', 'status': 'partial', 'updated_at': '2026-11-13',
               'snapshot': {'title': '缺陷', 'fields': {}, 'coverage': {}},
               'candidates': [{'id': 'group-0-0', 'state': 'failed', 'name': 'logs.zip', 'priority': 2}],
               'attempts': [{'candidate': 'group-0-0', 'status': 'failed', 'error': 'expanded size budget exceeded'}],
               'ingestions': {'group-0-0-0': {'name': 'logs.zip', 'state': 'processing',
                   'path': '/work/.iwork/staging/job_1/logs.zip', 'size': 3843712707,
                   'sha256': 'a' * 64}}}
        manifest = {'revision': 1, 'artifacts': [], 'archives': [], 'images': [], 'gaps': []}
        text = render(job, manifest)
        self.assertIn('已下载、待处理的文件', text)
        self.assertNotIn('未下载：expanded size budget exceeded', text)
        self.assertIn('/work/.iwork/staging/job_1/logs.zip', text)
        self.assertIn('3843712707', text)
        self.assertIn('.iwork/manifest.json', text)

    def test_located_group_file_failure_stays_in_manifest_without_report_diagnostics(self):
        job = {'job_id': 'job_1', 'bug_id': '1', 'status': 'partial', 'updated_at': '2026-11-13',
               'snapshot': {'title': '缺陷', 'fields': {}, 'coverage': {}}, 'candidates': [], 'attempts': []}
        manifest = {'revision': 1, 'artifacts': [], 'archives': [], 'images': [], 'gaps': [],
                    'searches': [{'complete': False, 'matched_files': [{'name': 'log.zip',
                        'message_id': 'message-1', 'created_at': '2026-07-27 17:27:03'}],
                        'download_status': 'failed', 'download_error': 'socket: operation not permitted'}]}
        text = render(job, manifest)
        self.assertNotIn('采集缺口', text)
        self.assertNotIn('socket: operation not permitted', text)
        self.assertEqual(manifest['searches'][0]['download_status'], 'failed')

    def test_authorization_refresh_result_stays_in_manifest_without_report_diagnostics(self):
        job = {'job_id': 'job_1', 'bug_id': '1', 'status': 'partial', 'updated_at': '2026-11-13',
               'snapshot': {'title': '缺陷', 'fields': {}, 'coverage': {}}, 'candidates': [], 'attempts': []}
        manifest = {'revision': 1, 'artifacts': [], 'archives': [], 'images': [], 'gaps': [],
                    'searches': [{'complete': False, 'authorization_refresh': 'unchanged',
                                  'authorization_expired': True, 'record_id': '300001'}]}
        text = render(job, manifest)
        self.assertNotIn('采集缺口', text)
        self.assertNotIn('过期签名', text)
        self.assertEqual(manifest['searches'][0]['authorization_refresh'], 'unchanged')

    def test_comment_authors_reply_links_and_local_images_are_kept_together(self):
        job = {
            'job_id': 'job_1', 'bug_id': '1', 'status': 'partial', 'updated_at': '2026-11-12',
            'snapshot': {'title': '缺陷', 'description': '问题', 'fields': {},
                         'coverage': {'gaps': []}, 'comments': [
                             {'id': 'c1', 'author': '张三', 'text': '主评论\n第二行', 'image_ids': ['img1']},
                             {'id': 'c2', 'author': '李四', 'text': '麻烦@李四接力', 'parent_id': 'c1'}],
                         'images': [{'id': 'img1', 'name': '背光 截图.png', 'comment_ids': ['c1']}],
                         'timeline': [{'id': 'h1', 'author': '王五', 'text': '把<span>状态</span>置为已解决'}]},
            'candidates': [], 'attempts': []
        }
        manifest = {'revision': 1, 'artifacts': [
            {'name': '背光 截图.png', 'path': '背光 截图.png', 'source_key': 'images-img1'}],
            'archives': [], 'images': [], 'gaps': []}
        text = render(job, manifest)
        self.assertIn('张三', text)
        self.assertIn('李四', text)
        self.assertIn('麻烦@李四接力', text)
        self.assertIn('回复 [张三 / c1](#comment-c1)', text)
        self.assertIn(quote('背光 截图.png'), text)
        self.assertLess(text.index(quote('背光 截图.png')), text.index('### 变更时间线'))
        self.assertIn('王五', text)
        self.assertNotIn('<span>', text)

    def test_report_keeps_original_log_clue_but_omits_collection_diagnostics(self):
        job = {
            'job_id': 'job_1', 'bug_id': '10000001', 'status': 'partial',
            'updated_at': '2026-11-12T12:00:00+08:00', 'revision': 1,
            'snapshot': {'title': '缺陷', 'url': 'https://iwork.example/bug/10000001',
                         'description': '问题描述', 'fields': {
                             '视频/日志信息': 'log_E001-10_20260727-020100_20260727170510433.zip'},
                         'comments': [], 'coverage': {'complete': False, 'gaps': ['页面未完整采集']}},
            'artifacts': [], 'archives': [], 'images': [],
            'gaps': ['DWS 不可用：安装 dws 并登录后，重试该群的文件采集'],
            'searches': [],
            'candidates': [{'id':'mention-0', 'name':'log_E001-10_20260727-020100_20260727170510433.zip',
                            'priority':1, 'state':'failed', 'group':{'name':'数字座舱统一日志基地'},
                            'source':{'kind':'group_mention'}}],
            'attempts': [{'candidate':'mention-0', 'status':'failed', 'error':'DWS 不可用'}]
        }
        text = render(job, {'revision':1, 'artifacts':[], 'archives':[], 'images':[], 'gaps':job['gaps']})
        self.assertNotIn('采集缺口', text)
        self.assertIn('已入库 0 个文件', text)
        self.assertIn('log_E001-10_20260727-020100_20260727170510433.zip', text)
        self.assertNotIn('DWS 不可用', text)
        self.assertNotIn('未下载', text)
        self.assertIn('DWS 不可用', _source_rows(job)[0]['status'])

    def test_report_distinguishes_empty_verified_comments_from_unavailable_timeline(self):
        job = {
            'job_id': 'job_2', 'bug_id': '2', 'status': 'partial',
            'updated_at': '2026-11-12T12:00:00+08:00', 'revision': 1,
            'snapshot': {
                'title': '缺陷', 'url': 'https://iwork.example/bug/2', 'description': '问题',
                'fields': {}, 'comments': [], 'timeline': [],
                'comment_coverage': {'attempted': True, 'complete': True},
                'timeline_coverage': {'attempted': True, 'complete': False},
                'coverage': {'complete': False, 'gaps': ['时间线接口失败']}
            },
            'artifacts': [], 'archives': [], 'images': [], 'gaps': ['时间线接口失败'],
            'searches': [], 'candidates': [], 'attempts': []
        }
        text = render(job, {'revision': 1, 'artifacts': [], 'archives': [], 'images': [], 'gaps': job['gaps']})
        self.assertIn('已查询 iWork 评论接口，返回 0 条；完整性：已证实', text)
        self.assertIn('已查询 iWork 变更记录接口，返回 0 条；完整性：未证实', text)

    def test_written_manifest_preserves_diagnostics_removed_from_agent_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {'job_id': 'job_1', 'bug_id': '1', 'status': 'partial', 'updated_at': '2026-11-16',
                'folder': temporary, 'snapshot': {'description': 'facts', 'coverage': {}},
                'artifacts': [], 'images': [], 'archives': [], 'gaps': ['missing-page-detail'],
                'searches': [{'download_status': 'failed', 'download_error': 'expired-signature'}],
                'candidates': [{'id': 'file1', 'name': 'logs.zip', 'state': 'failed'}],
                'attempts': [{'candidate': 'file1', 'status': 'failed', 'error': 'resource-not-found'}]}
            write_report(job)
            text = (Path(temporary) / 'bugreport.md').read_text()
            for diagnostic in ['采集缺口', 'missing-page-detail', 'expired-signature', 'resource-not-found']:
                self.assertNotIn(diagnostic, text)
            manifest = json.loads((Path(temporary) / '.iwork/manifest.json').read_text())
            self.assertEqual(manifest['gaps'], ['missing-page-detail'])
            self.assertEqual(manifest['searches'], job['searches'])
            self.assertIn('resource-not-found', manifest['sources'][0]['status'])


class ReportLocalLinkTests(unittest.TestCase):
    def job(self, **changes):
        return {'job_id': 'job_1', 'bug_id': '10000002', 'status': 'partial', 'updated_at': '2026-11-16',
            'snapshot': {'description': '[LOG]\n日志统一见：【数字座舱统一日志基地】钉钉群：\nlogs.zip\n[视频/图片]\n见附件',
                'fields': {}, 'coverage': {}},
            'candidates': [{'id': 'mention-0', 'target_id': 'log', 'name': 'logs.zip', 'state': 'done',
                'downloaded_files': [{'id': 'mention-0-0', 'name': 'logs.zip', 'state': 'done'}]}],
            'attempts': [], **changes}

    def manifest(self, artifacts):
        return {'revision': 1, 'artifacts': artifacts, 'archives': [], 'images': [], 'gaps': ['hidden-gap']}

    def test_original_archive_name_links_to_local_extracted_directory_by_source(self):
        manifest = self.manifest([
            {'name': 'a.log', 'path': 'files/conflict/logs/android/a.log', 'source_key': 'mention-0-0'},
            {'name': 'b.log', 'path': 'files/conflict/logs/linux/b.log', 'source_key': 'mention-0-0'}])
        text = render(self.job(), manifest)
        self.assertIn('[本地日志]\n- [./files/conflict/logs/](./files/conflict/logs/)', text)
        self.assertNotIn('日志统一见', text)
        self.assertNotIn('hidden-gap', text)
        self.assertNotIn('采集缺口', text)

    def test_single_extracted_file_is_linked_directly_with_encoded_path(self):
        text = render(self.job(), self.manifest([
            {'name': '原始 log (1).txt', 'path': '原始 log (1).txt', 'source_key': 'mention-0-0'}]))
        self.assertIn('[./原始 log (1).txt](' + quote('./原始 log (1).txt') + ')', text)

    def test_see_attachment_points_to_downloaded_media_including_comment_images(self):
        text = render(self.job(), self.manifest([
            {'name': '图片 (1).png', 'path': 'files/image/图片 (1).png', 'source_key': 'images-1'},
            {'name': '重启.mp4', 'path': '重启.mp4', 'source_key': 'attachments-2'}]))
        self.assertIn('[本地视频/图片]', text)
        self.assertNotIn('见附件', text)
        self.assertIn('<a id="local-media"></a>', text)
        self.assertIn('[./files/image/图片 (1).png](' + quote('./files/image/图片 (1).png', safe='/') + ')', text)
        self.assertIn('[./重启.mp4](' + quote('./重启.mp4') + ')', text)
        self.assertEqual(text.count('[./files/image/图片 (1).png]'), 1)
        self.assertNotIn('### 视频与图片', text)

    def test_missing_or_ambiguous_same_named_sources_do_not_get_guessed_links(self):
        job = self.job()
        job['candidates'].append({'id': 'other', 'target_id': 'other-log', 'name': 'logs.zip', 'state': 'failed'})
        text = render(job, self.manifest([
            {'name': 'a.log', 'path': 'unrelated/a.log', 'source_key': 'mention-0-0'}]))
        self.assertNotIn('[logs.zip]', text)
        self.assertNotIn('[见附件]', text)
        self.assertIn('[LOG]\n日志统一见', text)
        self.assertIn('[视频/图片]\n见附件', text)

    def test_shared_artifact_secondary_source_links_without_changing_snapshot(self):
        job = self.job()
        original = job['snapshot']['description']
        text = render(job, self.manifest([
            {'name': 'actual.log', 'path': 'actual.log', 'source_key': 'another',
             'additional_source_keys': ['mention-0-0']}]))
        self.assertIn('[./actual.log](./actual.log)', text)
        self.assertEqual(job['snapshot']['description'], original)

    def test_legacy_dws_child_without_saved_download_list_still_links_original_name(self):
        job = self.job()
        job['candidates'][0].pop('downloaded_files')
        job['candidates'][0]['transport'] = 'dws'
        text = render(job, self.manifest([
            {'name': 'a.log', 'path': 'legacy/a.log', 'source_key': 'mention-0-0'},
            {'name': 'b.log', 'path': 'legacy/b.log', 'source_key': 'mention-0-0'}]))
        self.assertIn('[./legacy/](./legacy/)', text)

    def test_shared_artifact_with_only_secondary_source_metadata_still_links(self):
        job = self.job()
        source = {'kind': 'dws', 'message_id': 'm1', 'group': 'group1', 'path': 'download/logs.zip'}
        job['candidates'][0]['downloaded_files'][0]['source'] = source
        text = render(job, self.manifest([
            {'name': 'a.log', 'path': 'shared/a.log', 'source_key': 'other',
             'source': {'kind': 'different'}, 'additional_sources': [source]},
            {'name': 'b.log', 'path': 'shared/b.log', 'source_key': 'other',
             'source': {'kind': 'different'}, 'additional_sources': [source]}]))
        self.assertIn('[./shared/](./shared/)', text)

    def test_reused_source_key_from_old_snapshot_does_not_link_failed_new_image(self):
        job = self.job(candidates=[{'id': 'images-api-image-1', 'target_id': 'new-image',
            'name': 'new.png', 'state': 'failed',
            'source': {'kind': 'images', 'id': 'api-image-1', 'url': 'https://host/new.png'}}])
        job['snapshot']['description'] = 'new.png'
        for source in [{}, {'kind': 'images', 'id': 'api-image-1', 'url': 'https://host/old.png'}]:
            with self.subTest(source=source):
                text = render(job, self.manifest([
                    {'name': 'old.png', 'path': 'old.png', 'source_key': 'images-api-image-1', 'source': source}]))
                self.assertNotIn('[new.png]', text)

    def test_reused_dws_child_key_only_links_matching_original_message(self):
        job = self.job()
        source = {'kind': 'dws', 'message_id': 'new-message', 'path': 'downloads/logs.zip'}
        job['candidates'][0]['downloaded_files'][0]['source'] = source
        text = render(job, self.manifest([
            {'name': 'a.log', 'path': 'new/a.log', 'source_key': 'mention-0-0', 'source': source},
            {'name': 'b.log', 'path': 'new/b.log', 'source_key': 'mention-0-0', 'source': source},
            {'name': 'old.log', 'path': 'old/old.log', 'source_key': 'mention-0-0',
             'source': {'kind': 'dws', 'message_id': 'old-message', 'path': 'downloads/old.zip'}}]))
        self.assertIn('[./new/](./new/)', text)
        self.assertNotIn('[logs.zip](./)', text)

    def test_partial_log_section_keeps_undownloaded_clues_and_following_sections(self):
        job = self.job()
        job['snapshot']['description'] = '[LOG]\nlogs.zip\nmissing.zip\n[测试人]\n测试员甲'
        job['candidates'].append({'id': 'missing', 'name': 'missing.zip', 'state': 'failed'})
        text = render(job, self.manifest([
            {'name': 'actual.log', 'path': 'actual.log', 'source_key': 'mention-0-0'}]))
        self.assertIn('missing.zip', text)
        self.assertIn('[测试人]\n测试员甲', text)
        self.assertNotIn('[本地日志]', text)

    def test_existing_markdown_links_code_spans_and_fences_remain_verbatim(self):
        job = self.job()
        protected = '[logs.zip](https://host/old.zip)\n`logs.zip`\n```text\nlogs.zip\n```\n'
        job['snapshot']['description'] = protected + 'logs.zip'
        text = render(job, self.manifest([
            {'name': 'actual.log', 'path': 'actual.log', 'source_key': 'mention-0-0'}]))
        self.assertIn(protected + '[logs.zip](actual.log)', text)

    def test_file_names_in_urls_or_longer_names_are_not_rewritten(self):
        job = self.job()
        job['snapshot']['description'] = 'logs.zip\nhttps://example.test/logs.zip\nother-logs.zip'
        text = render(job, self.manifest([
            {'name': 'actual.log', 'path': 'actual.log', 'source_key': 'mention-0-0'}]))
        self.assertIn('[logs.zip](actual.log)', text)
        self.assertIn('https://example.test/logs.zip', text)
        self.assertIn('other-logs.zip', text)
