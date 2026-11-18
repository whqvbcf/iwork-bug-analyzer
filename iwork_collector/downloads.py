"""Bounded HTTP downloads; browser owns authenticated iWork requests."""

from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class _EvidenceRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        next_request = super().redirect_request(request, fp, code, message, headers, newurl)
        old, new = urlsplit(request.full_url), urlsplit(newurl)
        if old.scheme == 'https' and new.scheme != 'https':
            raise ValueError('不允许将授权下载跳转到 HTTP')
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            for key, _ in next_request.header_items():
                if key.lower() in ('authorization', 'cookie') or key.lower().startswith('x-oss-'):
                    next_request.remove_header(key)
        return next_request


def _name(value):
    if not value or value in ('.', '..') or '/' in value or '\\' in value or '\0' in value:
        raise ValueError('无效原始文件名')
    return value


def download_http(url, directory, name='', max_bytes=2 * 1024 ** 3, timeout=30, request_headers=None):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username:
        raise ValueError('仅支持 HTTP(S) 下载地址')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / '.download-part'
    try:
        open_request = build_opener(_EvidenceRedirect()).open if request_headers else urlopen
        with open_request(Request(url, headers={'User-Agent': 'iwork-collector/0.1', **(request_headers or {})}), timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type in ('text/html', 'application/xhtml+xml', 'application/json', 'application/xml', 'text/xml'):
                raise ValueError('链接返回网页或错误响应，需要浏览器下载或专用适配器')
            length = response.headers.get('Content-Length')
            if length and int(length) > max_bytes:
                raise ValueError('下载超过大小预算')
            message = Message()
            message['Content-Disposition'] = response.headers.get('Content-Disposition', '')
            original = _name(name or message.get_filename() or unquote(Path(urlsplit(response.url).path).name) or 'unnamed-download')
            total, prefix = 0, b''
            with partial.open('xb') as stream:
                while True:
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise ValueError('下载超过大小预算')
                    if len(prefix) < 512:
                        prefix += block[:512 - len(prefix)]
                    stream.write(block)
            if length and total != int(length):
                raise ValueError('下载长度与响应不符')
            if prefix.lstrip().lower().startswith((b'<!doctype html', b'<html', b'<?xml', b'<error')):
                raise ValueError('下载内容实际为登录或错误页面')
            destination = directory / original
            if destination.exists():
                raise ValueError('下载暂存目标已存在')
            partial.rename(destination)
            return {'path': str(destination), 'name': original, 'size': total, 'content_type': content_type}
    except Exception:
        partial.unlink(missing_ok=True)
        raise
