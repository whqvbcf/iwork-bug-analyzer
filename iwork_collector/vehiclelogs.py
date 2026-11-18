"""Fetch the exact log record linked by the DingTalk parsing notification.

The API and gateway routes are the ones used by the vehicle-log web application.
Original archives and parsed members are distinct evidence, with explicit coverage.
Signed URLs/headers are consumed in memory and never included in returned records.
"""

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from .downloads import download_http
from .models import redact
from .storage import validate_name

PORTAL_HOST = 'fawivi-prod-k8-vehiclelogweb.faw.cn'
API_ROOT = 'https://vehicle-log-service.faw.cn/vehicle-log-file/'
GATEWAY_HOST = 'https://fawivi-gw.faw.cn:63025'


def _gateway():
    # The gateway route segment is an access credential, kept out of the source tree.
    route = os.environ.get('IWORK_VEHICLELOG_GATEWAY', '').strip().strip('/')
    if not route:
        raise ValueError('未配置车端日志下载网关路径（环境变量 IWORK_VEHICLELOG_GATEWAY）')
    return GATEWAY_HOST + '/' + route


class _RecordChanged(ValueError):
    """A renewed grant must still refer to the same evidence."""


def _paths(items):
    nodes = {str(item['id']): item for item in items if isinstance(item, dict) and 'id' in item}
    if len(nodes) != len(items):
        raise ValueError('日志平台文件树含缺失或重复 ID')

    def path(item):
        parts, seen = [], set()
        while item:
            identity = str(item['id'])
            if identity in seen or len(seen) >= 32:
                raise ValueError('日志平台文件树包含循环或层级过深')
            seen.add(identity)
            parts.append(validate_name(item.get('name')))
            parent = str(item.get('parent_id') or '0')
            if parent != '0' and parent not in nodes:
                raise ValueError('日志平台文件树缺少父目录')
            item = nodes.get(parent)
            if item and item.get('type') != 'folder':
                raise ValueError('日志平台文件的父节点不是目录')
        return '/'.join(reversed(parts))

    return [(item, path(item)) for item in items if item.get('type') == 'file']


def _load_record(endpoint, record_id, wanted, refresh=False):
    request = Request(endpoint, headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}) if refresh else endpoint
    with urlopen(request, timeout=30) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('日志平台文件列表超过读取预算')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('日志平台未返回记录对象')
    names = [str(data.get(k) or '').rsplit('/', 1)[-1] for k in ('fileName', 'dingPath')]
    if wanted not in names or str(data.get('id')) != record_id:
        raise _RecordChanged('日志平台记录与目标日志名或 ID 身份不一致')
    return data


def _expiry(item):
    values = parse_qs(urlsplit(str(item.get('signURL') or '')).query).get('Expires', [])
    if values:
        if len(values) != 1 or not values[0].isdigit():
            raise ValueError('日志平台文件签名过期时间无效')
        try:
            return datetime.fromtimestamp(int(values[0]), timezone.utc)
        except (ValueError, OverflowError, OSError) as error:
            raise ValueError('日志平台文件签名过期时间无效') from error
    return None


def _expired(item):
    expiry = _expiry(item)
    return expiry is not None and expiry <= datetime.now(timezone.utc)


def _authorization(data):
    return (data.get('filePath'), data.get('headers'),
            [item.get('signURL') for item in data.get('childFileList') or []])


def _members(data, max_bytes):
    members = _paths(data.get('childFileList') or [])
    if not members or len(members) > 2000:
        raise ValueError('日志平台没有解析文件或文件数超过 2000 项预算')
    sizes = [int(item.get('size') or 0) for item, _ in members]
    if any(size < 0 for size in sizes) or sum(sizes) > max_bytes:
        raise ValueError('日志平台解析文件大小无效或合计超过下载预算')
    return members


def collect_vehicle_logs(reference, wanted, directory, max_bytes=2 * 1024 ** 3):
    result = {'candidates': [], 'gaps': [], 'ledger': [], 'coverage': {'complete': False}}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        link = urlsplit(reference.get('url', ''))
        ids = parse_qs(link.query).get('id', [])
        if link.scheme not in ('https', 'http') or link.hostname != PORTAL_HOST or link.username or len(ids) != 1 or not ids[0].isdigit():
            raise ValueError('不是明确的车端日志平台记录地址')
        record_id = ids[0]
        endpoint = API_ROOT + record_id
        data = _load_record(endpoint, record_id, wanted)
        refreshed = False
        refreshed_members = {}

        def refresh():
            nonlocal data, refreshed, refreshed_members
            if refreshed:
                return False
            refreshed = True
            fresh = _load_record(endpoint, record_id, wanted, refresh=True)
            if data.get('md5') and str(fresh.get('md5') or '').lower() != str(data['md5']).lower():
                raise _RecordChanged('日志平台续签前后原包 MD5 身份不一致，停止下载')
            if data.get('childFileList'):
                before = {(path, int(item.get('size') or 0)) for item, path in _members(data, max_bytes)}
                after = {(path, int(item.get('size') or 0)) for item, path in _members(fresh, max_bytes)}
                if before != after:
                    raise _RecordChanged('日志平台续签前后文件树身份不一致，停止混合下载')
            changed = _authorization(fresh) != _authorization(data)
            data = fresh
            if fresh.get('childFileList'):
                refreshed_members = {path: item for item, path in _members(fresh, max_bytes)}
            status = 'changed' if changed else 'unchanged'
            result['coverage']['authorization_refresh'] = status
            result['ledger'].append({'phase': 'authorization-refresh', 'status': status,
                                     'record_id': record_id, 'attempts': 1})
            return changed
        source = {'kind': 'vehicle-log-platform', 'record_id': record_id,
                  'url': reference['url'], 'message_id': reference.get('message_id'), 'original_name': wanted}
        result['coverage'].update(record_id=record_id, original_name=wanted)
        result['ledger'].append({'endpoint': endpoint, 'record_id': record_id,
                                 'name': wanted, 'parsed_files': len(data.get('childFileList') or [])})
        original = directory / 'original'
        for attempt in range(2):
            archive_url = str(data.get('filePath') or '')
            parsed = urlsplit(archive_url)
            try:
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or not (
                        parsed.hostname == 'dingtalk.com' or parsed.hostname.endswith('.dingtalk.com')):
                    raise ValueError('日志平台没有提供可用的原包地址')
                raw_headers = data.get('headers') or {}
                headers = {k: str(v) for k, v in raw_headers.items()
                           if k.lower() in ('authorization', 'x-oss-date', 'x-oss-security-token')}
                downloaded = download_http(archive_url, original, wanted, max_bytes=max_bytes,
                                           timeout=60, request_headers=headers)
                expected = str(data.get('md5') or '').lower()
                if expected:
                    digest = hashlib.md5()
                    with Path(downloaded['path']).open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            digest.update(block)
                    if digest.hexdigest() != expected:
                        raise ValueError('日志原包 MD5 与平台记录不符')
                result['candidates'].append({'name': wanted, 'local_path': downloaded['path'],
                                              'transport': 'local', 'source': source})
                result['coverage'].update(complete=True, mode='original-archive')
                result['ledger'].append({'phase': 'original-archive', 'status': 'complete'})
                return result
            except (OSError, ValueError) as error:
                shutil.rmtree(original, ignore_errors=True)
                result['ledger'].append({'phase': 'original-archive', 'status': 'failed', 'error': str(redact(str(error)))})
                if attempt == 0 and isinstance(error, HTTPError) and error.code in (401, 403):
                    previous = (data.get('filePath'), data.get('headers'))
                    if refresh() and previous != (data.get('filePath'), data.get('headers')):
                        continue
                break
        members = _members(data, max_bytes)
        if any(_expired(item) for item, _ in members if int(item.get('size') or 0)):
            refresh()
            members = _members(data, max_bytes)
        failures = []
        for index, (item, relative) in enumerate(members):
            try:
                item = refreshed_members.get(relative, item)
                size = int(item.get('size') or 0)
                if size < 0:
                    raise ValueError('日志平台文件大小无效')
                staging = directory / 'parsed' / str(index)
                staging.mkdir(parents=True, exist_ok=True)
                name = item['name']
                if size == 0:
                    local = staging / name
                    local.touch(exist_ok=False)
                else:
                    sign_url = str(item.get('signURL') or '')
                    if not sign_url.startswith('/vehicle-log/') or sign_url.startswith('//'):
                        raise ValueError('日志平台没有提供该文件的有效下载地址')
                    if _expired(item):
                        result['coverage']['authorization_expired'] = True
                        raise ValueError('日志平台文件下载签名已过期：' + _expiry(item).isoformat()
                                         + '；已自动重新获取授权，服务端仍返回过期签名，需要平台服务端重新签发')
                    try:
                        downloaded = download_http(_gateway() + sign_url, staging, name,
                                                   max_bytes=min(size, max_bytes), timeout=60)
                    except HTTPError as error:
                        if error.code not in (401, 403) or refreshed:
                            raise
                        if not refresh():
                            raise ValueError('日志平台拒绝下载；自动重取授权后服务端仍返回相同授权') from error
                        current = refreshed_members.get(relative)
                        renewed = str(current.get('signURL') or '') if current else ''
                        if not renewed.startswith('/vehicle-log/') or renewed == sign_url or _expired(current):
                            raise ValueError('日志平台未重新签发该文件的有效下载授权') from error
                        downloaded = download_http(_gateway() + renewed, staging, name,
                                                   max_bytes=min(size, max_bytes), timeout=60)
                    local = Path(downloaded['path'])
                    if local.stat().st_size != size:
                        local.unlink(missing_ok=True)
                        raise ValueError('日志平台文件大小与列表不符')
                result['candidates'].append({'name': name, 'local_path': str(local), 'transport': 'local',
                    'relative_parent': str(Path(relative).parent) if '/' in relative else '',
                    'source': dict(source, mode='parsed-member', member=relative)})
            except (ValueError, OSError) as error:
                failures.append(relative)
                result['ledger'].append({'phase': 'parsed-member', 'name': relative, 'status': 'failed',
                                          'error': str(redact(str(error)))})
                # A failed gateway/DNS/auth affects the rest of the same batch.
                if not result['candidates'] or isinstance(error, (HTTPError, _RecordChanged)):
                    break
        result['coverage'].update(mode='parsed-members', total_files=len(members),
                                  downloaded=len(result['candidates']), failures=failures)
        result['gaps'].append(f'原始压缩包下载失败；已取得日志平台解析文件 {len(result["candidates"])}/{len(members)} 项，不能视为原包完整副本')
        if failures:
            result['gaps'].append('解析文件下载失败：' + '; '.join(entry['error'] for entry in result['ledger']
                                   if entry.get('phase') == 'parsed-member' and entry.get('status') == 'failed'))
    except (ValueError, OSError, KeyError, TypeError) as error:
        result['gaps'].append('车端日志平台获取失败：' + str(redact(str(error))))
    return result
