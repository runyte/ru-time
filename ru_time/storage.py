# SPDX-License-Identifier: MPL-2.0
"""Durable intervals; monotonic clocks measure running work, UTC dates label it."""
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import tempfile
import time
import uuid

STATUSES = ("todo", "in progress", "done")
MAX_TASKS = 1000
MAX_INTERVALS = 100_000
MAX_DURATION = 1_000_000_000_000  # milliseconds, about 31 years


class StorageError(ValueError):
    pass


def title_text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in value)):
        raise StorageError("Use a nonempty, single-line task title of at most 512 characters")
    return value.strip()


def default_database(workspace):
    root = str(Path(workspace).resolve())
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "ru-time" / (hashlib.sha256(root.encode()).hexdigest() + ".sqlite3")


def utc_text(milliseconds):
    return dt.datetime.fromtimestamp(milliseconds / 1000, dt.timezone.utc).isoformat(timespec="seconds")


def parse_end(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return int(parsed.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        raise StorageError("Enter an ISO date and time with timezone, for example 2026-09-12T15:30:00Z") from None


class Store:
    """One owner per database; every operation is serialized and transactional."""

    def __init__(self, path, *, wall=time.time, monotonic=time.monotonic):
        self.path = Path(path).expanduser().resolve()
        self.wall, self.monotonic = wall, monotonic
        self.lock = threading.RLock()
        self.active = None
        self.origin = 0
        self.generation = 0
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner = open(str(self.path) + ".lock", "a+b")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.owner.close()
            raise StorageError("This task database is already open in another process") from None
        try:
            self.db = sqlite3.connect(self.path, timeout=1, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise StorageError("Unsupported task database version")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('todo','in progress','done')),
                    created_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS intervals (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    start_ms INTEGER NOT NULL, checkpoint_ms INTEGER NOT NULL,
                    elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms >= 0), end_ms INTEGER,
                    interrupted INTEGER NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS one_running ON intervals((1)) WHERE end_ms IS NULL;
                CREATE INDEX IF NOT EXISTS task_intervals ON intervals(task_id);
                PRAGMA user_version=1;
            """)
            with self.db:
                self.db.execute("UPDATE intervals SET end_ms=checkpoint_ms, interrupted=1 WHERE end_ms IS NULL")
            os.chmod(self.path, 0o600)
            os.chmod(str(self.path) + ".lock", 0o600)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.owner.close()
            raise

    def now(self):
        return int(self.wall() * 1000)

    def _task(self, task_id):
        task = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise StorageError("Task no longer exists")
        return task

    def _elapsed(self):
        return min(MAX_DURATION, max(0, int((self.monotonic() - self.origin) * 1000)))

    def _checkpoint(self, finish=False):
        if self.active is not None:
            now = self.now()
            self.db.execute("UPDATE intervals SET elapsed_ms=?,checkpoint_ms=?,end_ms=? WHERE id=?",
                            (self._elapsed(), now, now if finish else None, self.active[1]))

    def checkpoint(self):
        with self.lock, self.db:
            self._checkpoint()

    def snapshot(self):
        with self.lock:
            rows = [dict(row) for row in self.db.execute("""
                SELECT t.*, COALESCE(SUM(i.elapsed_ms),0) AS elapsed_ms,
                    COALESCE(MAX(i.interrupted),0) AS interrupted
                FROM tasks t LEFT JOIN intervals i ON i.task_id=t.id
                GROUP BY t.id ORDER BY t.created_ms,t.rowid
            """)]
            for row in rows:
                row["running"] = self.active is not None and self.active[0] == row["id"]
                if row["running"]:
                    saved = self.db.execute("SELECT elapsed_ms FROM intervals WHERE id=?", (self.active[1],)).fetchone()[0]
                    row["elapsed_ms"] += self._elapsed() - saved
            return rows

    def add(self, title):
        title = title_text(title)
        with self.lock, self.db:
            if self.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= MAX_TASKS:
                raise StorageError("Task limit reached (1000)")
            key = uuid.uuid4().hex
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?)", (key, title, "todo", self.now()))
            self.generation += 1
            return key

    def rename(self, key, title):
        title = title_text(title)
        with self.lock, self.db:
            self._task(key)
            self.db.execute("UPDATE tasks SET title=? WHERE id=?", (title, key))
            self.generation += 1

    def toggle(self, key):
        with self.lock:
            self._task(key)
            if self.db.execute("SELECT 1 FROM intervals WHERE task_id=? AND interrupted=1", (key,)).fetchone():
                raise StorageError("Resolve this task's interrupted interval before starting its timer")
            if self.active is not None and self.active[0] == key:
                self.pause()
                return False
            if self.db.execute("SELECT COUNT(*) FROM intervals").fetchone()[0] >= MAX_INTERVALS:
                raise StorageError("Interval limit reached; export and archive this database")
            now, origin, interval = self.now(), self.monotonic(), uuid.uuid4().hex
            with self.db:
                self._checkpoint(finish=True)
                self.db.execute("INSERT INTO intervals VALUES (?,?,?,?,?,NULL,0)", (interval, key, now, now, 0))
                self.db.execute("UPDATE tasks SET status='in progress' WHERE id=?", (key,))
            self.active, self.origin = (key, interval), origin
            self.generation += 1
            return True

    def pause(self):
        with self.lock:
            if self.active is not None:
                with self.db:
                    self._checkpoint(finish=True)
                self.active = None
                self.generation += 1

    def status(self, key, value):
        if value not in STATUSES:
            raise StorageError("Unknown task status")
        with self.lock:
            self._task(key)
            finishing = value != "in progress" and self.active is not None and self.active[0] == key
            with self.db:
                if finishing:
                    self._checkpoint(finish=True)
                self.db.execute("UPDATE tasks SET status=? WHERE id=?", (value, key))
            if finishing:
                self.active = None
            self.generation += 1

    def delete(self, key):
        with self.lock:
            self._task(key)
            with self.db:
                self.db.execute("DELETE FROM tasks WHERE id=?", (key,))
            if self.active is not None and self.active[0] == key:
                self.active = None
            self.generation += 1

    def interrupted(self, key):
        with self.lock:
            self._task(key)
            row = self.db.execute("SELECT * FROM intervals WHERE task_id=? AND interrupted=1 ORDER BY start_ms LIMIT 1", (key,)).fetchone()
            return dict(row) if row else None

    def recover(self, interval, end_ms=None):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM intervals WHERE id=? AND interrupted=1", (interval,)).fetchone()
            if row is None:
                raise StorageError("Interrupted interval is no longer pending")
            if end_ms is None:
                end_ms, elapsed = row["checkpoint_ms"], row["elapsed_ms"]
            else:
                elapsed = end_ms - row["start_ms"]
                if not 0 <= elapsed <= MAX_DURATION or end_ms > self.now():
                    raise StorageError("End time must be between the interval start and now")
            self.db.execute("UPDATE intervals SET end_ms=?,elapsed_ms=?,interrupted=0 WHERE id=?", (end_ms, elapsed, interval))
            self.generation += 1

    def export_data(self):
        with self.lock:
            self.checkpoint()
            return {"version": 1, "tasks": [dict(r) for r in self.db.execute("SELECT * FROM tasks ORDER BY created_ms,rowid")],
                    "intervals": [dict(r) for r in self.db.execute("SELECT * FROM intervals ORDER BY start_ms,rowid")]}

    def import_data(self, data):
        """Validate everything before one commit; imports never replace existing work."""
        if not isinstance(data, dict) or set(data) != {"version", "tasks", "intervals"} or type(data["version"]) is not int or data["version"] != 1:
            raise StorageError("Unsupported export format")
        tasks, intervals = data["tasks"], data["intervals"]
        if not isinstance(tasks, list) or not isinstance(intervals, list) or len(tasks) > MAX_TASKS or len(intervals) > MAX_INTERVALS:
            raise StorageError("Invalid export limits")
        ids, interval_ids = set(), set()
        def integer(value):
            return type(value) is int and 0 <= value <= 253_402_214_400_000
        for task in tasks:
            if not isinstance(task, dict) or set(task) != {"id", "title", "status", "created_ms"}:
                raise StorageError("Invalid task record")
            key = task["id"]
            if not isinstance(key, str) or len(key) != 32 or any(c not in "0123456789abcdef" for c in key) or key in ids:
                raise StorageError("Invalid task identity")
            title_text(task["title"])
            if task["status"] not in STATUSES or not integer(task["created_ms"]):
                raise StorageError("Invalid task metadata")
            ids.add(key)
        normalized = []
        for record in intervals:
            if not isinstance(record, dict) or set(record) != {"id", "task_id", "start_ms", "checkpoint_ms", "elapsed_ms", "end_ms", "interrupted"}:
                raise StorageError("Invalid interval record")
            row = dict(record)
            key = row["id"]
            if (not isinstance(key, str) or len(key) != 32 or any(c not in "0123456789abcdef" for c in key)
                    or key in interval_ids or not isinstance(row["task_id"], str) or row["task_id"] not in ids
                    or not all(integer(row[k]) for k in ("start_ms", "checkpoint_ms", "elapsed_ms"))
                    or row["elapsed_ms"] > MAX_DURATION or type(row["interrupted"]) is not int or row["interrupted"] not in (0, 1)
                    or (row["end_ms"] is not None and not integer(row["end_ms"]))):
                raise StorageError("Invalid interval metadata")
            if row["end_ms"] is None:
                row.update(end_ms=row["checkpoint_ms"], interrupted=1)
            normalized.append(row)
            interval_ids.add(key)
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
                raise StorageError("Import requires an empty database; existing tasks were preserved")
            self.db.executemany("INSERT INTO tasks VALUES (:id,:title,:status,:created_ms)", tasks)
            self.db.executemany("INSERT INTO intervals VALUES (:id,:task_id,:start_ms,:checkpoint_ms,:elapsed_ms,:end_ms,:interrupted)", normalized)
            self.generation += 1

    def close(self, *, interrupted=False):
        with self.lock:
            if self.owner.closed:
                return
            if not interrupted:
                self.pause()
            self.db.close()
            self.owner.close()


def load_export(path):
    with open(path, "rb") as stream:
        raw = stream.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise StorageError("Export exceeds 64 MiB")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise StorageError("Invalid JSON export") from None


def write_export(path, data):
    """Publish a complete private export without replacing an existing file."""
    destination = Path(path).expanduser().resolve()
    descriptor, temporary = tempfile.mkstemp(prefix=".ru-time-export-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)
