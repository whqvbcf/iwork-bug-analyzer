"""Original-name evidence storage with a recoverable workspace index."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


def validate_name(name: str) -> str:
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or '/' in name or '\\' in name or any(ord(c) < 32 for c in name)):
        raise ValueError('Invalid original filename')
    return name


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', value):
        raise ValueError('Invalid identifier')
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self._safe(self.root / '.iwork')
        (self.root / '.iwork').mkdir(exist_ok=True)

    def _safe(self, path: Path) -> Path:
        """Reject links even if they currently resolve inside the workspace."""
        path = Path(path).absolute()
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise ValueError('Path is outside workspace') from None
        cursor = self.root
        if cursor.is_symlink():
            raise ValueError('Workspace root cannot be a symlink')
        for part in relative.parts:
            if part in ('.', '..'):
                raise ValueError('Unsafe path')
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError('Symlinks are not allowed in evidence paths')
        return path

    @contextlib.contextmanager
    def _lock(self):
        lock = self._safe(self.root / '.iwork/workspaces.lock')
        with lock.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def bug_dir(self, bug_id: str, source: str = 'iwork', now: datetime | None = None) -> Path:
        if not isinstance(bug_id, str) or not re.fullmatch(r'[0-9]+', bug_id):
            raise ValueError('Bug ID must contain only digits')
        _identifier(source)
        with self._lock():
            # Directory identity is authoritative; indexes are disposable caches.
            matches = []
            index = {}
            for candidate in self.root.glob('bug_*_*'):
                self._safe(candidate)
                metadata = self._safe(candidate / '.iwork/workspace.json')
                try:
                    identity = json.loads(metadata.read_text(encoding='utf-8'))
                    valid = (isinstance(identity, dict) and isinstance(identity.get('bug_id'), str)
                             and re.fullmatch(r'[0-9]+', identity['bug_id'])
                             and isinstance(identity.get('source'), str)
                             and (re.fullmatch(r'bug_[0-9]{6}_' + re.escape(identity['bug_id']), candidate.name)
                                  or re.fullmatch(r'bug_' + re.escape(identity['bug_id']) + r'_[0-9]{6}', candidate.name)))
                except (OSError, ValueError, TypeError):
                    identity, valid = {}, False
                if valid:
                    key = identity['source'] + ':' + identity['bug_id']
                    index.setdefault(key, []).append(candidate.name)
                named_for_bug = (re.fullmatch(r'bug_[0-9]{6}_' + re.escape(bug_id), candidate.name)
                                 or re.fullmatch(r'bug_' + re.escape(bug_id) + r'_[0-9]{6}', candidate.name))
                if valid and identity['bug_id'] == bug_id:
                    if identity['source'] != source:
                        raise ValueError('Existing Bug folder identity is missing, corrupt, or belongs to another source')
                    matches.append(candidate)
                elif named_for_bug and not valid:
                    raise ValueError('Existing Bug folder identity is missing, corrupt, or belongs to another source')
            if len(matches) > 1:
                raise ValueError('Multiple folders exist for this Bug; refusing to merge')
            if matches:
                folder = matches[0]
                legacy = re.fullmatch(r'bug_' + re.escape(bug_id) + r'_([0-9]{6})', folder.name)
                if legacy:
                    destination = self.root / f'bug_{legacy[1]}_{bug_id}'
                    if destination != folder:
                        if destination.exists():
                            raise ValueError('Bug migration destination already exists; refusing to overwrite')
                        folder.rename(destination)
                        folder = destination
            else:
                folder = self.root / f'bug_{(now or datetime.now().astimezone()):%y%m%d}_{bug_id}'
                folder.mkdir()
                atomic_json(folder / '.iwork/workspace.json', {'bug_id': bug_id, 'source': source})
            index[source + ':' + bug_id] = [folder.name]
            atomic_json(self.root / '.iwork/workspaces.json', index)
            return folder

    def staging_dir(self, job_id: str, item_id: str) -> Path:
        path = self._safe(self.root / '.iwork/staging' / _identifier(job_id) / _identifier(item_id))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _destination(self, folder: Path, name: str, source_key: str, equivalent) -> tuple[Path, bool]:
        """Called under lock, choose a stable original-name destination."""
        folder = self._safe(folder)
        if not folder.is_dir() or folder == self.root:
            raise ValueError('Evidence folder is invalid')
        validate_name(name)
        _identifier(source_key)
        choices = [] if name in {'bugreport.md', '.iwork', 'files'} else [folder / name]
        choices.append(folder / 'files' / source_key / name)
        version = 2
        while True:
            if not choices:
                choices.append(folder / 'files' / f'{source_key}-v{version}' / name)
                version += 1
            destination = self._safe(choices.pop(0))
            if not destination.exists():
                return destination, False
            if equivalent(destination):
                return destination, True

    def publish(self, folder: Path, source: Path, original_name: str, source_key: str) -> dict:
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise ValueError('Evidence source must be a regular file')
        digest = sha256_file(source)
        with self._lock():
            destination, reused = self._destination(
                folder, original_name, source_key,
                lambda p: p.is_file() and sha256_file(p) == digest)
            if not reused:
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix='.publish-', dir=destination.parent)
                try:
                    with os.fdopen(fd, 'wb') as output, source.open('rb') as input_file:
                        shutil.copyfileobj(input_file, output, 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    if sha256_file(Path(temporary)) != digest:
                        raise ValueError('Evidence changed during publication')
                    os.replace(temporary, destination)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            return {'path': destination.relative_to(folder).as_posix(), 'name': original_name,
                    'sha256': digest, 'size': destination.stat().st_size, 'reused': reused}
