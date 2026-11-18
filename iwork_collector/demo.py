"""Offline real-file demonstration of nested archives and report generation."""

import io
import tempfile
import zipfile
from pathlib import Path


def demo(service):
    with tempfile.TemporaryDirectory(prefix='iwork-demo-') as temp:
        temp = Path(temp)
        text = temp / '复现说明.txt'
        text.write_text('操作：启动设备后切换应用。\n现象：短暂黑屏。\n此文件仅为离线演示数据。\n', encoding='utf-8')
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as stream:
            stream.writestr('dmesg.txt', '11-11 10:00:00 display demo event\n')
        archive = temp / '设备日志.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            stream.writestr('system.log', '11-11 10:00:01 app transition demo event\n')
            stream.writestr('kernel.zip', nested.getvalue())
        snapshot = {'schema_version': 1, 'bug_id': '123456', 'title': '离线演示：应用切换黑屏',
                    'url': 'https://iwork.example/bug/123456',
                    'description': '用于验证采集器归档与报告的演示工单，不代表真实线上问题。',
                    'fields': {'平台': 'demo', '复现步骤': '启动设备，切换应用', '期望表现': '连续显示',
                               '实际表现': '短暂黑屏'}, 'comments': [],
                    'attachments': [{'id': 'demo-text', 'name': text.name, 'local_path': str(text), 'transport': 'local'},
                                    {'id': 'demo-archive', 'name': archive.name, 'local_path': str(archive), 'transport': 'local'}],
                    'coverage': {'complete': True, 'gaps': []}}
        job = service.capture(snapshot, trusted=True)
        return service.run(job['job_id'])
