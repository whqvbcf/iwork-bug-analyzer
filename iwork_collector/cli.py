"""CLI entry points for collection, native host installation and offline verification."""

import argparse
import json
import sys
import time
from pathlib import Path

from .demo import demo
from .install import EXTENSION_ID, install_native
from .models import redact
from .native import serve
from .service import Service


def main(argv=None):
    parser = argparse.ArgumentParser(description='iWork 工单证据采集与报告')
    parser.add_argument('--root', default=str(Path.home() / 'iwork_space'))
    parser.add_argument('--downloads-root', help='浏览器实际下载目录，默认 ~/Downloads')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    commands.add_parser('demo')
    collect = commands.add_parser('collect')
    collect.add_argument('snapshot', type=Path)
    collect.add_argument('--allow-local', action='store_true', help='信任本机快照中的本地文件引用')
    for name in ('status', 'resume', 'cancel'):
        command = commands.add_parser(name)
        command.add_argument('job_id')
    worker = commands.add_parser('worker')
    worker.add_argument('--wait-lock', action='store_true')
    worker.add_argument('job_id')
    commands.add_parser('list')
    commands.add_parser('daemon')
    native = commands.add_parser('native')
    native.add_argument('origin', nargs='?')
    native.add_argument('--extension-id', default=EXTENSION_ID)
    installer = commands.add_parser('install-native')
    installer.add_argument('--output', type=Path, help='仅准备注册文件，不写入浏览器配置')
    installer.add_argument('--extension-id', default=EXTENSION_ID)
    args = parser.parse_args(argv)
    service = None
    try:
        if args.command == 'install-native':
            result = install_native(args.root, args.output, args.extension_id, args.downloads_root)
        else:
            service = Service(args.root, downloads_root=args.downloads_root)
            if args.command == 'doctor':
                result = service.dispatch('ping', {})
                result.update(extension_id=EXTENSION_ID,
                              setup=['加载 extension/ 未打包扩展', '执行 install-native 完成用户级注册',
                                     '在真实工单验证选择器和附件下载'])
            elif args.command == 'demo':
                result = demo(service)
            elif args.command == 'collect':
                summary = service.capture(json.loads(args.snapshot.read_text()), trusted=args.allow_local,
                                          retry_failed=True)
                result = service.run(summary['job_id'])
            elif args.command == 'status':
                result = service.dispatch('status', {'job_id': args.job_id})
            elif args.command == 'resume':
                summary = service.resume(args.job_id)
                result = service.run(summary['job_id'])
            elif args.command == 'cancel':
                result = service.cancel(args.job_id)
            elif args.command == 'list':
                result = service.dispatch('list_jobs', {})
            elif args.command == 'worker':
                service.run(args.job_id, wait_for_lock=args.wait_lock)
                return 0
            elif args.command == 'native':
                if args.origin and args.origin != 'chrome-extension://' + args.extension_id + '/':
                    raise ValueError('未注册的扩展来源')
                serve(service)
                return 0
            elif args.command == 'daemon':
                while True:
                    for job in service.state.jobs():
                        if job['status'] in ('captured', 'processing', 'waiting_browser'):
                            service.run(job['job_id'])
                    time.sleep(2)
            else:
                raise ValueError('未支持的操作')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'error': str(redact(str(exc)))}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if service:
            service.close()
