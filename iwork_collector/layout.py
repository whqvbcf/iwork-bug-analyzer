"""Compact verified legacy single-file wrappers without copying log contents."""

import os
from pathlib import Path

from .archives import _stem
from .storage import sha256_file


def moved_path(path, moves):
    seen = set()
    while path in moves:
        if path in seen:
            raise ValueError('证据路径迁移记录存在循环')
        seen.add(path)
        path = moves[path]
    return path


def update_paths(job, moves):
    for item in [*job.get('artifacts', []), *job.get('images', [])]:
        if item.get('path'):
            item['path'] = moved_path(item['path'], moves)
    for candidate in job.get('candidates', []):
        source = candidate.get('source') or {}
        if source.get('image'):
            source['image'] = moved_path(source['image'], moves)


def legacy_wrappers(archives):
    """Resolve actual archive member paths, never infer ownership from basenames."""
    def output_path(archive, seen):
        if id(archive) in seen:
            return None
        if not archive.get('parent'):
            return Path(_stem(archive['name']))
        parents = [p for p in archives if p['name'] == archive['parent']]
        if len(parents) != 1:
            return None
        parent = parents[0]
        container = output_path(parent, seen | {id(archive)})
        members = [Path(m) for m in parent.get('members', []) if Path(m).name == archive['name']]
        if container is None or len(members) != 1:
            return None
        return container / members[0].parent / _stem(archive['name'])

    # Legacy records name archives by basename. If a proposed output is also
    # an actual member of a different archive, its ownership is ambiguous.
    original_owners = {}
    for archive in archives:
        container = output_path(archive, set())
        if container is not None:
            for member in archive.get('members', []):
                original_owners.setdefault((container / member).as_posix(), set()).add(id(archive))
    wrappers = set()
    for archive in archives:
        members = archive.get('members', [])
        if (archive.get('layout_version', 1) >= 2 or not archive.get('deleted') or len(members) != 1 or '/' in members[0]
                or '\\' in members[0]):
            continue
        output = output_path(archive, set())
        if output is not None:
            proposed = (output / members[0]).as_posix()
            if not (original_owners.get(proposed, set()) - {id(archive)}):
                wrappers.add(proposed)
    return wrappers


def compact_single_files(workspace, job):
    folder = Path(job['folder'])
    allowed = legacy_wrappers(job.get('archives', []))
    moves = job.setdefault('path_moves', {})
    cleanup = job.setdefault('layout_cleanup', {})
    for artifact in job.get('artifacts', []):
        source = workspace._safe(folder / artifact['path'])
        if artifact['path'] not in allowed or source.parent == folder:
            continue
        if not source.is_file() or list(source.parent.iterdir()) != [source]:
            continue
        destination = workspace._safe(source.parent.parent / source.name)
        if sha256_file(source) != artifact['sha256']:
            continue
        if destination.exists():
            if not destination.is_file() or sha256_file(destination) != artifact['sha256']:
                continue
        else:
            # Keep the old link until manifest AND job state commit. No data copy.
            os.link(source, destination)
        old, new = artifact['path'], destination.relative_to(folder).as_posix()
        moves[old] = new
        cleanup[old] = {'path': new, 'sha256': artifact['sha256']}
        artifact['path'] = new
    update_paths(job, moves)


def cleanup_single_files(workspace, job):
    folder = Path(job['folder'])
    for old, entry in list(job.get('layout_cleanup', {}).items()):
        source = workspace._safe(folder / old)
        destination = workspace._safe(folder / moved_path(entry['path'], job.get('path_moves', {})))
        try:
            if source.is_file():
                if (not destination.is_file() or sha256_file(destination) != entry['sha256']
                        or sha256_file(source) != entry['sha256']):
                    continue
                source.unlink()
            # rmdir only succeeds on empty folders; other user files survive.
            if source.parent.exists():
                source.parent.rmdir()
            del job['layout_cleanup'][old]
        except OSError:
            # The durable job already points to the verified new link. Retry later.
            pass
