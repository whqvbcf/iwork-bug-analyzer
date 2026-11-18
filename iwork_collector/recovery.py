"""Reuse a locally registered, verified archive tree without allocating its data twice."""
from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
from pathlib import Path

from .archives import _same_tree, check_disk_space
from .storage import Workspace, sha256_file, validate_name


def _relative_path(value):
    if not isinstance(value, str) or not value or value.startswith('/'):
        raise ValueError('恢复清单路径无效')
    parts = value.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('恢复清单路径无效')
    for part in parts:
        validate_name(part)
    return Path(*parts)


def _safe_cache(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('恢复来源路径无效')
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('恢复来源不接受符号链接')
    return path


def recover_archive(workspace: Workspace, folder: Path, original_name: str,
                    digest: str, source_key: str, bug_id: str, catalog: Path) -> dict | None:
    """The catalog is local configuration, never supplied by ticket content.

    Build a tree of hard links, verify every linked file, then atomically publish.
    Both the cache and incoming archive remain available if the worker stops.
    """
    catalog = Path(catalog)
    if not catalog.exists():
        return None
    _safe_cache(catalog)
    document = json.loads(catalog.read_text(encoding='utf-8'))
    if document.get('schema_version') != 1 or not isinstance(document.get('entries'), list):
        raise ValueError('恢复目录清单格式无效')
    matches = [entry for entry in document['entries'] if entry.get('bug_id') == bug_id
               and entry.get('archive_name') == original_name and entry.get('archive_sha256') == digest]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('恢复来源不唯一，未自动选择')
    entry = matches[0]
    cache = _safe_cache(entry['folder'])
    metadata = _safe_cache(cache / '.iwork/manifest.json')
    if sha256_file(metadata) != entry.get('manifest_sha256'):
        raise ValueError('恢复清单校验失败，保留原文件')
    manifest = json.loads(metadata.read_text(encoding='utf-8'))
    archives = manifest.get('archives', [])
    top = [record for record in archives if not record.get('parent')]
    if (manifest.get('bug_id') != bug_id or len(top) != 1
            or top[0].get('name') != original_name or top[0].get('sha256') != digest
            or not all(record.get('deleted') for record in archives)):
        raise ValueError('恢复归档身份或完成状态校验失败')
    tree = _relative_path(entry['tree'])
    cache_tree = _safe_cache(cache / tree)
    expected = {}
    for artifact in manifest.get('artifacts', []):
        path = _relative_path(artifact['path'])
        if not path.is_relative_to(tree) or path == tree:
            raise ValueError('恢复材料目录校验失败')
        relative = path.relative_to(tree)
        if relative in expected:
            raise ValueError('恢复清单有重复文件')
        expected[relative] = artifact
    if not expected:
        raise ValueError('恢复清单没有文件证据')
    actual = set()
    for path in cache_tree.rglob('*'):
        _safe_cache(path)
        mode = path.stat().st_mode
        if stat.S_ISREG(mode):
            actual.add(path.relative_to(cache_tree))
        elif not stat.S_ISDIR(mode):
            raise ValueError('恢复来源含特殊文件')
    if actual != set(expected):
        raise ValueError('恢复目录文件清单校验失败')
    folder = workspace._safe(folder)
    staging = workspace._safe(workspace.root / '.iwork/staging')
    staging.mkdir(parents=True, exist_ok=True)
    check_disk_space(staging)
    with tempfile.TemporaryDirectory(prefix='verified-recovery-', dir=staging) as temporary:
        output = Path(temporary) / tree.name
        output.mkdir()
        for relative, artifact in expected.items():
            source = _safe_cache(cache_tree / relative)
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, target, follow_symlinks=False)
            except OSError as error:
                raise ValueError('恢复文件无法共享存储，未复制大文件：' + str(error)) from error
            if (not stat.S_ISREG(target.lstat().st_mode) or target.stat().st_size != artifact.get('size')
                    or sha256_file(target) != artifact.get('sha256')):
                raise ValueError('恢复文件大小或 SHA-256 校验失败')
        if sha256_file(metadata) != entry['manifest_sha256']:
            raise ValueError('恢复清单在处理期间改变')
        with workspace._lock():
            destination, reused = workspace._destination(folder, tree.name, source_key,
                lambda existing: _same_tree(output, existing))
            destination.parent.mkdir(parents=True, exist_ok=True)
            artifacts = [dict(artifact, path=(destination / relative).relative_to(folder).as_posix(),
                              reused=reused, recovered=True)
                         for relative, artifact in expected.items()]
            if not reused:
                os.replace(output, destination)
    return {'artifacts': artifacts, 'archives': copy.deepcopy(archives), 'gaps': []}
