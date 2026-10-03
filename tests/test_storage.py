# SPDX-License-Identifier: MPL-2.0
import copy
import sqlite3
import tempfile
import threading
from pathlib import Path
import unittest

from ru_time.storage import (
    MAX_DURATION, MAX_INTERVALS, MAX_NOTE_BYTES, MAX_TASKS,
    Store, StorageError, parse_end, utc_text, write_export,
)


class Clock:
    wall = 1_800_000_000
    mono = 1000

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def reject_commit(action, argument, *_):
    return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION
            and argument == "COMMIT" else sqlite3.SQLITE_OK)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = self.open(Path(self.tmp.name) / "time.sqlite3")

    def open(self, path):
        store = Store(path, wall=lambda: self.clock.wall, monotonic=lambda: self.clock.mono)
        self.addCleanup(store.close)
        return store

    def test_switch_resume_status_and_independent_intervals(self):
        first, second = self.store.add("First"), self.store.add("Second")
        self.assertTrue(self.store.toggle(first))
        self.clock.advance(20)
        self.store.toggle(second)
        self.clock.advance(10)
        self.store.toggle(first)
        self.clock.advance(5)
        self.assertFalse(self.store.toggle(first))
        rows = self.store.snapshot()
        self.assertEqual([r["elapsed_ms"] for r in rows], [25000, 10000])
        self.assertEqual([r["status"] for r in rows], ["in progress"] * 2)
        self.assertEqual(len(self.store.export_data()["intervals"]), 3)

    def test_done_pauses_and_start_reopens(self):
        key = self.store.add("Done")
        self.store.toggle(key)
        self.clock.advance(2)
        self.store.status(key, "done")
        self.clock.advance(10)
        self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], 2000)
        self.assertIsNone(self.store.active)
        self.store.toggle(key)
        self.assertEqual(self.store.snapshot()[0]["status"], "in progress")

    def test_mark_in_progress_does_not_start_timer(self):
        key = self.store.add("Waiting")
        self.store.status(key, "in progress")
        self.assertIsNone(self.store.active)

    def test_clock_jump_does_not_change_elapsed(self):
        key = self.store.add("Clock")
        self.store.toggle(key)
        self.clock.wall -= 3600
        self.clock.mono += 7
        self.store.checkpoint()
        self.clock.mono += 3
        self.store.pause()
        self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], 10000)

    def test_snapshot_reuses_history_and_keeps_live_time_after_checkpoint(self):
        key = self.store.add("Live")
        self.store.toggle(key)
        self.clock.advance(3)
        first = self.store.snapshot()
        first[0]["title"] = "Changed returned copy"
        first[0]["elapsed_ms"] = -1
        self.clock.advance(2)
        self.store.checkpoint()
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            self.clock.advance(5)
            row = self.store.snapshot()[0]
            self.assertEqual(row["title"], "Live")
            self.assertEqual(row["elapsed_ms"], 10000)
            self.assertTrue(row["running"])
            self.assertEqual(statements, [])
        finally:
            self.store.db.set_trace_callback(None)
        self.store.pause()
        self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], 10000)

    def test_snapshot_invalidates_after_mutations_and_empty_import(self):
        self.assertEqual(self.store.snapshot(), [])
        key = self.store.add("Initial")
        self.assertEqual(self.store.snapshot()[0]["title"], "Initial")
        self.store.rename(key, "Renamed")
        self.assertEqual(self.store.snapshot()[0]["title"], "Renamed")
        self.store.status(key, "done")
        self.assertEqual(self.store.snapshot()[0]["status"], "done")
        exported = self.store.export_data()
        self.store.delete(key)
        self.assertEqual(self.store.snapshot(), [])
        self.store.import_data(exported)
        self.assertEqual(self.store.snapshot()[0]["title"], "Renamed")

    def test_failed_switch_commit_preserves_active_timer_and_snapshot(self):
        first, second = self.store.add("First"), self.store.add("Second")
        self.store.toggle(first)
        self.clock.advance(3)
        self.store.checkpoint()
        self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], 3000)
        active = self.store.active
        self.store.db.set_authorizer(reject_commit)
        try:
            self.clock.advance(4)
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.toggle(second)
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(self.store.active, active)
        rows = self.store.snapshot()
        self.assertEqual([r["elapsed_ms"] for r in rows], [7000, 0])
        self.assertEqual([r["running"] for r in rows], [True, False])
        intervals = self.store.export_data()["intervals"]
        self.assertEqual(len(intervals), 1)
        self.assertIsNone(intervals[0]["end_ms"])

    def test_failed_mutation_commits_preserve_cached_and_durable_tasks(self):
        key = self.store.add("Preserved")
        expected = self.store.snapshot()
        data = self.store.export_data()
        for operation in (lambda: self.store.add("Rejected"),
                          lambda: self.store.rename(key, "Rejected"),
                          lambda: self.store.status(key, "done"),
                          lambda: self.store.delete(key)):
            self.store.db.set_authorizer(reject_commit)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    operation()
            finally:
                self.store.db.set_authorizer(None)
            self.assertEqual(self.store.snapshot(), expected)
            self.assertEqual(self.store.export_data(), data)

    def test_failed_pause_and_status_commits_preserve_running_interval(self):
        key = self.store.add("Running")
        self.store.toggle(key)
        self.clock.advance(2)
        self.store.checkpoint()
        active = self.store.active
        for operation in (self.store.pause,
                          lambda: self.store.status(key, "todo"),
                          lambda: self.store.status(key, "done")):
            self.clock.advance(1)
            self.store.db.set_authorizer(reject_commit)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    operation()
            finally:
                self.store.db.set_authorizer(None)
            self.assertEqual(self.store.active, active)
            row = self.store.snapshot()[0]
            self.assertTrue(row["running"])
            self.assertEqual(row["status"], "in progress")
            interval = self.store.db.execute("SELECT elapsed_ms,end_ms FROM intervals").fetchone()
            self.assertEqual(tuple(interval), (2000, None))
        self.store.pause()
        self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], 5000)

    def test_snapshot_and_timer_limit_with_maximum_interval_history(self):
        key = self.store.add("History")
        now = self.store.now()
        with self.store.db:
            self.store.db.executemany("INSERT INTO intervals VALUES (?,?,?,?,?,?,?)",
                ((f"{i:032x}", key, now, now, 1000, now, 0) for i in range(MAX_INTERVALS - 1)))
        self.store.toggle(key)
        self.clock.advance(2)
        self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], (MAX_INTERVALS - 1) * 1000 + 2000)
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            self.clock.advance(3)
            self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], (MAX_INTERVALS - 1) * 1000 + 5000)
            self.assertEqual(statements, [])
        finally:
            self.store.db.set_trace_callback(None)
        self.assertFalse(self.store.toggle(key))  # Pausing is allowed at the limit.
        with self.assertRaises(StorageError):
            self.store.toggle(key)
        self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], (MAX_INTERVALS - 1) * 1000 + 5000)

    def test_task_limit_allows_edits_and_reuses_deleted_capacity(self):
        with self.store.db:
            self.store.db.executemany("INSERT INTO tasks VALUES (?,?,?,?)",
                ((f"{i:032x}", f"Task {i}", "todo", i) for i in range(MAX_TASKS)))
        self.assertEqual(len(self.store.snapshot()), MAX_TASKS)
        with self.assertRaises(StorageError):
            self.store.add("Too many")
        self.store.rename("0" * 32, "Still editable")
        self.store.delete("0" * 32)
        key = self.store.add("Replacement")
        self.assertEqual(len(self.store.snapshot()), MAX_TASKS)
        self.assertEqual(self.store.note(key)[0], "Replacement")

    def test_elapsed_duration_is_capped_for_live_and_persisted_intervals(self):
        key = self.store.add("Long interval")
        self.store.toggle(key)
        self.clock.mono += MAX_DURATION // 1000 + 1
        self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], MAX_DURATION)
        self.store.pause()
        self.assertEqual(self.store.snapshot()[0]["elapsed_ms"], MAX_DURATION)

    def test_note_limit_counts_utf8_bytes_and_failed_save_preserves_content(self):
        key = self.store.add("Unicode")
        text = "é" * (MAX_NOTE_BYTES // 2)
        version = self.store.save_note(key, text, self.store.note(key)[2])
        with self.assertRaises(StorageError):
            self.store.save_note(key, text + "x", version)
        self.assertEqual(self.store.note(key)[1:], (text, version))
        for invalid in ("bad\0", "bad\ud800"):
            with self.assertRaises(StorageError):
                self.store.save_note(key, invalid, version)
        self.assertEqual(self.store.note(key)[1:], (text, version))

    def test_database_note_limit_accounts_for_replacement_and_task_deletion(self):
        first, second, third = [self.store.add(str(i)) for i in range(3)]
        text = "x" * MAX_NOTE_BYTES
        first_version = self.store.save_note(first, text, self.store.note(first)[2])
        self.store.save_note(second, text, self.store.note(second)[2])
        empty_version = self.store.note(third)[2]
        with self.assertRaises(StorageError):
            self.store.save_note(third, "🦀", empty_version)
        self.assertEqual(self.store.note(third)[1], "")
        self.store.save_note(first, text[:-4], first_version)
        version = self.store.save_note(third, "🦀", empty_version)
        with self.assertRaises(StorageError):
            self.store.save_note(third, "🦀x", version)
        self.store.delete(second)
        self.store.save_note(third, text, version)
        self.assertEqual(self.store.note(third)[1], text)

    def test_note_commit_failure_preserves_content_and_version(self):
        key = self.store.add("Atomic note")
        version = self.store.save_note(key, "Preserved", self.store.note(key)[2])
        self.store.db.set_authorizer(reject_commit)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.save_note(key, "Rejected", version)
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(self.store.note(key)[1:], ("Preserved", version))
        self.store.save_note(key, "Accepted", version)
        self.assertEqual(self.store.note(key)[1], "Accepted")

    def test_simultaneous_conditional_note_saves_have_one_winner(self):
        key = self.store.add("Concurrent note")
        version = self.store.note(key)[2]
        barrier = threading.Barrier(2)
        outcomes = []
        def save(text):
            try:
                barrier.wait(timeout=2)
                outcomes.append((text, self.store.save_note(key, text, version)))
            except Exception as error:
                outcomes.append((text, error))
        threads = [threading.Thread(target=save, args=(text,), daemon=True)
                   for text in ("First", "Second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        winners = [(text, result) for text, result in outcomes if isinstance(result, str)]
        rejected = [result for _, result in outcomes if isinstance(result, StorageError)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(self.store.note(key)[1:], winners[0])

    def test_crash_recovery_keeps_checkpoint_and_never_counts_downtime(self):
        key = self.store.add("Interrupted")
        self.store.toggle(key)
        self.clock.advance(15)
        self.store.checkpoint()
        self.clock.advance(100)
        self.store.close(interrupted=True)
        reopened = self.open(self.store.path)
        row = reopened.snapshot()[0]
        self.assertEqual(row["elapsed_ms"], 15000)
        self.assertTrue(row["interrupted"])
        with self.assertRaises(StorageError):
            reopened.toggle(key)
        reopened.recover(reopened.interrupted(key)["id"])
        reopened.toggle(key)
        self.clock.advance(5)
        reopened.pause()
        self.assertEqual(reopened.snapshot()[0]["elapsed_ms"], 20000)

    def test_recovery_can_set_exact_end_and_rejects_future(self):
        key = self.store.add("Recover")
        self.store.toggle(key)
        start = self.store.now()
        self.clock.advance(100)
        self.store.close(interrupted=True)
        reopened = self.open(self.store.path)
        interval = reopened.interrupted(key)["id"]
        self.assertTrue(reopened.snapshot()[0]["interrupted"])
        for end in (start - 1, start + 101000):
            with self.assertRaises(StorageError):
                reopened.recover(interval, end)
        reopened.recover(interval, start + 50000)
        self.assertEqual(reopened.snapshot()[0]["elapsed_ms"], 50000)
        self.assertFalse(reopened.snapshot()[0]["interrupted"])

    def test_second_owner_refused_and_lock_released_on_close(self):
        with self.assertRaises(StorageError):
            Store(self.store.path)
        self.store.close()
        self.open(self.store.path)

    def test_failed_shutdown_releases_resources_and_preserves_recovery(self):
        key = self.store.add("Failed checkpoint")
        self.store.toggle(key)
        self.clock.advance(15)
        self.store.checkpoint()
        self.clock.advance(10)
        self.store.db.execute("PRAGMA query_only=ON")
        with self.assertRaises(sqlite3.OperationalError):
            self.store.close()
        self.assertTrue(self.store.owner.closed)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.store.db.execute("SELECT 1")
        self.store.close()
        reopened = self.open(self.store.path)
        interval = reopened.interrupted(key)
        self.assertEqual(interval["elapsed_ms"], 15000)
        self.assertEqual(reopened.snapshot()[0]["elapsed_ms"], 15000)

    def test_unicode_titles_and_invalid_titles(self):
        key = self.store.add("  猫 café 🦀  ")
        self.assertEqual(self.store.snapshot()[0]["title"], "猫 café 🦀")
        for title in ("", "  ", "x\ny", "\x1b[31m", "x\u2028y", "x" * 513):
            with self.assertRaises(StorageError):
                self.store.rename(key, title)

    def test_invalid_unicode_titles_are_rejected_before_database_writes(self):
        self.store.add("Preserved")
        original = self.store.export_data()
        other = self.open(Path(self.tmp.name) / "other.sqlite3")
        for title in ("bad\ud800", "bad\udfff"):
            with self.subTest(title=repr(title)):
                with self.assertRaises(StorageError):
                    self.store.add(title)
                with self.assertRaises(StorageError):
                    self.store.rename(original["tasks"][0]["id"], title)
                self.assertEqual(self.store.export_data(), original)
                candidate = copy.deepcopy(original)
                candidate["tasks"][0]["title"] = title
                with self.assertRaises(StorageError):
                    other.import_data(candidate)
                self.assertEqual(other.snapshot(), [])

    def test_delete_running_task_removes_only_its_history(self):
        key = self.store.add("Remove")
        keep = self.store.add("Keep")
        self.store.toggle(key)
        self.store.delete(key)
        self.assertIsNone(self.store.active)
        data = self.store.export_data()
        self.assertEqual([t["id"] for t in data["tasks"]], [keep])
        self.assertEqual(data["intervals"], [])

    def test_export_import_preserves_ids_and_totals(self):
        key = self.store.add("Portable")
        self.store.toggle(key)
        self.clock.advance(12)
        self.store.pause()
        data = self.store.export_data()
        other = self.open(Path(self.tmp.name) / "other.sqlite3")
        other.import_data(data)
        self.assertEqual(data, other.export_data())
        with self.assertRaises(StorageError):
            other.import_data(data)
        self.assertEqual(data, other.export_data())

    def test_invalid_import_is_atomic(self):
        key = self.store.add("Source")
        self.store.toggle(key)
        data = self.store.export_data()
        other = self.open(Path(self.tmp.name) / "other.sqlite3")
        for mutate in (lambda d: d["intervals"][0].update(task_id="missing"),
                       lambda d: d["tasks"][0].update(title="bad\n"),
                       lambda d: d["intervals"][0].update(elapsed_ms=-1),
                       lambda d: d["intervals"].append(copy.deepcopy(d["intervals"][0]))):
            candidate = copy.deepcopy(data)
            mutate(candidate)
            with self.assertRaises(StorageError):
                other.import_data(candidate)
            self.assertEqual(other.snapshot(), [])

    def test_import_commit_failure_rolls_back_tasks_intervals_and_notes(self):
        key = self.store.add("Source")
        self.store.save_note(key, "Source note", self.store.note(key)[2])
        self.store.toggle(key)
        self.clock.advance(1)
        self.store.pause()
        data = self.store.export_data()
        other = self.open(Path(self.tmp.name) / "other.sqlite3")
        self.assertEqual(other.snapshot(), [])
        other.db.set_authorizer(reject_commit)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                other.import_data(data)
        finally:
            other.db.set_authorizer(None)
        self.assertEqual(other.export_data(), {"version": 2, "tasks": [], "intervals": [], "notes": []})
        self.assertEqual(other.snapshot(), [])
        other.import_data(data)
        self.assertEqual(other.export_data(), data)

    def test_recovery_commit_failure_and_multiple_pending_intervals(self):
        key = self.store.add("Recovery")
        data = self.store.export_data()
        now = self.store.now()
        data["intervals"] = [
            {"id": f"{i:032x}", "task_id": key, "start_ms": now - 2000,
             "checkpoint_ms": now - 1000, "elapsed_ms": 1000,
             "end_ms": now - 1000, "interrupted": 1}
            for i in range(2)]
        other = self.open(Path(self.tmp.name) / "other.sqlite3")
        other.import_data(data)
        expected = other.snapshot()
        interval = other.interrupted(key)["id"]
        other.db.set_authorizer(reject_commit)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                other.recover(interval, now)
        finally:
            other.db.set_authorizer(None)
        self.assertEqual(other.snapshot(), expected)
        self.assertEqual(other.interrupted(key)["id"], interval)
        other.recover(interval, now)
        self.assertTrue(other.snapshot()[0]["interrupted"])
        with self.assertRaises(StorageError):
            other.toggle(key)
        other.recover(other.interrupted(key)["id"])
        self.assertFalse(other.snapshot()[0]["interrupted"])
        self.assertEqual(other.snapshot()[0]["elapsed_ms"], 3000)
        self.assertTrue(other.toggle(key))

    def test_import_running_interval_requires_recovery(self):
        self.store.toggle(self.store.add("Running"))
        self.clock.advance(5)
        other = self.open(Path(self.tmp.name) / "other.sqlite3")
        other.import_data(self.store.export_data())
        self.assertTrue(other.snapshot()[0]["interrupted"])
        self.assertIsNone(other.active)

    def test_export_refuses_existing_dangling_symlink(self):
        destination = Path(self.tmp.name) / "existing.json"
        target = Path(self.tmp.name) / "missing.json"
        destination.symlink_to(target.name)
        with self.assertRaises(FileExistsError):
            write_export(destination, self.store.export_data())
        self.assertTrue(destination.is_symlink())
        self.assertEqual(destination.readlink(), Path(target.name))
        self.assertFalse(target.exists())
        self.assertEqual(list(Path(self.tmp.name).glob(".ru-time-export-*")), [])

    def test_timestamp_roundtrip_and_timezone_required(self):
        value = 1_800_000_000_000
        self.assertEqual(parse_end(utc_text(value)), value)
        with self.assertRaises(StorageError):
            parse_end("2026-09-12T12:00:00")


if __name__ == "__main__":
    unittest.main()
