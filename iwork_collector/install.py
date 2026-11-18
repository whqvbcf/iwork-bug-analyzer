"""User-space native host registration; supports reviewable preparation output."""

import json
import os
import shlex
import sys
from pathlib import Path

EXTENSION_ID = 'oblbdhfcbmklhaebkonhfdidglfhinac'


def prepare_native(root, output, extension_id=EXTENSION_ID, downloads_root=None):
    import re
    if not re.fullmatch('[a-p]{32}', extension_id):
        raise ValueError('扩展 ID 必须是 32 位 Chromium 扩展标识')
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parent.parent
    wrapper = output / 'iwork-native-host'
    command = [sys.executable, '-m', 'iwork_collector', '--root', str(Path(root).expanduser().resolve())]
    if downloads_root:
        command += ['--downloads-root', str(Path(downloads_root).expanduser().resolve())]
    command += ['native', '--extension-id', extension_id]
    wrapper.write_text('#!/bin/sh\ncd ' + shlex.quote(str(project)) + '\n'
                       '[ -f local.env ] && . ./local.env\nexec ' +
                       ' '.join(shlex.quote(x) for x in command) + ' "$@"\n', encoding='utf-8')
    wrapper.chmod(0o700)
    manifest = output / 'com.iwork.collector.json'
    manifest.write_text(json.dumps({'name': 'com.iwork.collector', 'description': 'iWork 本地证据采集服务',
                                   'path': str(wrapper), 'type': 'stdio',
                                   'allowed_origins': ['chrome-extension://' + extension_id + '/']},
                                  ensure_ascii=False, indent=2), encoding='utf-8')
    return {'manifest': str(manifest), 'wrapper': str(wrapper), 'extension_id': extension_id,
            'extension_directory': str(project / 'extension')}


def install_native(root, output=None, extension_id=EXTENSION_ID, downloads_root=None):
    if output:
        return prepare_native(root, output, extension_id, downloads_root)
    config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    prepared = prepare_native(root, config / 'iwork-collector', extension_id, downloads_root)
    registrations = []
    for browser in ('google-chrome', 'chromium', 'microsoft-edge'):
        directory = config / browser / 'NativeMessagingHosts'
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / 'com.iwork.collector.json'
        destination.write_text(Path(prepared['manifest']).read_text(), encoding='utf-8')
        registrations.append(str(destination))
    return dict(prepared, registrations=registrations)
