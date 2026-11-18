"""Durable collection orchestration; native hosts can reconnect without a socket."""

import contextlib
import errno
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, time
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .archives import process_file, check_disk_space
from .discovery import LOG_ID, discover, key, ordered_targets
from .downloads import download_http
from .dws import DwsClient
from .images import inspect_image
from .layout import compact_single_files, cleanup_single_files, update_paths
from .models import fingerprint, normalize_snapshot, redact
from .report import write_report
from .recovery import recover_archive
from .state import State
from .storage import Workspace, atomic_json, sha256_file, validate_name
from .vehiclelogs import collect_vehicle_logs


def timestamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')


class Service:
    def __init__(self, root=None, downloads_root=None):
        self.root = Path(root or Path.home() / 'iwork_space').expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = Workspace(self.root)
        self.state = State(self.root)
        config_path = self.root / '.iwork/config.json'
        self.config = json.loads(config_path.read_text()) if config_path.exists() else {}
        self.recovery_catalog = Path(self.config.get('recovery_catalog') or
            Path(__file__).resolve().parent.parent / '.iwork/recovery-cache.json').expanduser()
        self.downloads_root = Path(downloads_root or self.config.get('downloads_root') or Path.home() / 'Downloads').expanduser().resolve()
        self.dws = DwsClient(executable=self.config.get('dws_executable', 'dws'),
                             profile=self.config.get('dws_profile'))
        self._migrate_bug_folders()
        try:
            self._prune_browser_directories()
        except (OSError, ValueError):
            # A later worker retries cleanup and reports permission/path failures.
            pass

    def close(self):
        self.state.close()

    def _migrate_bug_folders(self):
        legacy_ids = {job['bug_id'] for job in self.state.jobs()
                      if re.fullmatch(r'bug_' + re.escape(job['bug_id']) + r'_[0-9]{6}',
                                      Path(job['folder']).name)}
        for bug_id in legacy_ids:
            with self._bug_lock(bug_id):
                jobs = [job for job in self.state.jobs() if job['bug_id'] == bug_id]
                if any(Path(job['folder']).parent != self.root for job in jobs):
                    raise ValueError('Bug folder migration must remain inside the workspace')
                folder = self.workspace.bug_dir(bug_id)
                for job in jobs:
                    if job['folder'] != str(folder):
                        job['folder'] = str(folder)
                        self.state.put(job)

    def _prune_browser_directories(self):
        """Only remove empty collector directories; leave downloads and user files alone."""
        managed = self.downloads_root / 'iwork-collector'
        if managed.is_symlink() or any(parent.is_symlink() for parent in managed.parents):
            raise ValueError('浏览器中转目录不接受符号链接')
        if not managed.is_dir():
            return
        active = {self.downloads_root / task['relative_path'] for task in self.state.tasks()
                  if task['status'] in ('pending', 'started')}
        active_dirs = {path.parent for path in active}
        def remove_empty(path):
            try:
                path.rmdir()
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                    raise
        try:
            folders = list(managed.iterdir())
        except FileNotFoundError:
            return
        for folder in folders:
            if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r'job_[0-9a-f]{32}', folder.name):
                continue
            try:
                task_dirs = list(folder.iterdir())
            except FileNotFoundError:
                continue
            for task_dir in task_dirs:
                if (not task_dir.is_symlink() and task_dir.is_dir() and task_dir not in active_dirs
                        and re.fullmatch(r'task_[0-9a-f]{32}', task_dir.name)):
                    remove_empty(task_dir)
            remove_empty(folder)
        remove_empty(managed)

    def summary(self, job):
        targets = list(ordered_targets(job.get('candidates', [])).values())
        progress = {'phase': {'ready': 'complete', 'captured': 'queued'}.get(job['status'], job['status']),
                    'total': len(targets),
                    'completed': sum(any(c['state'] == 'done' for c in group) for group in targets),
                    'failed': sum(all(c['state'] in ('failed', 'skipped') for c in group) for group in targets)}
        if job['status'] == 'processing':
            path = self.workspace._safe(self.root / '.iwork/progress' / (job['job_id'] + '.json'))
            if path.is_file():
                activity = json.loads(path.read_text())
                progress.update(activity)
        return {'job_id': job['job_id'], 'bug_id': job['bug_id'], 'status': job['status'],
                'folder': job['folder'], 'report_path': str(Path(job['folder']) / 'bugreport.md'),
                'gaps': redact(list(dict.fromkeys(job['gaps']))),
                'waiting_count': sum(t['status'] in ('pending', 'started') for t in self.state.tasks(job['job_id'])),
                'updated_at': job['updated_at'], 'revision': job.get('revision', 0), 'progress': progress}

    def _progress(self, job, phase, name='', **details):
        path = self.workspace._safe(self.root / '.iwork/progress' / (job['job_id'] + '.json'))
        previous = json.loads(path.read_text()) if path.exists() else {}
        started = previous.get('started_at') if (previous.get('phase'), previous.get('name')) == (phase, name) else None
        atomic_json(path, redact({'phase': phase, 'name': name, 'started_at': started or timestamp(),
                                  'updated_at': timestamp(), **details}))

    @contextlib.contextmanager
    def _bug_lock(self, bug_id, blocking=True):
        lock_dir = self.root / '.iwork/locks'
        lock_dir.mkdir(exist_ok=True)
        with (lock_dir / ('bug-' + str(bug_id) + '.lock')).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _merge_current(self, job):
        current = Path(job['folder']) / '.iwork/manifest.json'
        manifest = json.loads(current.read_text()) if current.exists() else {}
        moves = {**manifest.get('path_moves', {}), **job.get('path_moves', {})}
        job['path_moves'] = moves
        job['layout_cleanup'] = {**manifest.get('layout_cleanup', {}), **job.get('layout_cleanup', {})}
        update_paths(manifest, moves)
        update_paths(job, moves)
        for field, identity in (
                ('artifacts', lambda item: item['path']),
                ('images', lambda item: item['sha256']),
                ('archives', lambda item: (item['sha256'], item['name'], item.get('parent')))):
            merged = {}
            for item in [*manifest.get(field, []), *job.get(field, [])]:
                previous = merged.get(identity(item))
                if field == 'artifacts' and previous and previous.get('sha256') == item.get('sha256'):
                    keys = list(dict.fromkeys(k for record in (previous, item)
                        for k in [record.get('source_key'), *record.get('additional_source_keys', [])] if k))
                    sources = []
                    for record in (previous, item):
                        for source in [record.get('source'), *record.get('additional_sources', [])]:
                            if source and source not in sources:
                                sources.append(source)
                    previous['additional_source_keys'] = keys
                    previous['additional_sources'] = sources
                else:
                    merged[identity(item)] = item
            job[field] = list(merged.values())

    def _processing_gaps(self, job):
        gaps = [gap for image in job.get('images', []) for gap in image.get('gaps', [])]
        gaps += ['原包清理或解压尚未完成：' + archive['name']
                 for archive in job.get('archives', []) if not archive.get('deleted')]
        return list(dict.fromkeys(gaps))

    def _check_evidence(self, job):
        self._merge_current(job)
        valid = []
        for artifact in job['artifacts']:
            try:
                path = self.workspace._safe(Path(job['folder']) / artifact['path'])
                if (not path.is_relative_to(Path(job['folder'])) or not path.is_file()
                        or sha256_file(path) != artifact['sha256']):
                    raise ValueError('Evidence cache is missing or changed')
                valid.append(artifact)
            except (ValueError, OSError):
                gap = '证据缺失或已被修改：' + artifact['path'] + '；请重新采集，用户修改不会被覆盖'
                job.setdefault('evidence_gaps', []).append(gap)
                job.setdefault('invalid_artifact_paths', []).append(artifact['path'])
        job['artifacts'] = valid
        job['gaps'].extend(job.get('evidence_gaps', []))
        if job['status'] == 'ready' and job['gaps']:
            job['status'] = 'partial'

    def _save(self, job):
        if not self.workspace._safe(Path(job['folder'])).is_dir():
            raise ValueError('工单目录已删除，请重新采集以建立新任务')
        self._merge_current(job)
        # Invalid cache entries are never reintroduced by a stale shared manifest.
        invalid_paths = set(job.get('invalid_artifact_paths', []))
        job['artifacts'] = [a for a in job['artifacts'] if a['path'] not in invalid_paths]
        old_processing = job.get('processing_gaps', [])
        job['gaps'] = [gap for gap in job['gaps'] if gap not in old_processing]
        job['processing_gaps'] = self._processing_gaps(job)
        job['gaps'].extend(job['processing_gaps'])
        job['gaps'] = list(dict.fromkeys(job['gaps']))
        if job['status'] == 'ready' and job['gaps']:
            job['status'] = 'partial'
        job['updated_at'] = timestamp()
        write_report(job)
        self.state.put(job)

    def _restore_workspace_metadata(self, job):
        """Repair only a recorded workspace with its own matching manifest."""
        folder = self.workspace._safe(Path(job['folder']))
        if not folder.exists():
            return False
        if (not folder.is_dir() or folder.parent != self.root or
                not re.fullmatch(r'bug_[0-9]{6}_' + re.escape(job['bug_id']), folder.name)):
            raise ValueError('工单目录身份无效')
        metadata = self.workspace._safe(folder / '.iwork/workspace.json')
        identity = {'bug_id': job['bug_id'], 'source': 'iwork'}
        if metadata.exists():
            if json.loads(metadata.read_text()) != identity:
                raise ValueError('工单目录身份不匹配')
        else:
            manifest_path = self.workspace._safe(folder / '.iwork/manifest.json')
            manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
            if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1
                    or manifest.get('bug_id') != job['bug_id']):
                raise ValueError('工单目录身份缺失，现有清单不能确认所属工单')
            atomic_json(metadata, identity)
        source = self.workspace._safe(folder / '.iwork/source' / job['job_id'])
        ticket = self.workspace._safe(source / 'ticket.json')
        markdown = self.workspace._safe(source / 'ticket.md')
        source.mkdir(parents=True, exist_ok=True)
        if not ticket.exists():
            atomic_json(ticket, redact(job['snapshot']))
        if not markdown.exists():
            markdown.write_text(redact(job['snapshot'].get('description', '')), encoding='utf-8')
        return True

    def _retire_deleted_workspace(self, job):
        # Do not write a report into the deleted folder or reuse old receipts.
        job['status'] = 'cancelled'
        job['workspace_deleted'] = True
        job['updated_at'] = timestamp()
        job['gaps'] = list(dict.fromkeys([*job['gaps'], '工单目录已删除，旧任务已停止；重新采集将创建新任务']))
        for task in self.state.tasks(job['job_id']):
            if task['status'] in ('pending', 'started'):
                task.update(status='failed', error='工单目录已删除')
                self.state.task_put(task)
        self.state.put(job)

    def capture(self, snapshot, trusted=False, retry_failed=False):
        if not isinstance(retry_failed, bool):
            raise ValueError('retry_failed 必须为布尔值')
        snapshot = normalize_snapshot(snapshot, trusted=trusted)
        with self._bug_lock(snapshot['bug_id'], blocking=False) as acquired:
            if not acquired:
                # A running worker holds the lock for the whole search/extract
                # cycle; blocking here would freeze the extension's serial queue.
                existing = next((j for j in self.state.jobs()
                                 if j['bug_id'] == snapshot['bug_id'] and not j.get('workspace_deleted')), None)
                if existing:
                    if retry_failed:
                        atomic_json(self._pending_request(existing['job_id']), {'resume': True})
                    return self.summary(existing)
                raise ValueError('该工单正在处理中，请稍后再试')
            return self._capture(snapshot, retry_failed=retry_failed)

    def _capture(self, snapshot, retry_failed=False):
        digest = fingerprint(snapshot)
        existing = self.state.find(digest)
        if existing and existing.get('workspace_deleted'):
            existing = None
        # Retire all old snapshots before a same-day folder can be recreated;
        # late browser receipts must never publish into the new workspace.
        for previous_job in self.state.jobs():
            if previous_job['bug_id'] == snapshot['bug_id'] and not previous_job.get('workspace_deleted'):
                if not self._restore_workspace_metadata(previous_job):
                    self._retire_deleted_workspace(previous_job)
                    if existing and existing['job_id'] == previous_job['job_id']:
                        existing = None
        if existing:
            repaired = self._repair_log_sources(existing)
            self._refresh_runtime_sources(existing, snapshot)
            self._check_evidence(existing)
            self._save(existing)
            failed_waiting = {t['candidate_id'] for t in self.state.tasks(existing['job_id'])
                              if t['status'] == 'failed'}
            if repaired or (retry_failed and any(c['state'] == 'failed' or
                    (c['state'] == 'waiting' and c['id'] in failed_waiting)
                    for c in existing['candidates'])):
                return self._resume(existing['job_id'])
            return self.summary(existing)
        folder = self.workspace.bug_dir(snapshot['bug_id'])
        job_id = 'job_' + uuid.uuid4().hex
        source = folder / '.iwork/source' / job_id
        source.mkdir(parents=True, exist_ok=True)
        atomic_json(source / 'ticket.json', redact(snapshot))
        (source / 'ticket.md').write_text(redact(snapshot['description']), encoding='utf-8')
        previous_path = folder / '.iwork/manifest.json'
        previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
        candidates, gaps = discover(snapshot)
        gaps += snapshot['coverage']['gaps']
        if snapshot['coverage'].get('complete') is not True and not snapshot['coverage']['gaps']:
            gaps.append('页面采集完整性未证实')
        job = {'job_id': job_id, 'bug_id': snapshot['bug_id'], 'fingerprint': digest,
               'snapshot': snapshot, 'folder': str(folder), 'status': 'captured',
               'candidates': [dict(c, state='pending') for c in candidates],
               'source_candidate_ids': [c['id'] for c in candidates],
               'gaps': gaps, 'attempts': [], 'searches': [],
               'artifacts': previous.get('artifacts', []), 'archives': previous.get('archives', []),
               'images': previous.get('images', []), 'created_at': timestamp(), 'updated_at': timestamp()}
        self._check_evidence(job)
        self._save(job)
        return self.summary(job)

    def _repair_log_sources(self, job):
        """Rebuild only the failed legacy tasks that mistook a log ID for a group."""
        old = [candidate for candidate in job['candidates'] if candidate['transport'] == 'dws']
        if not old or any(candidate.get('name') or candidate['state'] not in ('failed', 'pending') for candidate in old):
            return
        if not any(isinstance(candidate.get('group'), dict) and
                   LOG_ID.fullmatch(candidate['group'].get('name', '')) for candidate in old):
            return
        fresh, _ = discover(job['snapshot'])
        replacement = [candidate for candidate in fresh if candidate['transport'] == 'dws']
        if not any(candidate.get('name') for candidate in replacement):
            return
        retired = {candidate['id'] for candidate in old}
        job['candidates'] = [candidate for candidate in job['candidates'] if candidate['id'] not in retired]
        job['candidates'].extend(dict(candidate, state='pending') for candidate in replacement)
        job['source_candidate_ids'] = [candidate['id'] for candidate in fresh]
        job['attempts'] = [attempt for attempt in job['attempts'] if attempt['candidate'] not in retired]
        return True

    def _refresh_runtime_sources(self, job, snapshot):
        if 'page_tab_id' in snapshot:
            job['snapshot']['page_tab_id'] = snapshot['page_tab_id']
        fresh, _ = discover(snapshot)
        # Fingerprint equality guarantees the same ordered source structure. Keep
        # persistent candidate IDs even when a fallback ID hashed the old signature.
        slots = job.setdefault('source_candidate_ids', [c['id'] for c in job['candidates'][:len(fresh)]])
        candidates = {candidate['id']: candidate for candidate in job['candidates']}
        changed = {}
        for identity, source in zip(slots, fresh):
            candidate = candidates.get(identity)
            if candidate is not None and source.get('url'):
                candidate['url'] = source['url']
                candidate['source'] = source.get('source', {})
                changed[identity] = source['url']
        for task in self.state.tasks(job['job_id']):
            if task['candidate_id'] in changed:
                task['url'] = changed[task['candidate_id']]
                if task.get('page_fetch'):
                    task['page_tab_id'] = snapshot.get('page_tab_id')
                    task['page_url'] = snapshot.get('url', '')
                self.state.task_put(task)

    def _stage(self, job, candidate):
        return self.workspace.staging_dir(job['job_id'], key(candidate['id']))

    def _validate_payload(self, path):
        with Path(path).open('rb') as stream:
            prefix = stream.read(65536).lstrip(b'\xef\xbb\xbf \t\r\n').lower()
        if prefix.startswith((b'<!doctype html', b'<html', b'<head', b'<body', b'<form')):
            raise ValueError('下载内容为登录或错误网页，需要重新登录或专用适配器')
        if prefix.startswith(b'{'):
            try:
                payload = json.loads(prefix)
            except (ValueError, UnicodeError):
                payload = None
            if isinstance(payload, dict) and (payload.get('error') or payload.get('errors')
                    or str(payload.get('status', '')).lower() in ('error', 'failed', '401', '403', '404', '500')):
                raise ValueError('下载内容为 JSON 错误响应，未作为文件证据发布')

    def _cleanup_browser_copies(self, job):
        old = job.get('browser_cleanup_gaps', [])
        job['gaps'] = [gap for gap in job['gaps'] if gap not in old]
        gaps = []
        for task in self.state.tasks(job['job_id']):
            expected_hash = task.get('handoff_sha256')
            if task['status'] != 'complete' or not expected_hash:
                continue
            path = self.downloads_root / task['relative_path']
            if not path.exists() and not path.is_symlink():
                continue
            try:
                if (path.is_symlink() or not path.resolve().is_relative_to(self.downloads_root)
                        or any(parent.is_symlink() for parent in path.parents)
                        or sha256_file(path) != expected_hash):
                    raise ValueError('交接后的浏览器副本路径或内容已改变')
                path.unlink()
            except (OSError, ValueError):
                gaps.append('浏览器下载副本清理未完成：' + task['name'] + '；请检查路径、权限或内容变化后重试')
        job['browser_cleanup_gaps'] = gaps
        try:
            self._prune_browser_directories()
        except (OSError, ValueError):
            gaps.append('浏览器中转空目录清理未完成；请检查下载目录权限或符号链接后重试')
        job['gaps'].extend(gaps)

    def _cleanup_ingestions(self, job):
        for identity, entry in list(job.get('ingestions', {}).items()):
            if entry.get('state') != 'done':
                continue
            failed = False
            for value in (entry['path'], entry['processing_path']):
                path = self.workspace._safe(Path(value))
                if not path.is_relative_to(self.root / '.iwork/staging'):
                    raise ValueError('Invalid managed ingestion cleanup path')
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    failed = True
            for archive in job['archives']:
                if archive['sha256'] in entry.get('archive_hashes', []):
                    archive['deleted'] = not failed
            if not failed:
                del job['ingestions'][identity]

    def _ingest(self, job, candidate, path, name):
        path, name = Path(path), validate_name(name)
        self._progress(job, phase='verifying', name=name)
        self._validate_payload(path)
        # Keep the durable incoming file until candidate/artifact state is committed.
        # Archive processing may delete its working copy without losing replay input.
        processing = self.workspace.staging_dir(job['job_id'], 'processing-' + key(candidate['id'])) / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Evidence input must be a regular file')
        self.workspace._safe(processing)
        check_disk_space(processing.parent)
        processing.unlink(missing_ok=True)
        try:
            # Two durable names, one data allocation; deleting the processing
            # name after extraction cannot remove the incoming replay input.
            os.link(path, processing)
        except OSError as error:
            if error.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP):
                raise
            check_disk_space(processing.parent, path.stat().st_size)
            with path.open('rb') as source, processing.open('xb') as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    check_disk_space(processing.parent, len(chunk))
                    output.write(chunk)
        digest = sha256_file(path)
        if digest != sha256_file(processing):
            raise ValueError('证据处理副本校验失败')
        entry = {'path': str(path), 'processing_path': str(processing), 'name': name,
                 'state': 'processing', 'archive_hashes': [], 'sha256': digest,
                 'size': path.stat().st_size, 'source': candidate.get('source', {}),
                 'relative_parent': candidate.get('relative_parent', ''),
                 'parent_candidate_id': candidate.get('parent_candidate_id')}
        job.setdefault('ingestions', {})[candidate['id']] = entry
        self._save(job)
        parent_text = str(candidate.get('relative_parent') or '')
        parent = Path(parent_text)
        if parent_text and (parent.is_absolute() or any(part in ('', '.', '..') for part in parent_text.split('/'))):
            raise ValueError('证据目录路径无效')
        for part in parent.parts:
            validate_name(part)
        folder = self.workspace._safe(Path(job['folder']) / parent)
        folder.mkdir(parents=True, exist_ok=True)
        result = recover_archive(self.workspace, folder, name, digest, key(candidate['id']),
                                 job['bug_id'], self.recovery_catalog)
        if result is None:
            result = process_file(self.workspace, folder, processing, name, key(candidate['id']),
                                  limits=self.config.get('archive_limits'),
                                  progress=lambda data: self._progress(job, name=name, **data))
        for artifact in result['artifacts']:
            if parent_text:
                artifact['path'] = (parent / artifact['path']).as_posix()
            artifact['source_key'] = candidate['id']
            artifact['source'] = redact(candidate.get('source', {}))
            previous = next((a for a in job['artifacts'] if a['path'] == artifact['path']), None)
            if previous:
                previous.setdefault('additional_sources', []).append(artifact['source'])
            else:
                job['artifacts'].append(artifact)
            if Path(artifact['name']).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'):
                self._image(job, artifact)
        entry['archive_hashes'] = [archive['sha256'] for archive in result['archives']]
        for archive in result['archives']:
            # The incoming copy still exists until the durable checkpoint below.
            archive['deleted'] = False
            old = next((a for a in job['archives'] if a['sha256'] == archive['sha256']
                        and a['name'] == archive['name'] and a.get('parent') == archive.get('parent')), None)
            if old:
                old.update(archive)
            else:
                job['archives'].append(archive)
        job['gaps'].extend(result['gaps'])
        if not result['artifacts'] and result['gaps']:
            raise ValueError('证据处理未完成：' + '; '.join(result['gaps']))
        candidate['state'] = 'done'
        entry['state'] = 'done'
        self._save(job)
        self._cleanup_ingestions(job)
        self._save(job)

    def _image(self, job, artifact):
        if any(x.get('sha256') == artifact['sha256'] for x in job['images']):
            return
        destination = Path(job['folder']) / '.iwork/derived/images' / artifact['sha256'][:20]
        result = inspect_image(Path(job['folder']) / artifact['path'], destination,
                               languages=self.config.get('ocr_languages', 'eng'),
                               observation_command=self.config.get('image_command'))
        job['images'].append(dict(redact(result), name=artifact['name'], sha256=artifact['sha256'], path=artifact['path']))
        job['gaps'].extend(result['gaps'])
        # Only follow exact URLs and explicit filenames from OCR, and cap at two rounds.
        generation = job.get('current_generation', 0)
        for url in result['clues']['urls']:
            identity = 'ocr-' + key(url)
            if any(c['id'] == identity or c.get('url') == url for c in job['candidates']):
                continue
            if generation >= 2:
                job['gaps'].append('图片线索回流已达两轮预算，仍有未处理链接')
                continue
            job['candidates'].append({'id': identity, 'target_id': identity, 'priority': 4,
                                      'name': '', 'url': url, 'transport': 'http', 'state': 'pending',
                                      'generation': generation + 1,
                                      'source': {'kind': 'ocr', 'image': artifact['path']}})
        for filename in result['clues']['files']:
            for group in job['snapshot'].get('groups', []):
                identity = 'ocr-file-' + key(filename + json.dumps(group, sort_keys=True))
                if generation < 2 and not any(c['id'] == identity for c in job['candidates']):
                    job['candidates'].append({'id': identity, 'target_id': identity, 'priority': 1,
                                              'name': filename, 'group': group, 'transport': 'dws',
                                              'state': 'pending', 'generation': generation + 1,
                                              'source': {'kind': 'ocr', 'image': artifact['path']}})

    def _browser_task(self, job, candidate):
        existing = next((t for t in self.state.tasks(job['job_id']) if t['candidate_id'] == candidate['id']), None)
        if existing:
            if existing['status'] == 'failed':
                self._reset_browser_task(existing)
            candidate['state'] = 'waiting'
            return existing
        name = validate_name(candidate.get('name') or Path(urlsplit(candidate['url']).path).name or 'unnamed-download')
        task_id = 'task_' + uuid.uuid4().hex
        task = {'task_id': task_id, 'job_id': job['job_id'], 'candidate_id': candidate['id'],
                'url': candidate['url'], 'name': name, 'status': 'pending',
                'relative_path': '/'.join(('iwork-collector', job['job_id'], task_id, name))}
        if candidate.get('page_fetch'):
            tab_id = job['snapshot'].get('page_tab_id')
            if not isinstance(tab_id, int) or isinstance(tab_id, bool) or tab_id < 0:
                raise ValueError('评论图片需要原工单页面，请通过浏览器扩展重新采集')
            task.update(page_fetch=True, page_tab_id=tab_id, page_url=job['snapshot'].get('url', ''))
        self.state.task_put(task)
        candidate['state'] = 'waiting'
        return task

    def _reset_browser_task(self, task):
        task['status'] = 'pending'
        for field in ('download_id', 'error', 'staged_path', 'handoff_sha256'):
            task.pop(field, None)
        self.state.task_put(task)

    def _retained_dws_files(self, job, candidate):
        if candidate.get('downloaded_files'):
            return True
        # Older releases checkpointed only the child ingestion. Recover only an
        # exact original archive; a partial set of parsed members is insufficient.
        identity = candidate['id'] + '-0'
        entry = job.get('ingestions', {}).get(identity)
        if not entry or entry.get('name') != candidate.get('name') or not entry.get('archive_hashes'):
            return False
        path = self.workspace._safe(Path(entry['path']))
        if not path.is_relative_to(self._stage(job, candidate)) or not path.is_file():
            return False
        expected = entry.get('sha256') or entry['archive_hashes'][0]
        if sha256_file(path) != expected:
            raise ValueError('已下载原包校验失败，保留暂存文件供核对')
        candidate['downloaded_files'] = [{'id': identity, 'local_path': str(path),
            'name': entry['name'], 'source': entry.get('source') or candidate.get('source', {}),
            'sha256': expected, 'parent_candidate_id': candidate['id']}]
        self._save(job)
        return True

    def _ingest_dws_files(self, job, candidate):
        job['gaps'].extend(candidate.get('acquisition_gaps', []))
        parsed_members = any(child.get('source', {}).get('mode') == 'parsed-member'
                             for child in candidate['downloaded_files'])
        if parsed_members:
            job['gaps'].append('已取得平台解析文件，未取得原始压缩包，材料完整性未证实')
        for child in candidate['downloaded_files']:
            if child.get('state') == 'done':
                continue
            path = self.workspace._safe(Path(child['local_path']))
            if not path.is_relative_to(self.root / '.iwork/staging'):
                raise ValueError('Invalid retained DWS file path')
            if child.get('sha256') and sha256_file(path) != child['sha256']:
                raise ValueError('已下载文件内容已改变，保留暂存文件供核对')
            self._ingest(job, child, path, child['name'])
        if parsed_members:
            candidate['acquisition_mode'] = 'parsed-members'
        candidate['state'] = 'done'

    def _cleanup_redundant_downloads(self, job, completed):
        changed = False
        published = {item['sha256'] for item in job['artifacts']}
        published.update(item['sha256'] for item in job['archives'] if item.get('deleted'))
        for other in job['candidates']:
            if other['id'] == completed['id'] or other['target_id'] != completed['target_id']:
                continue
            children = other.get('downloaded_files', [])
            legacy_id = other['id'] + '-0'
            legacy = job.get('ingestions', {}).get(legacy_id)
            if not children and legacy and legacy.get('name') == other.get('name'):
                children = [{'id': legacy_id, 'local_path': legacy['path']}]
            for child in children:
                if child.get('state') == 'done':
                    continue
                path = self.workspace._safe(Path(child['local_path']))
                if not path.is_relative_to(self.root / '.iwork/staging') or not path.is_file():
                    job['gaps'].append('重复来源暂存文件无法核验：' + str(child.get('name', path.name)))
                    continue
                if sha256_file(path) not in published:
                    job['gaps'].append('重复来源内容不同，暂存文件保留：' + path.name)
                    continue
                entry = job.get('ingestions', {}).get(child['id'])
                if entry:
                    processing = self.workspace._safe(Path(entry['processing_path']))
                    if (not processing.is_relative_to(self.root / '.iwork/staging') or
                            (processing.exists() and sha256_file(processing) not in published)):
                        job['gaps'].append('重复来源处理副本无法核验，文件保留：' + path.name)
                        continue
                    entry['state'] = 'done'
                else:
                    job.setdefault('ingestions', {})[child['id']] = {
                        'path': str(path), 'processing_path': str(path), 'name': path.name,
                        'state': 'done', 'archive_hashes': []}
                child['state'] = 'done'
                changed = True
            if other['state'] == 'pending':
                other['state'] = 'skipped'
        if changed:
            # Persist done before unlinking, so missing files after a crash are
            # replayed as completed cleanup rather than lost evidence.
            self._save(job)
        self._cleanup_ingestions(job)

    def _dws_sources(self, job, candidate):
        if self._retained_dws_files(job, candidate):
            self._ingest_dws_files(job, candidate)
            return
        gap_start = len(job['gaps'])
        snapshot = job['snapshot']
        captured = snapshot.get('captured_at', timestamp())
        end = datetime.fromisoformat(captured.replace('Z', '+00:00')).replace(microsecond=0)
        if end.tzinfo is None:
            end = end.astimezone()
        start, end_text, basis = self._dws_window(job, candidate, end)
        if basis == '文件名日期前后 3 天':
            job['gaps'].append('钉钉时间范围缺少工单创建时间，本次根据日志文件名日期查询前后 3 天，覆盖有界')
        elif basis == '采集前七天':
            job['gaps'].append('钉钉时间范围缺少工单创建时间和文件日期，本次仅查询采集前七天，覆盖有界')
        self._progress(job, phase='searching', name=candidate.get('name', ''))
        old_progress = self.dws.progress
        self.dws.progress = lambda **data: self._progress(job, name=candidate.get('name', ''), **data)
        try:
            result = self.dws.search_files(candidate['group'], candidate.get('name', ''), start,
                                           end_text, self._stage(job, candidate),
                                           query=job['bug_id'] if not candidate.get('name') else None,
                                           prefer_chat=candidate.get('source', {}).get('kind') == 'group_mention')
        finally:
            self.dws.progress = old_progress
        coverage = redact(result['coverage'])
        coverage['window_basis'] = basis
        job['searches'].append(coverage)
        job['gaps'].extend(result['gaps'])
        atomic_json(Path(job['folder']) / '.iwork/collection' / (key(candidate['id']) + '-dws.json'), result['ledger'])
        if not result['candidates']:
            candidate['log_links'] = redact(result.get('log_links', []))
            remaining_originals = [other for other in job['candidates']
                if other['target_id'] == candidate['target_id'] and other['id'] != candidate['id']
                and other['transport'] == 'dws' and other['priority'] > candidate['priority']
                and other['state'] == 'pending']
            if remaining_originals:
                raise ValueError('群消息路径尚未取得原文件；继续检索该群文件，之后再尝试日志平台副本')
            links = list({link['url']: link for other in job['candidates']
                if other['target_id'] == candidate['target_id']
                for link in other.get('log_links', []) if link.get('url')}.values())
            if len(links) == 1:
                self._progress(job, phase='downloading', name=candidate.get('name', ''))
                portal = collect_vehicle_logs(links[0], candidate.get('name', ''), self._stage(job, candidate) / 'vehiclelogs')
                portal_coverage = redact(portal['coverage'])
                portal_coverage['log_links'] = redact(links)
                job['searches'].append(portal_coverage)
                job['gaps'].extend(portal['gaps'])
                atomic_json(Path(job['folder']) / '.iwork/collection' / (key(candidate['id']) + '-vehiclelogs.json'), portal['ledger'])
                result['candidates'] = portal['candidates']
            elif len(links) > 1:
                job['gaps'].append('命中多条日志平台记录，未自动选择；请核对消息来源')
            if not result['candidates']:
                raise ValueError('钉钉来源未取得可核验文件')
        candidate['downloaded_files'] = [dict(found, id=candidate['id'] + '-' + str(index),
            parent_candidate_id=candidate['id'], sha256=sha256_file(Path(found['local_path'])),
            size=Path(found['local_path']).stat().st_size)
            for index, found in enumerate(result['candidates'])]
        candidate['acquisition_gaps'] = list(dict.fromkeys(job['gaps'][gap_start:]))
        self._save(job)
        self._ingest_dws_files(job, candidate)

    def _dws_window(self, job, candidate, captured):
        """Bound DWS history around the explicit log date when the ticket has no creation date."""
        fields = job.get('snapshot', {}).get('fields', {})
        created = fields.get('created_at') or fields.get('创建时间') or fields.get('创建日期')
        if created:
            try:
                value = datetime.fromisoformat(str(created).replace('Z', '+00:00'))
                if value.tzinfo is None:
                    value = value.replace(tzinfo=captured.tzinfo)
                return str(created), captured.isoformat(), '工单创建时间'
            except ValueError:
                pass
        name = str(candidate.get('name') or '')
        text = name + '\n' + str(job.get('snapshot', {}).get('description') or '')
        dates = re.findall(r'(?<!\d)(20\d{6})(?!\d)', text)
        if dates:
            try:
                day = datetime.strptime(dates[0], '%Y%m%d').date()
                zone = captured.tzinfo
                start = datetime.combine(day - timedelta(days=3), time.min, tzinfo=zone)
                finish = datetime.combine(day + timedelta(days=4), time.min, tzinfo=zone)
                return start.isoformat(), finish.isoformat(), '文件名日期前后 3 天'
            except ValueError:
                pass
        return (captured - timedelta(days=7)).isoformat(), captured.isoformat(), '采集前七天'

    def run(self, job_id, wait_for_lock=False):
        job = self.state.get(job_id)
        # Kicked workers wait for the lock so a completion receipt arriving
        # mid-run is never lost; request paths stay non-blocking.
        with self._bug_lock(job['bug_id'], blocking=wait_for_lock) as acquired:
            if not acquired:
                return self.summary(self.state.get(job_id))
            job = self.state.get(job_id)
            if job['status'] == 'cancelled':
                return self.summary(job)
            request = self._pending_request(job_id)
            if request.exists():
                request.unlink(missing_ok=True)
                job_id = self._resume(job_id)['job_id']
                job = self.state.get(job_id)
            if not self._restore_workspace_metadata(job):
                self._retire_deleted_workspace(job)
                replacement = self._capture(job['snapshot'], retry_failed=True)
                job_id = replacement['job_id']
                job = self.state.get(job_id)
            self._check_evidence(job)
            self._cleanup_browser_copies(job)
            self._cleanup_ingestions(job)
            job['status'] = 'processing'
            self._progress(job, phase='preparing')
            self._save(job)
            while True:
                candidates_before = len(job['candidates'])
                for target, candidates in ordered_targets(job['candidates']).items():
                    if any(c['state'] == 'done' for c in candidates):
                        self._cleanup_redundant_downloads(job, next(c for c in candidates if c['state'] == 'done'))
                        for candidate in candidates:
                            if candidate['state'] == 'pending':
                                candidate['state'] = 'skipped'
                        continue
                    if any(c['state'] == 'failed' and (c.get('downloaded_files') or
                           job.get('ingestions', {}).get(c['id'] + '-0')) for c in candidates):
                        continue
                    # Prefer the durable local download over a new P1/P2 request.
                    retained = [c for c in candidates if c['transport'] == 'dws'
                                and c['state'] not in ('failed', 'skipped')
                                and (c.get('downloaded_files') or job.get('ingestions', {}).get(c['id'] + '-0'))]
                    candidates = retained + [c for c in candidates if c not in retained]
                    for candidate in candidates:
                        if candidate['state'] == 'failed' and candidate['transport'] == 'browser':
                            task = next((t for t in self.state.tasks(job_id)
                                         if t['candidate_id'] == candidate['id']), None)
                            if task and task['status'] == 'complete' and task.get('staged_path'):
                                candidate['state'] = 'waiting'
                        if candidate['state'] in ('failed', 'skipped'):
                            continue
                        job['current_generation'] = candidate.get('generation', 0)
                        try:
                            recovery = job.get('ingestions', {}).get(candidate['id'])
                            if recovery and Path(recovery['path']).is_file():
                                self._ingest(job, candidate, recovery['path'], recovery['name'])
                            elif candidate['state'] == 'waiting':
                                task = next(t for t in self.state.tasks(job_id) if t['candidate_id'] == candidate['id'])
                                if task['status'] in ('pending', 'started'):
                                    break
                                if task['status'] == 'failed':
                                    raise ValueError('浏览器下载失败：' + task.get('error', '未知原因'))
                                self._ingest(job, candidate, task['staged_path'], task['name'])
                            elif candidate['transport'] == 'browser':
                                self._browser_task(job, candidate)
                                task = next(t for t in self.state.tasks(job_id) if t['candidate_id'] == candidate['id'])
                                if task['status'] in ('pending', 'started'):
                                    break
                                if task['status'] == 'failed':
                                    raise ValueError('浏览器下载失败：' + task.get('error', '未知原因'))
                                self._ingest(job, candidate, task['staged_path'], task['name'])
                            elif candidate['transport'] == 'local':
                                original = Path(candidate['local_path'])
                                name = validate_name(candidate.get('name') or original.name)
                                staged = self._stage(job, candidate) / name
                                if not staged.exists():
                                    shutil.copy2(original, staged)
                                self._ingest(job, candidate, staged, name)
                            elif candidate['transport'] == 'http':
                                self._progress(job, phase='downloading', name=candidate.get('name', ''))
                                result = download_http(candidate['url'], self._stage(job, candidate), candidate.get('name', ''))
                                self._ingest(job, candidate, result['path'], result['name'])
                            elif candidate['transport'] == 'dws':
                                self._dws_sources(job, candidate)
                            else:
                                raise ValueError('尚未支持的证据传输方式')
                            self._cleanup_redundant_downloads(job, candidate)
                            job['attempts'].append({'candidate': candidate['id'], 'status': 'done'})
                            break
                        except (ValueError, OSError, KeyError) as exc:
                            candidate['state'] = 'failed'
                            message = str(redact(str(exc)))
                            job['attempts'].append({'candidate': candidate['id'], 'status': 'failed', 'error': message})
                            job['gaps'].append(candidate.get('name', '') + '：' + message)
                            # Acquisition succeeded: another source cannot fix a
                            # local extraction/disk failure. Keep the original.
                            if candidate.get('downloaded_files'):
                                self._save(job)
                                break
                        self._save(job)
                if len(job['candidates']) == candidates_before:
                    break
            waiting = any(c['state'] == 'waiting' for c in job['candidates'])
            job['status'] = 'waiting_browser' if waiting else ('partial' if job['gaps'] else 'ready')
            self._save(job)
            return self.summary(job)

    def browser_poll(self):
        return {'tasks': [{k: t[k] for k in ('task_id','job_id','url','name','relative_path',
                                           'page_fetch', 'page_tab_id', 'page_url') if k in t}
                          for t in self.state.tasks() if t['status'] == 'pending'][:20]}

    def browser_result(self, task_id, status, download_id=None, path=None, error=None):
        # Only task rows and per-task staging are written; the job itself is
        # untouched, so no bug lock is needed while a worker may hold it.
        return self._browser_result(task_id, status, download_id, path, error)

    def _browser_result(self, task_id, status, download_id=None, path=None, error=None):
        task = self.state.task_get(task_id)
        if status not in ('started', 'complete', 'failed'):
            raise ValueError('无效的浏览器下载状态')
        # A fresh download attempt supersedes an earlier failure even if the
        # resume reset has not been applied by a worker yet.
        new_attempt = task['status'] == 'failed' and status == 'started'
        if task['status'] in ('complete', 'failed') and not new_attempt:
            return self.summary(self.state.get(task['job_id']))
        if (task.get('download_id') is not None and download_id != task['download_id']
                and not new_attempt):
            raise ValueError('浏览器下载 ID 不匹配')
        if new_attempt:
            task.pop('error', None)
        if status == 'complete':
            expected = self.downloads_root / task['relative_path']
            actual = Path(path or '').resolve()
            if actual != expected.resolve() or not actual.is_relative_to(self.downloads_root) or not actual.is_file():
                raise ValueError('文件不属于本任务已登记的浏览器下载路径')
            if expected.is_symlink() or any(p.is_symlink() for p in expected.parents if p != self.downloads_root.parent):
                raise ValueError('不接受符号链接下载路径')
            staged = self.workspace.staging_dir(task['job_id'], task['task_id']) / validate_name(task['name'])
            if staged.exists():
                if sha256_file(staged) != sha256_file(actual):
                    raise ValueError('下载暂存路径已有不同内容')
            else:
                shutil.copy2(actual, staged)
                if sha256_file(actual) != sha256_file(staged):
                    staged.unlink()
                    raise ValueError('浏览器文件交接校验失败')
            task['staged_path'] = str(staged)
            task['handoff_sha256'] = sha256_file(staged)
            # Commit the recoverable handoff before deleting the browser copy.
            task.update(status=status, download_id=download_id)
            self.state.task_put(task)
            actual.unlink()
            self._prune_browser_directories()
        task.update(status=status, download_id=download_id)
        if error:
            task['error'] = str(redact(str(error)))
        self.state.task_put(task)
        return self.summary(self.state.get(task['job_id']))

    def cancel(self, job_id):
        job = self.state.get(job_id)
        with self._bug_lock(job['bug_id'], blocking=False) as acquired:
            if not acquired:
                raise ValueError('任务正在处理中，请稍后再取消')
            return self._cancel(job_id)

    def _cancel(self, job_id):
        job = self.state.get(job_id)
        job['status'] = 'cancelled'
        job['gaps'].append('用户取消采集，以上为已取得的材料')
        for task in self.state.tasks(job_id):
            if task['status'] in ('pending', 'started'):
                task.update(status='failed', error='用户取消采集')
                self.state.task_put(task)
        if not job.get('workspace_deleted') and self.workspace._safe(Path(job['folder'])).is_dir():
            self._save(job)
        else:
            job['updated_at'] = timestamp()
            self.state.put(job)
        return self.summary(job)

    def resume(self, job_id):
        job = self.state.get(job_id)
        with self._bug_lock(job['bug_id'], blocking=False) as acquired:
            if not acquired:
                # The running worker applies the request after its current pass.
                atomic_json(self._pending_request(job_id), {'resume': True})
                return self.summary(job)
            self._pending_request(job_id).unlink(missing_ok=True)
            return self._resume(job_id)

    def _pending_request(self, job_id):
        return self.root / '.iwork/requests' / (job_id + '.json')

    def _resume(self, job_id):
        job = self.state.get(job_id)
        if job.get('workspace_deleted'):
            return self._capture(job['snapshot'], retry_failed=True)
        if not self._restore_workspace_metadata(job):
            self._retire_deleted_workspace(job)
            return self._capture(job['snapshot'], retry_failed=True)
        self._repair_log_sources(job)
        self._check_evidence(job)
        self._cleanup_browser_copies(job)
        self._cleanup_ingestions(job)
        tasks = self.state.tasks(job_id)
        active_candidates = {t['candidate_id'] for t in tasks
                             if t['status'] in ('pending', 'started', 'complete')}
        active_targets = {c['target_id'] for c in job['candidates']
                          if c['state'] == 'waiting' and c['id'] in active_candidates}
        for candidate in job['candidates']:
            if candidate['state'] == 'failed' and candidate['target_id'] not in active_targets:
                candidate['state'] = 'pending'
        for task in tasks:
            if task['status'] == 'failed':
                for candidate in job['candidates']:
                    if candidate['id'] == task['candidate_id']:
                        if candidate['state'] in ('done', 'skipped'):
                            continue
                        # Let preferred sources run before re-enqueuing the browser
                        # fallback; a recovered group file may satisfy this target.
                        preferred = any(other['target_id'] == candidate['target_id'] and
                            other['priority'] < candidate['priority'] and other['state'] == 'pending'
                            for other in job['candidates'])
                        if preferred:
                            candidate['state'] = 'pending'
                        else:
                            self._reset_browser_task(task)
                            candidate['state'] = 'waiting'
        job['status'] = 'captured'
        job['gaps'] = (list(job['snapshot']['coverage']['gaps']) +
                       job.get('evidence_gaps', []) + job.get('browser_cleanup_gaps', []) +
                       self._processing_gaps(job))
        if job['snapshot']['coverage'].get('complete') is not True and not job['snapshot']['coverage']['gaps']:
            job['gaps'].append('页面采集完整性未证实')
        self._save(job)
        return self.summary(job)

    def dispatch(self, method, params):
        if method == 'ping':
            archives = ['zip', 'tar', 'gzip']
            if shutil.which('7z'):
                archives.append('7z')
                if shutil.which('unrar'):
                    archives.append('rar')
            return {'version': __version__, 'capabilities': {'dws': self.dws.available(),
                    'ocr': bool(shutil.which('tesseract')), 'archives': archives}, 'workspace_root': str(self.root)}
        if method == 'capture':
            return self.capture(params['snapshot'], retry_failed=params.get('retry_failed', False))
        if method == 'status':
            return self.summary(self.state.get(params['job_id']))
        if method == 'list_jobs':
            return {'jobs': [self.summary(j) for j in self.state.jobs()][:50]}
        if method == 'browser_poll':
            return self.browser_poll()
        if method == 'browser_result':
            return self.browser_result(**params)
        if method == 'resume':
            return self.resume(params['job_id'])
        if method == 'cancel':
            return self.cancel(params['job_id'])
        if method == 'open':
            job = self.state.get(params['job_id'])
            if params['kind'] not in ('folder', 'report'):
                raise ValueError('不支持的打开类型')
            if params['kind'] == 'report':
                self.refresh_report(job['job_id'])
            path = job['folder'] if params['kind'] == 'folder' else str(Path(job['folder']) / 'bugreport.md')
            subprocess.Popen(['xdg-open', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {'opened': True}
        raise ValueError('未知操作：' + str(method))

    def refresh_report(self, job_id):
        job = self.state.get(job_id)
        with self._bug_lock(job['bug_id'], blocking=False) as acquired:
            if not acquired:
                return
            job = self.state.get(job_id)
            if job.get('workspace_deleted'):
                raise ValueError('旧任务的目录已删除，请选择重新采集的新任务')
            if not self._restore_workspace_metadata(job):
                raise ValueError('工单目录已删除，请点击采集当前工单或重试')
            self._merge_current(job)
            compact_single_files(self.workspace, job)
            self._save(job)
            cleanup_single_files(self.workspace, job)
            current = Path(job['folder']) / '.iwork/manifest.json'
            manifest = json.loads(current.read_text())
            manifest['layout_cleanup'] = job.get('layout_cleanup', {})
            atomic_json(current, manifest)
            self.state.put(job)

    def kick(self, job_id):
        command = [sys.executable, '-m', 'iwork_collector', '--root', str(self.root),
                   '--downloads-root', str(self.downloads_root), 'worker', '--wait-lock', job_id]
        subprocess.Popen(command, cwd=Path(__file__).resolve().parent.parent,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
