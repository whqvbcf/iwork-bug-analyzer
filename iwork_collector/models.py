"""Versioned snapshots, input validation and credential-safe persisted data."""

import copy
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET = re.compile(r'token|secret|password|authorization|cookie|signature|credential|accesskeyid|x-amz-|x-oss-|security-token|api.?key|session.?id|^(?:auth|sig|key)$', re.I)
URL = re.compile(r'https?://[^\s<>"\[\]，。；）]+')
# iWork hands out a fresh one-time resource token per API call for the same
# document image; it must not change the snapshot identity across re-captures.
ONE_TIME_RESOURCE = re.compile(r'(/api/document/fileRelation/getRelatedFile/[0-9a-fA-F]{32}/)[0-9a-fA-F]{32}')
FILE_FIELDS = {'name', 'fileName', 'filename', 'original_name', 'path', 'local_path', 'relative_path'}


def safe_url(value):
    try:
        parts = urlsplit(value)
        query = [(k, '[redacted]' if SECRET.search(k) else v)
                 for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        host = parts.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if parts.port:
            host += ':' + str(parts.port)
        fragment = parts.fragment
        if '?' in fragment:
            route, query_text = fragment.split('?', 1)
            fragment = route + '?' + urlencode([(k, '[redacted]' if SECRET.search(k) else v)
                                                for k, v in parse_qsl(query_text, keep_blank_values=True)])
        elif '=' in fragment and not fragment.startswith('/'):
            fragment = urlencode([(k, '[redacted]' if SECRET.search(k) else v)
                                   for k, v in parse_qsl(fragment, keep_blank_values=True)])
        return urlunsplit((parts.scheme, host, parts.path, urlencode(query), fragment))
    except ValueError:
        return '[invalid URL]'


def redact(value):
    if isinstance(value, dict):
        return {k: '[redacted]' if SECRET.search(k) else
                v if k in FILE_FIELDS and isinstance(v, str) and not v.startswith(('http://', 'https://'))
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = URL.sub(lambda m: safe_url(m.group()), value)
        value = re.sub(r'(?im)\b(Authorization|Cookie|Set-Cookie)\s*:\s*[^\r\n]+', r'\1: [redacted]', value)
        value = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [redacted]', value)
        return value
    return value


def normalize_snapshot(value, trusted=False):
    if not isinstance(value, dict):
        raise ValueError('工单快照必须是 JSON 对象')
    bug_id = value.get('bug_id')
    if isinstance(bug_id, bool) or not re.fullmatch(r'[0-9]{1,32}', str(bug_id or '')):
        raise ValueError('需要页面明确提供的数字 Bug ID')
    if value.get('schema_version', 1) != 1:
        raise ValueError('不支持的快照版本')
    result = copy.deepcopy(value)
    result.update(schema_version=1, bug_id=str(bug_id))
    for key in ('title', 'description', 'url'):
        result.setdefault(key, '')
        if not isinstance(result[key], str):
            raise ValueError(key + ' 必须是字符串')
    for key in ('comments', 'timeline', 'attachments', 'images', 'groups', 'file_mentions'):
        result.setdefault(key, [])
        if not isinstance(result[key], list) or any(not isinstance(x, dict) for x in result[key]):
            raise ValueError(key + ' 必须是对象数组')
    result.setdefault('fields', {})
    if not isinstance(result['fields'], dict):
        raise ValueError('fields 必须是对象')
    result.setdefault('captured_at', datetime.now().astimezone().isoformat())
    result.setdefault('coverage', {'complete': False, 'gaps': ['未提供页面采集完整性']})
    if not isinstance(result['coverage'], dict):
        raise ValueError('coverage 必须是对象')
    result['coverage'].setdefault('gaps', [])
    if not isinstance(result['coverage']['gaps'], list):
        raise ValueError('coverage.gaps 必须是数组')
    for key in ('attachments', 'images'):
        for item in result[key]:
            if not trusted and (item.get('local_path') or item.get('transport') == 'local'):
                raise ValueError('浏览器不能提交本地文件路径')
            if item.get('url'):
                parsed = urlsplit(item['url'])
                if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username:
                    raise ValueError('仅支持无嵌入凭证的 HTTP(S) 资源地址')
    return result


def fingerprint(snapshot):
    value = redact({k: v for k, v in snapshot.items() if k not in ('captured_at', 'request_id', 'page_tab_id')})
    text = ONE_TIME_RESOURCE.sub(r'\1[one-time-resource]', json.dumps(value, sort_keys=True, ensure_ascii=False))
    return hashlib.sha256(text.encode()).hexdigest()
