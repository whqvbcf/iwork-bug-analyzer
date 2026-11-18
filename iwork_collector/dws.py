"""Bounded, read-only DingTalk/DWS evidence retrieval.

Resolve the exact group, list its drive, and search its chat messages without
importing the reference project. Chat attachments are re-read and downloaded
through their original message; drive-list entries retain their drive context.
Every command and response is retained in a redacted ledger.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import platform
import unicodedata
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from .models import SECRET, redact

_FILE_ID_KEYS = ('fileId', 'file_id', 'nodeId', 'node_id', 'id')
_RESOURCE_ID_KEYS = ('resourceId', 'resource_id')
_NAME_KEYS = ('name', 'fileName', 'filename', 'file_name', 'title')
_MATERIAL_RE = re.compile(
    r'\.(?:zip|7z|rar|tar(?:\.gz)?|tgz|gz|log|txt|trace|perfetto-trace|mp4|png|jpe?g)(?:\.\d{3})?$', re.I)
_ARCHIVE_RE = re.compile(r'\.(?:tar\.gz|zip|7z|rar|tar|tgz|gz)$', re.I)


class DwsCommandError(ValueError):
    def __init__(self, message, process):
        super().__init__(message)
        self.diagnostic = {'returncode': process.returncode,
            'stdout': _diagnostic_output(process.stdout),
            'stderr': _diagnostic_output(process.stderr)}


def _resolve_executable(value):
    requested = str(value or 'dws').strip() or 'dws'
    if '/' in requested:
        return requested
    found = shutil.which(requested)
    if found:
        return found
    if requested != 'dws':
        return requested
    bundled = Path(__file__).resolve().parent.parent / 'vendor/dws/dws'
    candidates = ([bundled] if requested == 'dws' else []) + [
        Path.home() / '.local/bin/dws', Path.home() / '.npm-global/bin/dws',
        Path('/usr/local/bin/dws'), Path('/opt/dingtalk/bin/dws')]
    for candidate in candidates:
        if candidate == bundled and (platform.system() != 'Linux' or platform.machine().lower() not in {'x86_64', 'amd64'}):
            continue
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return requested


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _json_body(text):
    if isinstance(text, (dict, list)):
        return text
    text = str(text or '')
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _diagnostic_output(text):
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')
    text = str(text or '')
    try:
        return redact(json.loads(text))
    except ValueError:
        pass
    lines = []
    for line in str(text or '').splitlines():
        try:
            lines.append(redact(json.loads(line)))
        except ValueError:
            # Verbose output can mix JSON log lines, prefixed lines and a
            # pretty-printed error. Redact quoted secret fields in fragments.
            line = re.sub(r'("([^"\\]+)"\s*:\s*)"(?:\\.|[^"\\])*"',
                lambda match: match.group(1) + '"[redacted]"' if SECRET.search(match.group(2)) else match.group(0), line)
            lines.append(redact(line))
    body = _json_body(text)
    if body is not None:
        # Keep surrounding CLI context as well as structured trace/error fields.
        return {'lines': lines, 'json': redact(body)}
    return lines if len(lines) > 1 else lines[0] if lines else ''


def _timeout_diagnostic(exc):
    return {'timeout_seconds': exc.timeout, 'stdout': _diagnostic_output(exc.output),
            'stderr': _diagnostic_output(exc.stderr)}


def _result(value):
    if isinstance(value, dict) and isinstance(value.get('result'), dict):
        return value['result']
    return value if isinstance(value, dict) else {}


def _norm(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip().casefold()


def _name(item):
    if not isinstance(item, dict):
        return ''
    for field in _NAME_KEYS:
        if item.get(field):
            return str(item[field]).strip()
    return ''


def _file_id(item):
    if not isinstance(item, dict):
        return ''
    for field in _FILE_ID_KEYS:
        value = item.get(field)
        if value not in (None, '') and not isinstance(value, (dict, list)):
            return str(value).strip()
    resource_kind = _norm(item.get('resourceIdType') or item.get('resource_id_type') or item.get('resourceType') or item.get('type'))
    if resource_kind in {'fileid', 'file'}:
        for field in _RESOURCE_ID_KEYS:
            value = item.get(field)
            if value not in (None, '') and not isinstance(value, (dict, list)):
                return str(value).strip()
    return ''


def _is_folder(item):
    kind = str(item.get('type') or item.get('kind') or '').upper() if isinstance(item, dict) else ''
    return bool(isinstance(item, dict) and (item.get('isFolder') is True or kind in {'FOLDER', 'DIRECTORY'}))


def _safe_name(value):
    value = str(value or '').strip()
    if not value or value in {'.', '..'} or '/' in value or '\\' in value or any(ord(c) < 32 for c in value):
        return ''
    return value


def _same_material(actual, wanted):
    if _norm(actual) == _norm(wanted):
        return True
    # A literal extensionless log identifier can resolve to the server's full
    # archive name; arbitrary prefixes and other date/sequence variants cannot.
    return bool(re.fullmatch(r'log_[A-Za-z0-9][A-Za-z0-9._-]*', str(wanted), re.I)
                and not _MATERIAL_RE.search(str(wanted)) and _ARCHIVE_RE.search(str(actual))
                and _norm(_ARCHIVE_RE.sub('', str(actual))) == _norm(wanted))


def _chat_window(wanted, start, end):
    """Explicit filenames are identities; their upload can be delayed past incident day."""
    match = re.search(r'(?<!\d)(20\d{6})[-_](\d{6})(?!\d)', str(wanted or ''))
    if not match:
        return start, end
    try:
        zone = datetime.fromisoformat(str(start).replace('Z', '+00:00')).tzinfo
        anchor = datetime.strptime(match.group(1) + match.group(2), '%Y%m%d%H%M%S').replace(tzinfo=zone)
        lower = anchor - timedelta(days=7)
        current = datetime.now(zone) + timedelta(days=2)
        supplied_end = datetime.fromisoformat(str(end).replace('Z', '+00:00'))
        return lower.isoformat(), max(supplied_end, current).isoformat()
    except (TypeError, ValueError):
        return start, end


def _extract_file_refs(value):
    refs = []
    for item in _walk(value):
        if not isinstance(item, dict):
            continue
        name = _name(item)
        file_id = _file_id(item)
        if file_id and name and (_MATERIAL_RE.search(name) or re.fullmatch(r'log_[A-Za-z0-9][A-Za-z0-9._-]*', name, re.I)
                                 or item.get('fileSize') or item.get('mimeType')):
            refs.append((name, file_id, item))
    return refs


class DwsClient:
    def __init__(self, executable='dws', profile=None, runner=None, verbose=False):
        self.executable = _resolve_executable(executable)
        self.profile = profile  # kept for compatibility with older local dws builds.
        self.runner = runner
        self.verbose = verbose
        self.progress = None
        self._process_diagnostic = None
        self.last_ledger = []
        self.log_links = []

    def available(self):
        return bool(self.runner or shutil.which(self.executable) or Path(self.executable).is_file())

    def _run(self, args, cwd, timeout=120):
        self._process_diagnostic = None
        args = list(args)
        if not any(x in args for x in ('-f', '--format')):
            args += ['-f', 'json']
        if self.profile and '--profile' not in args:
            args += ['--profile', self.profile]
        command = [self.executable, *args]
        if self.runner:
            return self.runner(command, cwd)
        try:
            proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise ValueError('DWS 不可用：找不到 dws 可执行文件，请把 dws 加入 PATH') from exc
        if proc.stderr:
            self._process_diagnostic = {'returncode': proc.returncode,
                'stderr': _diagnostic_output(proc.stderr)}
        if proc.returncode:
            raw_error = (proc.stderr or proc.stdout or '').strip()
            if re.search(r'未登录|not_authenticated|dws\s+auth\s+login|token.*(?:expired|illegal)', raw_error, re.I):
                raise DwsCommandError('DWS 登录已过期或未完成授权，请运行 dws auth login 后重试', proc)
            detail = _json_body(proc.stderr) or _json_body(proc.stdout)
            if isinstance(detail, dict):
                error = detail.get('error') if isinstance(detail.get('error'), dict) else detail
                message = error.get('message') or error.get('reason') or 'DWS 调用失败'
                diagnostic = {key: error[key] for key in ('reason', 'server_error_code', 'trace_id', 'retryable', 'hint') if key in error}
                if diagnostic:
                    message += '；' + json.dumps(redact(diagnostic), ensure_ascii=False)
            else:
                safe = _diagnostic_output(proc.stderr or proc.stdout or 'DWS 调用失败')
                message = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False)
                message = message[-500:]
            raise DwsCommandError(f'DWS 调用失败：{message}', proc)
        body = _json_body(proc.stdout)
        if body is None:
            raise ValueError('DWS 未返回可识别的结构化 JSON')
        return body

    def _call(self, args, cwd, timeout=120):
        args = list(args)
        if self.progress:
            downloading = ('--download-resources' in args or '+messages-resource-download' in args
                           or args[:2] == ['drive', 'download'])
            self.progress(phase='downloading' if downloading else 'searching')
        if self.profile and '--profile' not in args:
            args += ['--profile', self.profile]
        if self.verbose and '--verbose' not in args:
            args += ['--verbose']
        command = [self.executable, *args]
        try:
            value = self._run(args, cwd, timeout)
            item = {'command': command, 'response': redact(value)}
            if self._process_diagnostic:
                item['diagnostic'] = self._process_diagnostic
            self.last_ledger.append(item)
            return value
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            item = {'command': command, 'error': str(redact(str(exc)))}
            if isinstance(exc, DwsCommandError):
                item['diagnostic'] = exc.diagnostic
            elif isinstance(exc, subprocess.TimeoutExpired):
                item['diagnostic'] = _timeout_diagnostic(exc)
            self.last_ledger.append(item)
            raise

    def _resolve_group(self, group, cwd):
        name = str(group.get('name') or '').strip()
        if not name:
            return {'name': '', 'open_conversation_id': '', 'space_id': ''}
        data = self._call(['chat', 'search', '--query', name, '--limit', '5', '-f', 'json'], cwd)
        if any(x.get('outcome') in {'ambiguous', 'ambiguous_target', 'resolution_required'}
               or x.get('status') in {'ambiguous', 'resolution_required'} for x in _walk(data)):
            raise ValueError('DWS 群或目标存在歧义；需明确会话 ID 后重试')
        groups = _result(data).get('groups') or []
        exact = next((item for item in groups if _norm(item.get('title')) == _norm(name)), None)
        if not exact:
            if 'profiles' in _result(data) or any('isOrgCurrent' in item for item in _walk(data)):
                raise ValueError('DWS profile 无唯一当前组织；请指定 dws_profile')
            raise ValueError('DWS 未找到同名钉钉群：' + name)
        extension = exact.get('extension') if isinstance(exact.get('extension'), dict) else {}
        return {'name': name,
                'open_conversation_id': str(exact.get('openConversationId') or ''),
                'space_id': str(extension.get('newCSpaceIdIM') or exact.get('spaceId') or '')}

    def _list_drive(self, space_id, parent_id, cwd, max_pages=4):
        if not space_id:
            return [], True
        items, cursor, complete = [], '', True
        for _ in range(max_pages):
            args = ['drive', 'list', '--space-id', space_id, '--limit', '50', '-f', 'json']
            if parent_id:
                args += ['--parent-id', parent_id]
            if cursor:
                args += ['--cursor', cursor]
            data = self._call(args, cwd)
            result = _result(data)
            page = result.get('items') if isinstance(result.get('items'), list) else []
            items.extend(page)
            cursor = str(result.get('nextToken') or result.get('next_token') or '')
            if not cursor:
                break
        else:
            complete = False
        return items, complete and not bool(cursor)

    def _drive_find(self, group, wanted, cwd):
        queue = deque([('', '')])
        seen_nodes, matches, entries = set(), [], 0
        complete = True
        while queue and entries < 200:
            parent_id, parent_path = queue.popleft()
            items, page_complete = self._list_drive(group['space_id'], parent_id, cwd)
            complete = complete and page_complete
            entries += len(items)
            for item in items:
                node = _file_id(item)
                if node and node in seen_nodes:
                    continue
                if node:
                    seen_nodes.add(node)
                name = _name(item)
                path = '/'.join(x for x in (parent_path, name) if x)
                if name and _same_material(name, wanted) and node and not _is_folder(item):
                    matches.append({'name': name, 'file_id': node, 'path': path,
                                    'file_size': item.get('fileSize') or item.get('size'),
                                    'source_kind': 'drive'})
                if _is_folder(item) and node and path.count('/') < 4:
                    queue.append((node, path))
            if entries >= 200 and queue:
                complete = False
        return matches, complete and not queue

    def _chat_find(self, group, wanted, start, end, cwd):
        if not group['open_conversation_id']:
            return []
        start, end = _chat_window(wanted, start, end)
        matches = []
        queries = [wanted]
        if re.search(r'\.(?:mp4|mov|mkv|avi)$', wanted, re.I):
            # Video indexes can tokenize the extension separately. Search the
            # literal stem too, while resource acceptance stays exact-name only.
            queries.append(wanted.rsplit('.', 1)[0])
        date_match = re.search(r'(20\d{6}-\d{6})', wanted)
        upload_match = re.search(r'(?<!\d)(\d{13,})(?=\.)', wanted)
        if date_match:
            prefix = wanted[:date_match.start()].rstrip('_-')
            if prefix:
                queries.append(prefix + ' ' + date_match.group(1))
            queries.append(date_match.group(1))
        if upload_match:
            queries.append(upload_match.group(1))
        for query in dict.fromkeys(queries):
            args = ['chat', 'message', 'search', '--query', query,
                    '--group', group['open_conversation_id'], '--start', start, '--end', end,
                    '--limit', '50', '-f', 'json']
            data = self._call(args, cwd)
            result = _result(data)
            conversations = result.get('conversationMessagesList') or result.get('conversation_messages_list') or []
            for conversation in conversations:
                if not isinstance(conversation, dict):
                    continue
                title = str(conversation.get('title') or group['name'])
                for message in conversation.get('messages') or []:
                    if not isinstance(message, dict):
                        continue
                    content = message.get('content') or ''
                    content_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                    if _norm(wanted) in _norm(content_text):
                        for link in re.findall(r'https?://fawivi-prod-k8-vehiclelogweb\.faw\.cn/\?id=\d+', content_text):
                            if not any(item['url'] == link for item in self.log_links):
                                self.log_links.append({'url': link,
                                    'message_id': message.get('openMessageId') or message.get('messageId'),
                                    'group': group['name'], 'original_name': wanted})
                    refs = _extract_file_refs(message)
                    for textual in re.finditer(r'([^\s]+)\s+fileId\s*:\s*([A-Za-z0-9_-]+)', content_text, re.I):
                        if _same_material(textual.group(1), wanted):
                            refs.append((textual.group(1), textual.group(2), {}))
                    for name, node, ref in refs:
                        if not _same_material(name, wanted):
                            continue
                        matches.append({'name': name if _MATERIAL_RE.search(name) else wanted,
                                        'file_id': node, 'path': str(ref.get('path') or f'{title}/{name}'),
                                        'file_size': ref.get('fileSize') or ref.get('size'),
                                        'source_kind': 'chat-message',
                                        'reload_message_resource': bool(message.get('resourceRefs')),
                                        'message_id': message.get('openMessageId') or message.get('open_message_id'),
                                        'open_conversation_id': message.get('openConversationId') or message.get('open_conversation_id') or conversation.get('openConversationId') or group.get('open_conversation_id'),
                                        'message_text': content_text,
                                        'created_at': message.get('createTime') or message.get('create_time')})
            if matches:
                break
        unique = {}
        for match in matches:
            unique.setdefault(match['file_id'], match)
        return list(unique.values())

    def _download(self, match, group, directory):
        match = dict(match)
        name = _safe_name(match.get('name'))
        if not name:
            raise ValueError('DWS 返回了不安全或缺少原始文件名')
        node = str(match.get('file_id') or '')
        if not node:
            raise ValueError('钉钉消息命中但没有可下载的 fileId')
        attempt = directory / ('.dws-attempt-' + re.sub(r'[^A-Za-z0-9_.-]', '_', node))
        shutil.rmtree(attempt, ignore_errors=True)
        attempt.mkdir(parents=True, exist_ok=True)
        def publish(source=None):
            source = self._download_path(source, directory, attempt)
            expected = match.get('file_size')
            if source.stat().st_size <= 0 or (expected and int(expected) != source.stat().st_size):
                raise ValueError('DWS 原文件大小校验失败，未发布不完整下载')
            original_name = _safe_name(match.get('name'))
            if not original_name:
                raise ValueError('DWS 当前原消息附件没有有效原始文件名')
            target = directory / original_name
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)
            return target

        try:
            # Re-read the exact message before choosing its typed transfer route.
            if match.get('source_kind') == 'chat-message' or match.get('reload_message_resource'):
                return publish(self._download_message_resource(match, group, directory, attempt))
            return publish(self._download_drive(node, group.get('space_id'), name, directory, attempt))
        finally:
            shutil.rmtree(attempt, ignore_errors=True)

    def _download_path(self, value, directory, attempt):
        if not isinstance(value, (str, Path)) or not str(value):
            raise ValueError('DWS 下载完成但没有返回可核验文件路径')
        source = Path(value)
        if '..' in source.parts:
            raise ValueError('DWS 下载路径包含上级目录')
        if not source.is_absolute():
            source = directory / source
        if not source.resolve().is_relative_to(attempt):
            raise ValueError('DWS 下载路径超出本次暂存目录')
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents) or not source.is_file():
            raise ValueError('DWS 下载结果不是可信普通文件')
        return source

    def _download_drive(self, node, space_id, name, directory, attempt):
        args = ['drive', 'download', '--node', node]
        if space_id:
            args += ['--space-id', space_id]
        args += ['--output', str(attempt), '-f', 'json', '--parallel', '8', '--timeout', '600']
        raw = self._call(args, directory, timeout=630)
        body = _result(raw)
        if raw.get('success') is False or body.get('success') is False:
            raise ValueError('DWS 文件下载回执报告失败')
        if 'nodeId' in body and str(body['nodeId']) != str(node):
            raise ValueError('DWS 文件下载回执与原资源不一致')
        explicit = body.get('savedPath') or raw.get('savedPath') or raw.get('output') or body.get('output') or body.get('path')
        if explicit:
            source = self._download_path(explicit, directory, attempt)
        else:
            files = [path for path in attempt.rglob('*') if path.is_file() and _same_material(path.name, name)]
            if len(files) != 1:
                raise ValueError('DWS 下载未取得唯一对应原始文件：' + name)
            source = self._download_path(files[0], directory, attempt)
        if 'sizeBytes' in body:
            size = body['sizeBytes']
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size != source.stat().st_size:
                raise ValueError('DWS 文件大小与下载回执不一致')
        return source

    def _message_resource(self, raw, match, group):
        message_id = str(match.get('message_id') or '')
        if not message_id:
            raise ValueError('群消息文件缺少原消息 ID，未将搜索摘要的 fileId 当作钉盘节点')
        body = _result(raw)
        messages = body.get('messages', [])
        current = [message for message in messages if isinstance(message, dict) and
            str(message.get('messageId') or message.get('openMessageId') or message.get('id') or '') == message_id]
        if len(current) != 1:
            raise ValueError('DWS 未重新取得唯一原消息：' + json.dumps(redact(body.get('failures', [])), ensure_ascii=False))
        conversation = (current[0].get('conversationId') or current[0].get('openConversationId') or '')
        expected_conversation = match.get('open_conversation_id') or group.get('open_conversation_id')
        if conversation and expected_conversation and conversation != expected_conversation:
            raise ValueError('DWS 原消息会话与目标日志群不一致')
        refs = {}
        resource_refs = current[0].get('resourceRefs')
        # DWS resourceRefs includes quoted/forwarded resources with their own
        # message context; raw nested message bodies are a compatibility fallback.
        for item in resource_refs if isinstance(resource_refs, list) and resource_refs else _walk(current[0]):
            if not isinstance(item, dict):
                continue
            if not _same_material(_name(item), match['name']):
                continue
            kind = str(item.get('resourceIdType') or item.get('resourceType') or item.get('type') or '').lower()
            download = item.get('download')
            arguments = download.get('arguments') if isinstance(download, dict) else None
            if download is not None and (not isinstance(arguments, dict) or
                    download.get('ready') is False or download.get('missing')):
                raise ValueError('DWS 附件下载参数尚未完整，未猜测资源上下文')
            arguments = arguments if isinstance(arguments, dict) else {}
            owner = ((arguments.get('message-id'), arguments.get('open-conversation-id')) if download is not None
                     else (message_id, conversation or expected_conversation))
            if (kind == 'mediaid' or item.get('mediaId')) and any(not isinstance(value, str) or not value for value in owner):
                raise ValueError('DWS 媒体附件缺少资源所属消息或会话 ID')
            resource_id = item.get('resourceId')
            if resource_id and kind in {'fileid', 'mediaid'}:
                refs[(kind, str(resource_id), owner if kind == 'mediaid' else None)] = item
            for field in ('fileId', 'mediaId'):
                if item.get(field):
                    refs[(field.lower(), str(item[field]), owner if field == 'mediaId' else None)] = item
        if len(refs) != 1:
            raise ValueError('DWS 原消息未确定唯一同名附件资源，未自动选择')
        (kind, resource_id, owner), ref = next(iter(refs.items()))
        if ((match.get('expected_resource_id') and match['expected_resource_id'] != resource_id)
                or (match.get('expected_resource_type') and match['expected_resource_type'] != kind)):
            raise ValueError('DWS 原消息资源与本次固定的类型或 ID 不一致，未替换目标')
        download = ref.get('download')
        arguments = download.get('arguments') if isinstance(download, dict) else None
        if isinstance(arguments, dict) and (str(arguments.get('type') or '').lower() != kind or
                str(arguments.get('resource-id') or '') != resource_id):
            raise ValueError('DWS 附件下载参数与原消息资源不一致')
        match['name'] = _name(ref)
        if ref.get('fileSize') or ref.get('size'):
            match['file_size'] = ref.get('fileSize') or ref.get('size')
        return kind, resource_id, owner, current[0]

    def _query_resource(self, raw, match, group):
        body = _result(raw)
        if (body.get('complete') is False or body.get('messagesComplete') is False
                or body.get('partial') is True or body.get('failures')):
            raise ValueError('DWS 原消息查询未完整成功：' + json.dumps(redact(body.get('failures', [])), ensure_ascii=False))
        kind, resource_id, owner, message = self._message_resource(raw, match, group)
        if len(body.get('messages', [])) != 1:
            raise ValueError('DWS 返回了原消息之外的消息，未扩大下载范围')
        refs = message.get('resourceRefs')
        if not isinstance(refs, list) or not refs:
            raise ValueError('DWS 原消息缺少可枚举的资源引用，未扩大下载范围')
        if not (message.get('conversationId') or message.get('openConversationId')):
            raise ValueError('DWS 原消息缺少可核验的会话 ID')
        if any(not _same_material(_name(ref), match['name']) for ref in refs):
            raise ValueError('原消息还包含其他资源，未扩大下载范围')
        return kind, resource_id, owner, message

    def _download_message_resource(self, match, group, directory, attempt):
        message_id = str(match.get('message_id') or '')
        if not message_id:
            raise ValueError('群消息文件缺少原消息 ID，未将搜索摘要的 fileId 当作钉盘节点')
        args = ['chat', '+messages-mget', '--msg-ids', message_id,
            '--no-reactions', '--no-threads', '-f', 'json']
        raw = self._call(args, directory)
        kind, resource_id, owner, message = self._message_resource(raw, match, group)
        if kind == 'fileid':
            # Query downloads all resources in this exact message. Refuse a
            # message with other attachments rather than widening the download.
            self._query_resource(raw, match, group)
            raw = self._call(args + ['--download-resources', '--output-dir', attempt.name + '/',
                '--timeout', '600'], directory, timeout=630)
            fresh_kind, fresh_id, _, _ = self._query_resource(raw, match, group)
            if (fresh_kind, fresh_id) != (kind, resource_id):
                raise ValueError('DWS 查询下载时原消息资源发生变化，未混用资源版本')
            ledger = _result(raw).get('resourceDownloads')
            if not isinstance(ledger, dict):
                raise ValueError('DWS 查询未返回逐项资源下载账本')
            downloads, failures = ledger.get('downloads'), ledger.get('failures')
            if (ledger.get('ok') is not True or ledger.get('partial') is True or ledger.get('failedCount', 0) != 0
                    or ledger.get('downloadedCount', 1) != 1 or failures
                    or not isinstance(downloads, list) or len(downloads) != 1):
                raise ValueError('DWS 原消息资源下载未成功：' + json.dumps(redact(ledger), ensure_ascii=False))
            item = downloads[0]
            # v1.0.63 fileId ledger may omit messageId; the resource must then
            # match the unique reference in the same exact-message response.
            if ((str(item.get('resourceType') or '').lower(), str(item.get('resourceId') or '')) != (kind, resource_id)
                    or item.get('messageId') not in (None, '', message_id)):
                raise ValueError('DWS 查询下载账本与原消息资源不一致')
            value = item.get('localPath')
            if not isinstance(value, str) or Path(value).is_absolute():
                raise ValueError('DWS 消息附件未返回约定的相对路径')
            source = self._download_path(value, directory, attempt)
            size = item.get('sizeBytes')
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size != source.stat().st_size:
                raise ValueError('DWS 消息附件大小与逐项下载账本不一致')
            return source
        message_id, conversation = owner
        if not isinstance(message_id, str) or not message_id or not isinstance(conversation, str) or not conversation:
            raise ValueError('DWS 媒体附件缺少资源所属消息或会话 ID')
        raw = self._call(['chat', '+messages-resource-download', '--type', 'mediaId',
            '--resource-id', resource_id, '--message-id', message_id,
            '--open-conversation-id', conversation, '--output', attempt.name + '/',
            '--timeout', '600', '-f', 'json'], directory, timeout=630)
        item = _result(raw)
        if (str(item.get('resourceType') or '').lower(), str(item.get('resourceId') or '')) != (kind, resource_id) or item.get('messageId') != message_id:
            raise ValueError('DWS 媒体下载账本与原消息资源不一致')
        value = item.get('localPath')
        if isinstance(value, str) and Path(value).is_absolute():
            raise ValueError('DWS 媒体附件未返回约定的相对路径')
        source = self._download_path(value, directory, attempt)
        size = item.get('sizeBytes')
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size != source.stat().st_size:
            raise ValueError('DWS 媒体附件大小与下载账本不一致')
        return source

    def _legacy_search(self, group, name, start, end, directory, query=None):
        """Compatibility path for older local dws builds exposing +search-msg."""
        if not self.profile:
            raw = self._call(['profile', 'list', '-f', 'json'], directory)
            profiles = [x for x in _walk(raw) if x.get('isOrgCurrent') is True]
            if len(profiles) != 1:
                raise ValueError('DWS profile 无唯一当前组织；请指定 dws_profile')
            item = profiles[0]
            self.profile = item.get('profileId') or item.get('id') or ((item.get('corpId') or '') + ':' + (item.get('userId') or ''))
        args = ['chat', '+search-msg', '--profile', self.profile,
                '--' + ('group' if group.get('conversation_id') else 'chat-query'),
                group.get('conversation_id') or group.get('name'), '--start', start, '--end', end,
                '--page-all', '--page-limit', '5', '--download-resources', '--output-dir', './resources']
        if name or query:
            args += ['--query', name or query]
        raw = self._call(args, directory)
        nodes = list(_walk(raw))
        if any(x.get('outcome') in {'ambiguous', 'ambiguous_target', 'resolution_required'}
               or x.get('status') in {'ambiguous', 'resolution_required'} for x in nodes):
            raise ValueError('DWS 群或目标存在歧义；需明确会话 ID 后重试')
        found = []
        for item in nodes:
            path = item.get('localPath') or item.get('local_path') or item.get('outputPath') or item.get('path')
            if not isinstance(path, str):
                continue
            local = Path(path)
            if not local.is_absolute():
                local = directory / local
            local = local.resolve()
            if not local.is_relative_to(directory) or not local.is_file():
                continue
            original = item.get('fileName') or item.get('filename') or item.get('name') or local.name
            if name and original != name:
                continue
            found.append({'name': original, 'local_path': str(local), 'transport': 'local',
                          'source': {'kind': 'dws', 'message_id': item.get('messageId') or item.get('message_id'),
                                     'group': group}})
        return found, bool(found)

    def search_files(self, group, name, start, end, directory, query=None, prefer_chat=False):
        self.log_links = []
        result = {'candidates': [], 'gaps': [], 'coverage': {'complete': False}, 'ledger': [],
                  'log_links': self.log_links}
        if not self.available():
            result['gaps'].append('DWS 不可用：安装 dws 并登录后，重试该群的文件采集')
            return result
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.last_ledger = []
        try:
            if isinstance(group, str):
                group = {'name': group}
            if not group or not (group.get('conversation_id') or group.get('name')):
                raise ValueError('DWS 需要明确群名或真实会话 ID，未执行跨群扫描')
            if not name and not query:
                raise ValueError('群文件检索需要明确文件名或工单编号主题，未执行全群文件下载')
            resolved = {'name': str(group.get('name') or ''),
                        'open_conversation_id': str(group.get('open_conversation_id') or group.get('openConversationId') or group.get('conversation_id') or ''),
                        'space_id': str(group.get('space_id') or group.get('spaceId') or '')}
            if group.get('name') and not group.get('conversation_id') and not group.get('open_conversation_id'):
                discovered = self._resolve_group(group, directory)
                resolved.update({key: value or resolved[key] for key, value in discovered.items()})
            result['coverage'].update(group=group, start=start, end=end,
                space_id=resolved.get('space_id', ''),
                open_conversation_id=resolved.get('open_conversation_id', ''))
            wanted = str(name or query or '').strip()
            drive_errors = []
            matches, drive_complete = [], True
            if prefer_chat and resolved['open_conversation_id']:
                try:
                    matches = self._chat_find(resolved, wanted, start, end, directory) if resolved['open_conversation_id'] else []
                except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    matches = []
                    drive_errors.append('DWS 群消息检索失败：' + str(exc))
            if not matches and resolved['space_id']:
                try:
                    matches, drive_complete = self._drive_find(resolved, wanted, directory)
                except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    matches, drive_complete = [], False
                    drive_errors.append('DWS drive 检索失败，已继续尝试群消息：' + str(exc))
            if not matches and not prefer_chat:
                try:
                    matches = self._chat_find(resolved, wanted, start, end, directory) if resolved['open_conversation_id'] else []
                except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    matches = []
                    drive_errors.append('DWS 群消息检索失败：' + str(exc))
            result['gaps'].extend(drive_errors)
            if not matches and self.last_ledger:
                last_response = self.last_ledger[-1].get('response', {})
                if any(isinstance(x, dict) and ('downloads' in x or 'resourceDownloads' in x) for x in _walk(last_response)):
                    legacy, complete = self._legacy_search(group, name, start, end, directory, query)
                    if legacy:
                        if name and len(legacy) > 1:
                            result['ambiguous_candidates'] = legacy
                            result['gaps'].append('DWS 返回多个同名文件；保留 ledger，需按消息来源消歧后重试')
                            return self._finish(result)
                        result['candidates'] = legacy
                        result['coverage'] = {'complete': complete, 'group': group, 'start': start, 'end': end}
                        return self._finish(result)
            if any(x.get('outcome') in {'ambiguous', 'ambiguous_target', 'resolution_required'}
                   or x.get('status') in {'ambiguous', 'resolution_required'} for x in _walk(self.last_ledger)):
                raise ValueError('DWS 群或目标存在歧义；需明确会话 ID 后重试')
            unique = {}
            for match in matches:
                unique.setdefault(match.get('file_id') or match.get('path'), match)
            matches = list(unique.values())
            result['coverage']['matched_files'] = [{field: match.get(field, '') for field in
                ('name', 'file_id', 'message_id', 'source_kind', 'created_at')} for match in matches]
            if name and len(matches) > 1:
                result['ambiguous_candidates'] = matches
                result['gaps'].append('DWS 返回多个同名文件；保留 ledger，需按消息来源消歧后重试')
            elif matches:
                match = matches[0]
                result['coverage']['download_status'] = 'pending'
                local = self._download(match, resolved, directory)
                result['coverage']['download_status'] = 'complete'
                result['candidates'] = [{'name': local.name, 'local_path': str(local), 'transport': 'local',
                                         'source': {'kind': 'dws', 'message_id': match.get('message_id'),
                                                    'group': group, 'path': match.get('path', '')}}]
            result['coverage']['complete'] = bool(drive_complete and not result.get('gaps'))
            if not result['coverage']['complete']:
                result['gaps'].append('DWS 检索覆盖未证实完整；保留分页和下载 ledger')
            if not result['candidates'] and not result.get('ambiguous_candidates'):
                result['gaps'].append('DWS 本次未返回可核验的匹配下载文件；消息摘要不能代替文件')
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            if result['coverage'].get('download_status') == 'pending':
                result['coverage'].update(download_status='failed', download_error=str(redact(str(exc))))
            result['gaps'].append(str(exc) if isinstance(exc, ValueError) else 'DWS 调用失败或超时')
        return self._finish(result)

    def _finish(self, result):
        result['ledger'] = redact(self.last_ledger)
        return result
