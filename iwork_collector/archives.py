"""Bounded, transactional archive extraction without executing archive members."""
from __future__ import annotations

import contextlib
import gzip
import os
import re
import selectors
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

from .storage import Workspace, _identifier, sha256_file, validate_name


class ArchiveError(ValueError):
    pass


def check_disk_space(path, required=0):
    if shutil.disk_usage(path).free < required + 1024 ** 3:
        raise ArchiveError('磁盘空间不足，处理需保留至少 1 GiB 可用空间')


_SUFFIXES = ('.tar.gz', '.tar.bz2', '.tar.xz', '.tar.zst', '.tgz', '.tbz2', '.txz',
             '.zip', '.tar', '.gz', '.7z', '.rar')


def _stem(name: str) -> str:
    lower = name.lower()
    for suffix in _SUFFIXES:
        if lower.endswith(suffix):
            return validate_name(name[:-len(suffix)])
    return name


def _kind(path: Path, name: str) -> str | None:
    with path.open('rb') as stream:
        header = stream.read(512)
    if header.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')):
        return 'zip'
    if header.startswith(b'\x1f\x8b'):
        return 'gzip'
    if header.startswith(b'7z\xbc\xaf\x27\x1c'):
        return '7z'
    if header.startswith(b'Rar!'):
        return 'rar'
    # tarfile also validates older TAR headers that have no ustar marker.
    if tarfile.is_tarfile(path):
        return 'tar'
    lower = name.lower()
    if lower.endswith(('.7z', '.rar', '.tar.zst')):
        return 'unsupported'
    if lower.endswith(_SUFFIXES):
        raise ArchiveError('Archive signature or header is corrupt')
    return None


class _Budget:
    def __init__(self, limits, progress=None):
        values = {'max_expanded_size': 64 * 1024 ** 3, 'max_files': 20000,
                  'max_nesting': 6, 'max_seconds': 1800}
        if limits:
            unknown = set(limits) - set(values)
            if unknown:
                raise ValueError('Unknown archive limits: ' + ', '.join(sorted(unknown)))
            values.update(limits)
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError('Archive limits must be positive numbers')
        self.values = values
        self.bytes = 0
        self.files = 0
        self.started = time.monotonic()
        self.disk_root = None
        self.disk_checked_at = None
        self.progress = progress
        self.reported_at = None
        self.completed_files = 0
        self.current_file = ''
        self.phase = 'extracting'

    def notify(self, force=False):
        now = time.monotonic()
        if self.progress and (force or self.reported_at is None or now - self.reported_at >= 1):
            self.reported_at = now
            self.progress({'phase': self.phase, 'expanded_bytes': self.bytes,
                           'files_done': self.completed_files, 'current_file': self.current_file})

    def check_disk(self, size=0):
        if self.disk_root is None:
            return
        now = time.monotonic()
        if size == 0 and self.disk_checked_at is not None and now - self.disk_checked_at < 1:
            return
        self.disk_checked_at = now
        check_disk_space(self.disk_root, size)

    def check(self):
        if time.monotonic() - self.started > self.values['max_seconds']:
            raise ArchiveError('Archive processing time budget exceeded')
        self.check_disk()

    def member(self, size=0):
        self.check()
        self.files += 1
        if self.files > self.values['max_files']:
            raise ArchiveError('Archive member count budget exceeded')
        if size > self.values['max_expanded_size'] - self.bytes:
            raise ArchiveError('Archive expanded size budget exceeded')
        self.check_disk(size)

    def copy(self, input_file, destination: Path):
        self.current_file = destination.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as output:
            while True:
                self.check()
                chunk = input_file.read(min(1024 * 1024, int(self.values['max_expanded_size'] - self.bytes) + 1))
                if not chunk:
                    break
                self.bytes += len(chunk)
                if self.bytes > self.values['max_expanded_size']:
                    raise ArchiveError('Archive expanded size budget exceeded')
                output.write(chunk)
                self.notify()
        self.completed_files += 1


def _member_path(name: str, root: Path) -> Path:
    if not isinstance(name, str) or not name or '\\' in name or name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        raise ArchiveError('Unsafe archive member path')
    parts = name.rstrip('/').split('/')
    if not parts or any(part in ('', '..') for part in parts):
        raise ArchiveError('Unsafe archive member path')
    parts = [part for part in parts if part != '.']
    for part in parts:
        validate_name(part)
    return root.joinpath(*parts)


def _gzip_name(path: Path) -> str | None:
    """Read bounded RFC 1952 FNAME; never trust it as a filesystem path."""
    with path.open('rb') as stream:
        header = stream.read(10)
        if len(header) != 10 or header[:3] != b'\x1f\x8b\x08' or header[3] & 0xe0:
            raise ArchiveError('Invalid GZIP header')
        flags = header[3]
        if flags & 4:
            raw = stream.read(2)
            if len(raw) != 2:
                raise ArchiveError('Truncated GZIP extra header')
            if len(stream.read(int.from_bytes(raw, 'little'))) != int.from_bytes(raw, 'little'):
                raise ArchiveError('Truncated GZIP extra header')
        if flags & 8:
            raw = bytearray()
            while len(raw) < 65536:
                byte = stream.read(1)
                if not byte:
                    raise ArchiveError('Truncated GZIP filename')
                if byte == b'\0':
                    name = raw.decode('latin-1')
                    return validate_name(name) if name else None
                raw.extend(byte)
            raise ArchiveError('GZIP filename exceeds header budget')
    return None


def _same_tree(left: Path, right: Path) -> bool:
    if not left.is_dir() or not right.is_dir() or right.is_symlink():
        return False
    def listing(root):
        result = {}
        for entry in root.rglob('*'):
            if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                return None
            key = entry.relative_to(root).as_posix()
            result[key] = None if entry.is_dir() else sha256_file(entry)
        return result
    return listing(left) == listing(right)


def _external_run(command, budget, destination=None, size=None):
    """Drain both pipes with a deadline; never let a child write archive paths."""
    captured = bytearray()
    received = errors = 0
    limit = 16 * 1024 * 1024 if destination is None else size
    budget.check()
    with contextlib.ExitStack() as stack:
        output = None
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            output = stack.enter_context(destination.open('xb'))
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={**os.environ, 'LC_ALL': 'C.UTF-8'})
        try:
            selector = stack.enter_context(selectors.DefaultSelector())
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                budget.check()
                for key, _ in selector.select(timeout=0.05):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == 'stderr':
                        errors += len(chunk)
                        if errors > 65536:
                            raise ArchiveError('Archive tool diagnostic output budget exceeded')
                        continue
                    received += len(chunk)
                    if received > limit:
                        raise ArchiveError('Archive listing output budget exceeded' if output is None
                                           else 'Archive member size differs from listing')
                    if output is None:
                        captured.extend(chunk)
                    else:
                        budget.bytes += len(chunk)
                        if budget.bytes > budget.values['max_expanded_size']:
                            raise ArchiveError('Archive expanded size budget exceeded')
                        output.write(chunk)
                        budget.current_file = destination.name
                        budget.notify()
            while process.poll() is None:
                budget.check()
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                raise ArchiveError(f'Archive tool failed with exit status {process.returncode}; '
                                   'corrupt, password-protected or unsupported archive')
            if output is not None and received != size:
                raise ArchiveError('Archive member size differs from listing')
            if output is not None:
                budget.completed_files += 1
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()
    try:
        return captured.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ArchiveError('Archive listing is not valid UTF-8') from error


def _technical_records(text):
    records = []
    for block in re.split(r'\n{2,}', text.strip('\n')):
        if not block:
            continue
        fields = {}
        for line in block.splitlines():
            key, separator, value = line.partition(' = ')
            if not separator or key in fields:
                raise ArchiveError('Ambiguous archive technical listing')
            fields[key] = value
        records.append(fields)
    return records


def _external_members(path, kind, output, budget):
    seven_zip = shutil.which('7z')
    unrar = shutil.which('unrar') if kind == 'rar' else None
    if not seven_zip or (kind == 'rar' and not unrar):
        raise ArchiveError('7z/RAR extraction is unavailable; install 7z and, for RAR, unrar')
    text = _external_run([seven_zip, 'l', '-slt', '-sccUTF-8', '-p-', '--', str(path)], budget)
    header, separator, listing = text.partition('\n----------\n')
    if not separator:
        raise ArchiveError('Archive technical listing has no member section')
    header = header.partition('--\n')[2]
    headers = _technical_records(header)
    if len(headers) != 1 or headers[0].get('Type') not in (
            ('7z',) if kind == '7z' else ('Rar', 'Rar5')):
        raise ArchiveError('Unexpected archive type in technical listing')
    metadata = headers[0]
    if (metadata.get('Multivolume', '-') != '-'
            or metadata.get('Volumes', '1') != '1'
            or any(key in metadata for key in ('Volume', 'Volume Index'))):
        raise ArchiveError('Multivolume archives require manual recovery')
    members = []
    seen = set()
    total_size = 0
    for fields in _technical_records(listing):
        name = fields.get('Path', '')
        destination = _member_path(name, output)
        if name.startswith('@'):
            raise ArchiveError('Archive member path uses unsupported listfile syntax')
        key = destination.relative_to(output).as_posix()
        if key in seen:
            raise ArchiveError('Duplicate archive member path')
        seen.add(key)
        if any('link' in field.lower() or 'reparse' in field.lower() for field in fields):
            raise ArchiveError('Archive links and special files are not supported')
        attributes = fields.get('Attributes', '')
        mode = re.search(r'(?:^|\s)([a-zA-Z?-][rwxstST-]{9})(?:\s|$)', attributes)
        if (mode and mode[1][0] not in ('-', 'd')) or 'L' in attributes.split(' ')[0]:
            raise ArchiveError('Archive links and special files are not supported')
        if fields.get('Encrypted') != '-':
            raise ArchiveError('Password-protected archives require manual recovery')
        if any(fields.get(field, '-') != '-' for field in ('Split Before', 'Split After')):
            raise ArchiveError('Multivolume archives require manual recovery')
        if not re.fullmatch(r'[0-9]+', fields.get('Size', '')):
            raise ArchiveError('Invalid archive member size in listing')
        size = int(fields['Size'])
        directory = (fields.get('Folder') == '+' or attributes.startswith('D')
                     or (mode is not None and mode[1].startswith('d')))
        if fields.get('Folder', '-') not in ('+', '-') or (directory and size):
            raise ArchiveError('Unsupported archive member type or size')
        if kind == 'rar' and any(character in name for character in '*?[]'):
            raise ArchiveError('RAR wildcard member paths require manual recovery')
        budget.member(size)
        total_size += size
        if total_size > budget.values['max_expanded_size'] - budget.bytes:
            raise ArchiveError('Archive expanded size budget exceeded')
        members.append((name, destination, size, directory))
    # A file must never also be the parent of another member.
    files = {destination for _, destination, _, directory in members if not directory}
    if any(parent in files for _, destination, _, _ in members for parent in destination.parents):
        raise ArchiveError('Archive member file/directory path conflict')
    if kind == 'rar':
        # Old 7z versions omit Unix link attributes for RAR; unrar's Type is required too.
        text = _external_run([unrar, 'lt', '-c-', '-p-', '-cfg-', '-@', '--', str(path)], budget)
        rar_members = []
        current = None
        for line in text.splitlines():
            key, separator, value = line.lstrip().partition(': ')
            if key == 'Name' and separator:
                if current is not None:
                    rar_members.append(current)
                current = {}
            if current is not None and separator:
                if key in current:
                    raise ArchiveError('Ambiguous RAR technical listing')
                current[key] = value
        if current is not None:
            rar_members.append(current)
        if len(rar_members) != len(members):
            raise ArchiveError('RAR member listings disagree')
        for fields, (name, _, size, directory) in zip(rar_members, members):
            if fields.get('Type') not in ('File', 'Directory') or 'Target' in fields:
                raise ArchiveError('RAR links and special files are not supported')
            if (fields.get('Name') != name or fields.get('Size') != str(size)
                    or (fields['Type'] == 'Directory') != directory):
                raise ArchiveError('RAR member listings disagree')
    return seven_zip, unrar, members


class _Extractor:
    def __init__(self, budget: _Budget, scratch: Path):
        self.budget = budget
        self.scratch = scratch
        self.records = []

    def expand(self, path: Path, name: str, depth: int = 1, lineage: str = '') -> Path:
        self.budget.check()
        kind = _kind(path, name)
        if kind is None:
            return path
        record = {'name': name, 'sha256': sha256_file(path), 'deleted': False, 'members': [], 'layout_version': 2}
        if lineage:
            record['parent'] = lineage
        self.records.append(record)
        if depth > self.budget.values['max_nesting']:
            raise ArchiveError('Archive nesting depth budget exceeded')
        if kind == 'unsupported':
            raise ArchiveError('Archive format extraction is unavailable; provide ZIP/TAR/GZIP or a safely supported adapter')
        area = Path(tempfile.mkdtemp(prefix='expand-', dir=self.scratch))
        output = area / _stem(name)
        archive_file = path
        if kind == 'gzip':
            member_name = _gzip_name(path) or (name[:-3] if name.lower().endswith('.gz') else _stem(name))
            member_name = validate_name(member_name)
            uncompressed = area / 'gzip-payload'
            self.budget.member()
            with gzip.open(path, 'rb') as stream:
                self.budget.copy(stream, uncompressed)
            if tarfile.is_tarfile(uncompressed):
                kind, archive_file = 'tar', uncompressed
            else:
                record['members'].append(member_name)
                final_name = member_name
                payload = area / 'payload' / final_name
                payload.parent.mkdir()
                os.replace(uncompressed, payload)
                expanded = self.expand(payload, final_name, depth + 1, name)
                return expanded
        output.mkdir()
        seen = set()
        if kind in ('7z', 'rar'):
            seven_zip, unrar, members = _external_members(archive_file, kind, output, self.budget)
            for member_name, destination, size, directory in members:
                record['members'].append(destination.relative_to(output).as_posix())
                if directory:
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if kind == '7z':
                    command = [seven_zip, 'x', '-so', '-spd', '-ssc', '-p-', '-bd',
                               '-sccUTF-8', '--', str(archive_file), member_name]
                else:
                    command = [unrar, 'p', '-inul', '-p-', '-cfg-', '-@', '--',
                               str(archive_file), member_name]
                _external_run(command, self.budget, destination, size)
        elif kind == 'zip':
            with zipfile.ZipFile(archive_file) as archive:
                for member in archive.infolist():
                    self.budget.member(member.file_size)
                    member_name = member.orig_filename
                    destination = _member_path(member_name, output)
                    key = destination.relative_to(output).as_posix()
                    if key in seen:
                        raise ArchiveError('Duplicate archive member path')
                    seen.add(key)
                    mode = member.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ArchiveError('Archive links and special files are not supported')
                    if member.flag_bits & 1:
                        raise ArchiveError('Password-protected ZIP requires manual recovery')
                    record['members'].append(key)
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        with archive.open(member) as stream:
                            self.budget.copy(stream, destination)
        else:
            with tarfile.open(archive_file, 'r:*') as archive:
                for member in archive:
                    self.budget.member(member.size)
                    destination = _member_path(member.name, output)
                    key = destination.relative_to(output).as_posix()
                    if key in seen:
                        raise ArchiveError('Duplicate archive member path')
                    seen.add(key)
                    record['members'].append(key)
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                    elif member.isfile() and not member.issparse():
                        with archive.extractfile(member) as stream:
                            self.budget.copy(stream, destination)
                    else:
                        raise ArchiveError('Archive links, sparse files and special files are not supported')
        # Freeze the original member list so recursively derived names are not processed twice.
        for member_path in sorted(output.rglob('*')):
            if not member_path.is_file():
                continue
            expanded = self.expand(member_path, member_path.name, depth + 1, name)
            if expanded != member_path:
                destination = member_path.parent / expanded.name
                if destination != member_path and destination.exists():
                    raise ArchiveError('Nested archive output conflicts with another member')
                member_path.unlink()
                os.replace(expanded, destination)
        children = list(output.iterdir())
        # The wrapper was created by us. A single root file needs no directory;
        # keep actual member directories and multi-file layouts intact.
        return children[0] if len(children) == 1 and children[0].is_file() else output


def process_file(workspace: Workspace, folder: Path, path: Path,
                 original_name: str, source_key: str, limits: dict | None = None, progress=None) -> dict:
    validate_name(original_name)
    _identifier(source_key)
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Evidence input must be a regular file')
    workspace._safe(Path(folder))
    result = {'artifacts': [], 'archives': [], 'gaps': []}
    budget = _Budget(limits, progress=progress)
    try:
        kind = _kind(path, original_name)
    except (ValueError, OSError, tarfile.TarError) as error:
        kind = 'corrupt'
    if kind is None:
        result['artifacts'].append(workspace.publish(folder, path, original_name, source_key))
        return result
    staging = workspace.root / '.iwork/staging'
    workspace._safe(staging)
    staging.mkdir(parents=True, exist_ok=True)
    # A user-supplied local archive is never implicitly deleted. Work on a managed copy.
    managed = path.absolute().is_relative_to(staging)
    retained_directory = None
    if not managed:
        retained_directory = Path(tempfile.mkdtemp(prefix='archive-input-', dir=staging))
        managed_path = retained_directory / original_name
        shutil.copyfile(path, managed_path)
    else:
        workspace._safe(path)
        managed_path = path
    original_digest = sha256_file(managed_path)
    with tempfile.TemporaryDirectory(prefix='archive-work-', dir=staging) as temporary:
        budget.disk_root = staging
        extractor = _Extractor(budget, Path(temporary))
        try:
            budget.notify(force=True)
            expanded = extractor.expand(managed_path, original_name)
            budget.phase = 'verifying'
            budget.notify(force=True)
            result['archives'] = extractor.records
            if not extractor.records:
                raise ArchiveError('Archive format could not be verified')
            if sha256_file(managed_path) != original_digest:
                raise ArchiveError('Archive input changed during extraction')
            if expanded.is_file():
                record = workspace.publish(folder, expanded, expanded.name, source_key)
                result['artifacts'].append(record)
            else:
                with workspace._lock():
                    destination, reused = workspace._destination(
                        folder, expanded.name, source_key, lambda existing: _same_tree(expanded, existing))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    # Compute and verify publication records before the atomic directory rename.
                    for file in sorted(expanded.rglob('*')):
                        if file.is_file():
                            relative = file.relative_to(expanded)
                            result['artifacts'].append({
                                'path': (destination / relative).relative_to(folder).as_posix(),
                                'name': file.name, 'sha256': sha256_file(file),
                                'size': file.stat().st_size, 'reused': reused})
                    if not reused:
                        os.replace(expanded, destination)
            try:
                managed_path.unlink()
                for record in result['archives']:
                    record['deleted'] = True
                if retained_directory:
                    retained_directory.rmdir()
            except OSError as error:
                result['gaps'].append(f'归档已解压但原包清理失败：{original_name}（{type(error).__name__}）')
            if not managed:
                result['archives'][0]['original_preserved'] = True
        except (OSError, ValueError, EOFError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as error:
            result['artifacts'] = []
            result['archives'] = extractor.records or [
                {'name': original_name, 'sha256': original_digest, 'deleted': False, 'members': []}]
            result['gaps'].append(f'归档处理失败，原包保留在暂存区：{original_name}（{error}）')
    return result
