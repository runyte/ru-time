# S004: Every live refresh scans all interval history

Status: resolved.

`Store.snapshot()` recomputed task aggregates from the full interval table for
every call. Live display refreshes run each second, and commands also request
snapshots. With the supported 1,000 tasks and 100,000 intervals (distributed
across tasks), repeated snapshots took about 26 ms in this Linux environment.
The work grows with historical intervals even when only the running timer changes.

Fix: cache at most the 1,000 per-task summaries until a task or interval
mutation changes the store generation. Exclude the active interval from the
cached elapsed sum, then add its live monotonic elapsed time to each returned
copy. Checkpoint writes therefore need no history re-scan. SQLite remains the
authoritative source on every mutation; returned data cannot mutate the cache.

Validation covers add/rename/status/delete/import invalidation, live checkpoints,
rolled-back timer switches and task mutations using actual denied SQLite commits,
recovery, and repeated reads at the supported interval limit. Tests assert query
behavior and exact totals, not machine-dependent timings. The targeted storage
suite passes all 21 tests.

Measured on Linux x86_64, Python 3.14.7, SQLite 3.51.2, using 25 samples after
warming each path: uncached median 25.010 ms (24.681–27.162 ms), cached median
0.0644 ms (0.0639–0.1066 ms), approximately 388 times faster for this repeated-read
workload. Initial snapshots after mutations still aggregate the database history.

Reproduce from the repository root; all data stays in a temporary directory:

```sh
python3 - <<'PY'
import platform, sqlite3, statistics, tempfile, time
from pathlib import Path
from ru_time.storage import Store
with tempfile.TemporaryDirectory() as directory:
    store = Store(Path(directory) / 'benchmark.sqlite3')
    with store.db:
        store.db.executemany('INSERT INTO tasks VALUES (?,?,?,?)',
            ((f'{i:032x}', f'Task {i}', 'todo', i) for i in range(1000)))
        store.db.executemany('INSERT INTO intervals VALUES (?,?,?,?,?,?,?)',
            ((f'{i:032x}', f'{i % 1000:032x}', i, i, 1000, i, 0)
             for i in range(100000)))
    def uncached():
        rows = [dict(row) for row in store.db.execute('''
            SELECT t.*, COALESCE(SUM(i.elapsed_ms),0) AS elapsed_ms,
                COALESCE(MAX(i.interrupted),0) AS interrupted
            FROM tasks t LEFT JOIN intervals i ON i.task_id=t.id
            GROUP BY t.id ORDER BY t.created_ms,t.rowid''')]
        for row in rows:
            row['running'] = False
        return rows
    assert uncached() == store.snapshot()
    print(platform.platform(), platform.python_version(), sqlite3.sqlite_version)
    for name, function in [('uncached', uncached), ('cached', store.snapshot)]:
        samples = []
        for _ in range(25):
            start = time.perf_counter()
            function()
            samples.append((time.perf_counter() - start) * 1000)
        print(name, 'milliseconds: min/median/max',
              min(samples), statistics.median(samples), max(samples))
    store.close()
PY
```
