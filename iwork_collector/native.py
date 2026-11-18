"""Chromium Native Messaging framing and a stateless durable-service bridge."""

import json
import struct
import sys

MAX_MESSAGE = 1024 * 1024


def _read(stream, length):
    chunks = []
    while length:
        chunk = stream.read(length)
        if not chunk:
            raise ValueError('Native Messaging 消息被截断')
        chunks.append(chunk)
        length -= len(chunk)
    return b''.join(chunks)


def read_message(stream):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError('Native Messaging 消息头不完整')
    length = struct.unpack('=I', header)[0]
    if length > MAX_MESSAGE:
        raise ValueError('快照超过 1 MiB，请缩小页面采集范围')
    return json.loads(_read(stream, length))


def write_message(stream, value):
    encoded = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_MESSAGE:
        raise ValueError('Native Messaging 响应超过 1 MiB')
    stream.write(struct.pack('=I', len(encoded)) + encoded)
    stream.flush()


def serve(service, source=None, output=None):
    source, output = source or sys.stdin.buffer, output or sys.stdout.buffer
    while True:
        request = read_message(source)
        if request is None:
            return
        try:
            if not isinstance(request, dict) or not isinstance(request.get('params', {}), dict):
                raise ValueError('请求必须包含对象类型的 params')
            method, params = request.get('method'), request.get('params', {})
            result = service.dispatch(method, params)
            if method in ('capture', 'resume'):
                service.kick(result['job_id'])
            elif method == 'browser_result' and params.get('status') in ('complete', 'failed'):
                service.kick(result['job_id'])
            response = {'id': request.get('id'), 'ok': True, 'result': result}
        except (ValueError, KeyError, OSError, TypeError) as exc:
            from .models import redact
            response = {'id': request.get('id') if isinstance(request, dict) else None,
                        'ok': False, 'error': {'code': 'collector_error', 'message': str(redact(str(exc)))}}
        write_message(output, response)
