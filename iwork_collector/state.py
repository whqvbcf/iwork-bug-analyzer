"""Private SQLite job state, shared safely by native hosts and worker processes."""

import json
import os
import sqlite3
from pathlib import Path


class State:
    def __init__(self, root):
        self.directory = Path(root) / '.iwork'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / 'state.sqlite'
        self.db = sqlite3.connect(self.path, timeout=30)
        os.chmod(self.path, 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT, bug_id TEXT, data TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS browser_tasks (id TEXT PRIMARY KEY, job_id TEXT, data TEXT)')
        self.db.commit()

    def close(self):
        self.db.close()

    def put(self, job):
        self.db.execute('INSERT INTO jobs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                        (job['job_id'], job['fingerprint'], job['bug_id'], json.dumps(job, ensure_ascii=False)))
        self.db.commit()

    def get(self, job_id):
        row = self.db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('找不到采集任务')
        return json.loads(row[0])

    def find(self, digest):
        row = self.db.execute('SELECT data FROM jobs WHERE fingerprint=? ORDER BY rowid DESC LIMIT 1', (digest,)).fetchone()
        return json.loads(row[0]) if row else None

    def jobs(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT data FROM jobs ORDER BY rowid DESC')]

    def task_put(self, task):
        self.db.execute('INSERT INTO browser_tasks VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                        (task['task_id'], task['job_id'], json.dumps(task, ensure_ascii=False)))
        self.db.commit()

    def task_get(self, task_id):
        row = self.db.execute('SELECT data FROM browser_tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise ValueError('未登记的浏览器下载任务')
        return json.loads(row[0])

    def tasks(self, job_id=None):
        rows = self.db.execute('SELECT data FROM browser_tasks' + (' WHERE job_id=?' if job_id else ''),
                               (job_id,) if job_id else ())
        return [json.loads(row[0]) for row in rows]
