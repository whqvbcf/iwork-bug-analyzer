import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from iwork_collector.vehiclelogs import collect_vehicle_logs


class VehicleLogTests(unittest.TestCase):
    reference = {'url': 'https://fawivi-prod-k8-vehiclelogweb.faw.cn/?id=300001',
                 'message_id': 'robot-message'}

    def setUp(self):
        environment = patch.dict(os.environ, {'IWORK_VEHICLELOG_GATEWAY': 'test-gateway-route'})
        environment.start()
        self.addCleanup(environment.stop)

    def metadata(self, **values):
        return {'id': 300001, 'fileName': '/log.zip', 'dingPath': '/log.zip',
                'filePath': 'https://trans.dingtalk.com/log',
                'headers': {'Authorization': 'private-authorization'},
                'childFileList': [], **values}

    def test_original_403_requests_new_authorization_once_and_downloads_with_new_headers(self):
        old = self.metadata(md5=hashlib.md5(b'archive').hexdigest())
        fresh = dict(old, headers={'Authorization': 'fresh-private-auth'})
        requests, downloads = [], []
        def metadata(request, **options):
            requests.append(request)
            return io.BytesIO(json.dumps(old if len(requests) == 1 else fresh).encode())
        def download(url, directory, name='', **options):
            downloads.append(options.get('request_headers'))
            if len(downloads) == 1:
                raise HTTPError(url, 403, 'RequestTimeTooSkewed', {}, None)
            p = Path(directory) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'archive')
            return {'path': str(p), 'name': name, 'size': 7}
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', side_effect=metadata), \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=download):
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(downloads[-1]['Authorization'], 'fresh-private-auth')
        self.assertEqual(requests[-1].get_header('Cache-control'), 'no-cache')
        self.assertEqual(len(requests), 2)
        self.assertNotIn('fresh-private-auth', json.dumps(result))

    def test_expired_member_signature_is_refreshed_before_downloading(self):
        old = self.metadata(filePath='', childFileList=[{'id': 1, 'parent_id': 0, 'type': 'file',
            'name': 'system.log', 'size': 3, 'signURL': '/vehicle-log/md5/system.log?Expires=1&Signature=old'}])
        fresh = dict(old, childFileList=[dict(old['childFileList'][0],
            signURL='/vehicle-log/md5/system.log?Expires=4102444800&Signature=new-private')])
        def download(url, directory, name='', **options):
            self.assertIn('new-private', url)
            p = Path(directory) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'log')
            return {'path': str(p), 'name': name, 'size': 3}
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', side_effect=[
                    io.BytesIO(json.dumps(old).encode()), io.BytesIO(json.dumps(fresh).encode())]) as fetch, \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=download):
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertNotIn('new-private', json.dumps(result))

    def test_unchanged_expired_authorization_is_not_retried_or_reported_as_renewed(self):
        old = self.metadata(childFileList=[{'id': 1, 'parent_id': 0, 'type': 'file',
            'name': 'system.log', 'size': 3, 'signURL': '/vehicle-log/md5/system.log?Expires=1&Signature=old'}])
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', side_effect=lambda *a, **k: io.BytesIO(json.dumps(old).encode())) as fetch, \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=HTTPError(
                    'https://trans.dingtalk.com/log', 403, 'Forbidden', {}, None)) as download:
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertFalse(result['candidates'])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(result['coverage']['authorization_refresh'], 'unchanged')
        self.assertIn('服务端仍返回过期', ' '.join(result['gaps']))

    def test_authorization_refresh_cannot_switch_original_archive_identity(self):
        old = self.metadata(md5='a' * 32)
        fresh = dict(old, md5='b' * 32, headers={'Authorization': 'new-private'})
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', side_effect=[
                    io.BytesIO(json.dumps(old).encode()), io.BytesIO(json.dumps(fresh).encode())]), \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=HTTPError(
                    'https://trans.dingtalk.com/log', 403, 'Forbidden', {}, None)) as download:
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertEqual(download.call_count, 1)
        self.assertFalse(result['candidates'])
        self.assertIn('身份', ' '.join(result['gaps']))

    def test_member_403_refreshes_once_and_retries_the_same_member_with_new_signature(self):
        old = self.metadata(filePath='', childFileList=[{'id': i, 'parent_id': 0, 'type': 'file',
            'name': f'system{i}.log', 'size': 3, 'signURL': f'/vehicle-log/md5/system{i}.log?Signature=old'}
            for i in (1, 2)])
        fresh = dict(old, childFileList=[dict(item,
            signURL=f'/vehicle-log/md5/{item["name"]}?Signature=fresh') for item in old['childFileList']])
        calls = []
        def download(url, directory, name='', **options):
            calls.append(url)
            if len(calls) == 1:
                raise HTTPError(url, 403, 'Forbidden', {}, None)
            self.assertIn('Signature=fresh', url)
            p = Path(directory) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'log')
            return {'path': str(p), 'name': name, 'size': 3}
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', side_effect=[
                    io.BytesIO(json.dumps(old).encode()), io.BytesIO(json.dumps(fresh).encode())]), \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=download):
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertEqual(len(result['candidates']), 2)
        self.assertEqual(len(calls), 3)

    def test_expired_member_refresh_cannot_change_path_or_size(self):
        old = self.metadata(filePath='', childFileList=[{'id': 1, 'parent_id': 0, 'type': 'file',
            'name': 'system.log', 'size': 3, 'signURL': '/vehicle-log/md5/system.log?Expires=1'}])
        fresh = dict(old, childFileList=[dict(old['childFileList'][0], size=9,
            signURL='/vehicle-log/md5/system.log?Expires=4102444800')])
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', side_effect=[
                    io.BytesIO(json.dumps(old).encode()), io.BytesIO(json.dumps(fresh).encode())]), \
                patch('iwork_collector.vehiclelogs.download_http') as download:
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertFalse(result['candidates'])
        download.assert_not_called()
        self.assertIn('身份', ' '.join(result['gaps']))

    def test_mid_download_identity_change_stops_the_rest_of_the_batch(self):
        old = self.metadata(filePath='', childFileList=[{'id': i, 'parent_id': 0, 'type': 'file',
            'name': f'system{i}.log', 'size': 3, 'signURL': f'/vehicle-log/md5/system{i}.log?Signature=old'}
            for i in (1, 2, 3)])
        fresh = dict(old, childFileList=[dict(item, size=9) for item in old['childFileList']])
        calls = []
        def download(url, directory, name='', **options):
            calls.append(url)
            if len(calls) > 1:
                raise HTTPError(url, 403, 'Forbidden', {}, None)
            p = Path(directory) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'log')
            return {'path': str(p), 'name': name, 'size': 3}
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', side_effect=[
                    io.BytesIO(json.dumps(old).encode()), io.BytesIO(json.dumps(fresh).encode())]), \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=download):
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(len(calls), 2)
        self.assertIn('身份', ' '.join(result['gaps']))

    def test_original_archive_is_verified_and_temporary_credentials_are_not_recorded(self):
        data = self.metadata(md5=hashlib.md5(b'archive').hexdigest())
        def download(url, directory, name='', **options):
            self.assertEqual(options['request_headers']['Authorization'], 'private-authorization')
            p = Path(directory) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'archive')
            return {'path': str(p), 'name': name, 'size': 7}
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', return_value=io.BytesIO(json.dumps(data).encode())), \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=download):
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertEqual(result['candidates'][0]['name'], 'log.zip')
        self.assertTrue(result['coverage']['complete'])
        self.assertNotIn('private-authorization', json.dumps(result))

    def test_expired_original_falls_back_to_parsed_files_with_original_names_and_directories(self):
        data = self.metadata(childFileList=[
            {'id': 1, 'parent_id': 0, 'type': 'folder', 'name': 'log'},
            {'id': 2, 'parent_id': 1, 'type': 'folder', 'name': 'android'},
            {'id': 3, 'parent_id': 2, 'type': 'file', 'name': 'logcat', 'size': 3,
             'signURL': '/vehicle-log/md5/logcat?Signature=private-signature'},
            {'id': 4, 'parent_id': 1, 'type': 'folder', 'name': 'qnx'},
            {'id': 5, 'parent_id': 4, 'type': 'file', 'name': 'logcat', 'size': 3,
             'signURL': '/vehicle-log/md5/qnx/logcat?Signature=private-signature'}])
        def download(url, directory, name='', **options):
            if 'trans.dingtalk.com' in url:
                raise ValueError('原包签名已过期')
            self.assertTrue(url.startswith('https://fawivi-gw.faw.cn:63025/test-gateway-route/vehicle-log/'))
            p = Path(directory) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'log')
            return {'path': str(p), 'name': name, 'size': 3}
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', return_value=io.BytesIO(json.dumps(data).encode())), \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=download):
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertEqual([c['name'] for c in result['candidates']], ['logcat', 'logcat'])
        self.assertEqual([c['relative_parent'] for c in result['candidates']], ['log/android', 'log/qnx'])
        self.assertFalse(result['coverage']['complete'])
        self.assertIn('解析', ' '.join(result['gaps']))
        self.assertNotIn('private-signature', json.dumps(result))

    def test_missing_gateway_route_reports_configuration_gap_without_downloading(self):
        data = self.metadata(filePath='', childFileList=[{'id': 1, 'parent_id': 0, 'type': 'file',
            'name': 'system.log', 'size': 3, 'signURL': '/vehicle-log/md5/system.log?Signature=x'}])
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {'IWORK_VEHICLELOG_GATEWAY': ''}), \
                patch('iwork_collector.vehiclelogs.urlopen', return_value=io.BytesIO(json.dumps(data).encode())), \
                patch('iwork_collector.vehiclelogs.download_http') as download:
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertFalse(result['candidates'])
        download.assert_not_called()
        self.assertIn('IWORK_VEHICLELOG_GATEWAY', ' '.join(result['gaps']))

    def test_unrelated_log_record_is_not_downloaded(self):
        data = self.metadata(fileName='/another-log.zip', dingPath='/another-log.zip')
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', return_value=io.BytesIO(json.dumps(data).encode())), \
                patch('iwork_collector.vehiclelogs.download_http') as download:
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertFalse(result['candidates'])
        download.assert_not_called()

    def test_failed_first_transfer_does_not_publish_partial_files_or_signed_urls(self):
        data = self.metadata(childFileList=[
            {'id': 1, 'parent_id': 0, 'type': 'file', 'name': 'system.log', 'size': 3,
             'signURL': '/vehicle-log/md5/system.log?AccessKeyId=private-access&Signature=private-signature'}])
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', return_value=io.BytesIO(json.dumps(data).encode())), \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=ValueError(
                    '下载失败 https://example.test/log?AccessKeyId=private-access&Signature=private-signature')):
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertFalse(result['candidates'])
        self.assertFalse(result['coverage']['complete'])
        self.assertNotIn('private-access', json.dumps(result))
        self.assertNotIn('private-signature', json.dumps(result))

    def test_expired_parsed_signatures_are_reported_without_trying_the_stale_url(self):
        data = self.metadata(childFileList=[{'id': 1, 'parent_id': 0, 'type': 'file',
            'name': 'system.log', 'size': 3, 'signURL': '/vehicle-log/md5/system.log?Expires=1&Signature=hidden'}])
        with tempfile.TemporaryDirectory() as temp, \
                patch('iwork_collector.vehiclelogs.urlopen', side_effect=lambda *a, **k: io.BytesIO(json.dumps(data).encode())), \
                patch('iwork_collector.vehiclelogs.download_http', side_effect=ValueError('原包下载失败')) as download:
            result = collect_vehicle_logs(self.reference, 'log.zip', Path(temp))
        self.assertEqual(download.call_count, 1)
        self.assertIn('签名已过期', ' '.join(result['gaps']))

