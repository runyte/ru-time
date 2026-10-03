# SPDX-License-Identifier: MPL-2.0
"""Opt-in deterministic storage state-machine check, using temporary data only."""
import argparse
from pathlib import Path
import random
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ru_time.storage import MAX_INTERVALS, MAX_TASKS, Store, StorageError


def reference_snapshot(store):
    rows = [dict(row) for row in store.db.execute("""
        SELECT t.*, COALESCE(SUM(i.elapsed_ms),0) AS elapsed_ms,
            COALESCE(MAX(i.interrupted),0) AS interrupted
        FROM tasks t LEFT JOIN intervals i ON i.task_id=t.id
        GROUP BY t.id ORDER BY t.created_ms,t.rowid
    """)]
    for row in rows:
        row["running"] = store.active is not None and store.active[0] == row["id"]
        if row["running"]:
            saved = store.db.execute("SELECT elapsed_ms FROM intervals WHERE id=?",
                                     (store.active[1],)).fetchone()[0]
            row["elapsed_ms"] += store._elapsed() - saved
    return rows


def reject_commit(action, argument, *_):
    return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION
            and argument == "COMMIT" else sqlite3.SQLITE_OK)


def exercise(steps, seed):
    generator = random.Random(seed)
    wall, monotonic = 1_800_000_000.0, 1000.0
    with tempfile.TemporaryDirectory(prefix="ru-time-storage-stress-") as directory:
        path = Path(directory) / "storage.sqlite3"
        def open_store():
            return Store(path, wall=lambda: wall, monotonic=lambda: monotonic)
        store = open_store()
        try:
            for step in range(steps):
                wall += generator.uniform(-1, 3)
                monotonic += generator.uniform(0, 3)
                tasks = store.snapshot()
                task = generator.choice(tasks) if tasks else None
                operation = generator.randrange(10) if tasks else 0
                inject_failure = generator.randrange(10) == 0
                if inject_failure:
                    store.db.set_authorizer(reject_commit)
                try:
                    if operation == 0:
                        store.add(f"Task {step}")
                    elif operation == 1:
                        store.rename(task["id"], f"Renamed {step}")
                    elif operation == 2:
                        store.toggle(task["id"])
                    elif operation == 3:
                        store.pause()
                    elif operation == 4:
                        store.status(task["id"], generator.choice(("todo", "in progress", "done")))
                    elif operation == 5:
                        store.delete(task["id"])
                    elif operation == 6:
                        store.checkpoint()
                    elif operation == 7:
                        pending = store.interrupted(task["id"])
                        if pending:
                            store.recover(pending["id"])
                    elif operation == 8:
                        store.close(interrupted=True)
                        store = open_store()
                    else:
                        store.save_note(task["id"], f"Note {step}", store.note(task["id"])[2])
                except sqlite3.DatabaseError:
                    if not inject_failure:
                        raise
                except StorageError:
                    at_task_limit = operation == 0 and len(tasks) >= MAX_TASKS
                    blocked_timer = operation == 2 and (task["interrupted"] or
                        store.db.execute("SELECT COUNT(*) FROM intervals").fetchone()[0] >= MAX_INTERVALS)
                    if not (at_task_limit or blocked_timer):
                        raise
                finally:
                    store.db.set_authorizer(None)
                if store.snapshot() != reference_snapshot(store):
                    raise AssertionError(f"Snapshot mismatch at step {step}, seed {seed}")
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=20261003)
    args = parser.parse_args()
    if args.steps < 1:
        parser.error("--steps must be positive")
    exercise(args.steps, args.seed)
    print(f"{args.steps} operations matched uncached SQLite aggregates (seed {args.seed})")


if __name__ == "__main__":
    main()
