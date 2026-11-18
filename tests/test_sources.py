import json
import tempfile
import threading
import unittest
import io
from email.message import Message
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request

from iwork_collector.models import normalize_snapshot, redact
from iwork_collector.discovery import discover, ordered_targets
from iwork_collector.downloads import download_http
from iwork_collector.images import inspect_image


class SourceTests(unittest.TestCase):
    def test_untrusted_capture_cannot_read_local_paths(self):
        with self.assertRaises(ValueError):
            normalize_snapshot({'bug_id': '123456', 'attachments': [
                {'name': 'secret', 'local_path': '/etc/passwd'}]})

    def test_requires_real_numeric_identity(self):
        for value in ['BUG-123456', '../123', '', True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_snapshot({'bug_id': value})

    def test_redacts_signed_links_and_sensitive_keys(self):
        result = redact({'description': '日志 https://a.test/log.zip?token=hidden&v=3',
                         'access_token': 'secret', 'url': 'https://a.test/?X-Amz-Signature=abc'})
        text = json.dumps(result)
        self.assertNotIn('hidden', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('abc', text)
        self.assertIn('v=3', text)

    def test_redaction_preserves_spa_locator_and_removes_plain_credentials(self):
        value = redact({'url':'https://iwork.example/#/bug/123456?token=secret',
                        'description':'Authorization: Bearer abcdef\nCookie: sessionid=hidden\n日志说明',
                        'api_key':'private'})
        text = json.dumps(value)
        for secret in ('secret', 'abcdef', 'hidden', 'private'):
            self.assertNotIn(secret, text)
        self.assertIn('/bug/123456', value['url'])

    def test_short_signed_query_keys_are_redacted_without_changing_filenames(self):
        result = redact({'url': 'https://a.test/log?sig=hidden&key=private&v=1',
                         'name': 'Bearer diagnostic.log',
                         'path': 'files/source/Bearer diagnostic.log'})
        self.assertNotIn('hidden', result['url'])
        self.assertNotIn('private', result['url'])
        self.assertEqual(result['name'], 'Bearer diagnostic.log')
        self.assertEqual(result['path'], 'files/source/Bearer diagnostic.log')

    def test_query_only_url_fragments_hide_credentials_and_preserve_other_parameters(self):
        result = redact({'url': 'https://a.test/#access_token=hidden&sig=private&tab=details'})
        self.assertNotIn('hidden', result['url'])
        self.assertNotIn('private', result['url'])
        self.assertIn('tab=details', result['url'])

    def test_same_named_independent_attachments_are_not_conflated(self):
        snapshot = normalize_snapshot({'bug_id': '123456', 'attachments': [
            {'id': 'a', 'name': 'log.txt', 'url': 'https://a.test/a'},
            {'id': 'b', 'name': 'log.txt', 'url': 'https://a.test/b'}]})
        candidates, gaps = discover(snapshot)
        self.assertEqual(len(ordered_targets(candidates)), 2)
        self.assertFalse(gaps)

    def test_priority_is_per_target_and_all_targets_remain(self):
        candidates = [{'id': 'url', 'target_id': 'log', 'priority': 4},
                      {'id': 'group', 'target_id': 'log', 'priority': 1},
                      {'id': 'video', 'target_id': 'video', 'priority': 3}]
        targets = ordered_targets(candidates)
        self.assertEqual([c['id'] for c in targets['log']], ['group', 'url'])
        self.assertIn('video', targets)

    def test_plain_text_group_and_filename_create_explicit_search(self):
        snapshot = normalize_snapshot({'bug_id':'123456',
                    'description':'钉钉群：平台问题群\n日志文件：device_log.zip'})
        candidates, gaps = discover(snapshot)
        mentions = [c for c in candidates if c['priority'] == 1]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0]['name'], 'device_log.zip')
        self.assertEqual(mentions[0]['group']['name'], '平台问题群')

    def test_multiple_groups_are_not_arbitrarily_assigned_to_unscoped_filename(self):
        snapshot = normalize_snapshot({'bug_id':'123456', 'groups':[{'name':'A群'},{'name':'B群'}],
                    'file_mentions':[{'name':'device.zip'}]})
        candidates, gaps = discover(snapshot)
        self.assertFalse([c for c in candidates if c['priority'] == 1])
        self.assertIn('多个', ' '.join(gaps))

    def test_unique_named_group_reference_precedes_matching_attachment(self):
        snapshot = normalize_snapshot({'bug_id':'123456', 'groups':[{'name':'A群'}],
                    'file_mentions':[{'name':'device.zip'}], 'attachments':[
                        {'id':'a', 'name':'device.zip', 'url':'https://example.test/a'}]})
        candidates, gaps = discover(snapshot)
        target = next(c['target_id'] for c in candidates if c['priority'] == 3)
        self.assertEqual([c['priority'] for c in ordered_targets(candidates)[target]], [1, 2, 3])

    def test_bracketed_dingtalk_group_suffix_and_log_filename_are_linked(self):
        snapshot = normalize_snapshot({'bug_id':'10000001', 'description':
            '[视频/日志信息*]：log_E001-10_20260727-020100_20260727170510433.zip\n'
            '日志及视频统一见：【数字座舱统一日志基地】钉钉群'})
        candidates, gaps = discover(snapshot)
        mention = next(c for c in candidates if c['priority'] == 1)
        self.assertEqual(mention['name'], 'log_E001-10_20260727-020100_20260727170510433.zip')
        self.assertEqual(mention['group']['name'], '数字座舱统一日志基地')
        target = mention['target_id']
        self.assertEqual([candidate['priority'] for candidate in ordered_targets(candidates)[target]], [1, 2])

    def test_extensionless_log_identity_is_a_file_clue_even_when_old_capture_called_it_a_group(self):
        name = 'log_E001-10_20260830-012100_20260830153917485'
        snapshot = normalize_snapshot({'bug_id': '10000004', 'description':
            '[缺陷描述*]：\n时间：13：51\n日志群：\n' + name + '\n'
            '日志及视频统一见：【数字座舱统一日志基地】钉钉群',
            'groups': [{'name': name}, {'name': '数字座舱统一日志基地'}],
            'comments': [{'id': 'c1', 'text': 'AND_0057_20260830_135221_1502.log、AND_0058_20260830_140722_1521.log'}]})
        candidates, gaps = discover(snapshot)
        dws = [c for c in candidates if c['transport'] == 'dws']
        self.assertTrue(dws)
        self.assertEqual({c['group']['name'] for c in dws}, {'数字座舱统一日志基地'})
        self.assertEqual({c['name'] for c in dws}, {name,
            'AND_0057_20260830_135221_1502.log', 'AND_0058_20260830_140722_1521.log'})
        self.assertFalse(gaps)

    def test_image_ocr_unavailable_keeps_source_and_reports_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '截图.png'
            path.write_bytes(b'image fixture')
            result = inspect_image(path, Path(temp) / 'derived', executable='missing-iwork-ocr')
            self.assertTrue(path.exists())
            self.assertIn('OCR', ' '.join(result['gaps']))


class DownloadTests(unittest.TestCase):
    def test_cross_host_redirect_does_not_forward_original_archive_credentials(self):
        from iwork_collector.downloads import _EvidenceRedirect
        request = Request('https://original.dingtalk.com/log', headers={
            'Authorization': 'private', 'x-oss-date': 'old-date', 'User-Agent': 'iwork-collector'})
        redirected = _EvidenceRedirect().redirect_request(request, None, 302, 'Found', {},
            'https://another.example.test/log')
        self.assertIsNone(redirected.get_header('Authorization'))
        self.assertIsNone(redirected.get_header('X-oss-date'))
        same = _EvidenceRedirect().redirect_request(request, None, 302, 'Found', {},
            'https://original.dingtalk.com/actual-log')
        self.assertEqual(same.get_header('Authorization'), 'private')

    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'<html>login</html>' if self.path == '/login' else b'raw log bytes\n'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if self.path == '/login' else 'text/plain')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''%E6%97%A5%E5%BF%97.txt")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        try:
            cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except PermissionError:
            cls.server = None
            cls.base = 'http://127.0.0.1:18080'
            def response(request, **kwargs):
                content = b'<html>login</html>' if request.full_url.endswith('/login') else b'raw log bytes\n'
                stream = io.BytesIO(content)
                stream.headers = Message()
                stream.headers['Content-Type'] = 'text/html' if request.full_url.endswith('/login') else 'text/plain'
                stream.headers['Content-Length'] = str(len(content))
                stream.url = request.full_url
                return stream
            cls.patcher = patch('iwork_collector.downloads.urlopen', side_effect=response)
            cls.patcher.start()
            return
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        if cls.server is None:
            cls.patcher.stop()
            return
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_download_preserves_verified_name_and_original_content(self):
        with tempfile.TemporaryDirectory() as temp:
            result = download_http(self.base + '/log', Path(temp), '原始名字.txt')
            self.assertEqual(result['name'], '原始名字.txt')
            self.assertEqual(Path(result['path']).read_bytes(), b'raw log bytes\n')

    def test_http_200_login_html_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
            download_http(self.base + '/login', Path(temp), 'log.zip')

    def test_streaming_size_limit(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
            download_http(self.base + '/log', Path(temp), 'log.txt', max_bytes=3)


if __name__ == '__main__':
    unittest.main()
