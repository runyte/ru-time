# SPDX-License-Identifier: MPL-2.0
"""Optional real-editor acceptance. Set RUNYTE_BIN to a compatible Runyte build."""
import fcntl
from contextlib import closing
import json
import os
from pathlib import Path
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import tempfile
import termios
import time
import unittest

from ru_time.__main__ import configuration


class NativeEditor:
    """A real PTY and optionally retained host, with all state below one temp root."""
    def __init__(self, test, *, persistent=False):
        self.test, self.persistent = test, persistent
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.database = self.root / "tasks.sqlite3"
        config = configuration()
        config.update(lsp={"enable": False})
        config["plugins"][0]["args"] += ["--database", str(self.database)]
        self.config = self.root / "config" / "runyte" / "config.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config))
        (self.project / "note.txt").write_text("Native plugin acceptance\n")
        # Launch an independent editor even when the test command itself runs
        # inside a Runyte terminal. Do not inherit parent attachment, tracing,
        # shared host inventory, or internal test-control environment values.
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("RUNYTE_")}
        self.environment.update(TERM="xterm-256color", HOME=str(self.root / "home"),
                                RUNYTE_ALL_HOSTS_DIR=str(self.root / "all-hosts"))
        for variable, name in [("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                               ("XDG_CONFIG_HOME", "config"), ("XDG_RUNTIME_DIR", "runtime"),
                               ("XDG_STATE_HOME", "state")]:
            self.environment[variable] = str(self.root / name)
        (self.root / "home").mkdir()
        self.output = bytearray()
        self.child = self.master = None

    def attach(self):
        self.test.assertIsNone(self.master)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
        arguments = [os.environ["RUNYTE_BIN"], "--config", str(self.config),
                     "--persistent" if self.persistent else "--init", str(self.project)]
        try:
            self.child = subprocess.Popen(arguments, cwd=self.project, env=self.environment,
                                          stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master = master

    def drain(self, seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([self.master], [], [], max(0, min(0.05, end - time.monotonic())))[0]:
                try:
                    chunk = os.read(self.master, 65536)
                    if not chunk:
                        break
                    self.output.extend(chunk)
                except OSError:
                    break
        self.test.assertIsNone(self.child.poll(), self.output[-8000:].decode(errors="replace"))

    def send(self, keys):
        os.write(self.master, keys)
        self.drain()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            self.drain(0.1)
        self.test.fail(self.output[-8000:].decode(errors="replace"))

    def query(self, sql):
        if not self.database.exists():
            return []
        with closing(sqlite3.connect(self.database)) as db:
            try:
                return db.execute(sql).fetchall()
            except sqlite3.OperationalError as error:
                if "no such table" in str(error):
                    return []
                raise

    def rows(self):
        return self.query("SELECT title,status FROM tasks ORDER BY created_ms,rowid")

    def active(self):
        return self.query("SELECT COUNT(*) FROM intervals WHERE end_ms IS NULL")[0][0]

    def detach(self):
        os.write(self.master, b":detach\r")
        self.test.assertEqual(self.child.wait(timeout=5), 0)
        os.close(self.master)
        self.master = None

    def __enter__(self):
        try:
            self.attach()
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(self, exception_type, *_):
        try:
            if self.child is not None and self.child.poll() is None:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=3)
            if self.master is not None:
                os.close(self.master)
            if self.persistent:
                # A detached host has a separate process group; explicitly stop
                # only this isolated workspace before removing its state.
                result = subprocess.run([os.environ["RUNYTE_BIN"], "--config", str(self.config),
                                         "--session-stop", str(self.project), "--force"],
                                        cwd=self.project, env=self.environment, capture_output=True,
                                        text=True, timeout=10)
                if exception_type is None:
                    self.test.assertEqual(result.returncode, 0, result.stderr)
        finally:
            self.temporary.cleanup()


@unittest.skipUnless(os.environ.get("RUNYTE_BIN"), "Set RUNYTE_BIN for real-editor acceptance")
class NativeTests(unittest.TestCase):
    def test_short_commands_and_space_pause_with_native_actions(self):
        with NativeEditor(self) as editor:
            drain, send, wait_for = editor.drain, editor.send, editor.wait_for
            database, rows, active = editor.database, editor.rows, editor.active
            master, child = editor.master, editor.child
            drain(1.5)
            send(b"::time\r")
            wait_for(database.exists)
            # Cancel an unanswered native prompt, then prove the next command
            # is admitted instead of leaving the plugin permanently busy.
            send(b"::time-add\r")
            send(b"Cancelled title\x1b")
            self.assertEqual(rows(), [])
            send(b"::time-add\r")
            send(b"Native task\r")
            wait_for(lambda: rows() == [("Native task", "todo")])
            send(b"ggj")
            send(b"\r")
            wait_for(lambda: rows() == [("Native task", "in progress")] and active() == 1)
            drain(1.1)
            send(b" =p")
            wait_for(lambda: active() == 0)
            with closing(sqlite3.connect(database)) as db:
                self.assertGreaterEqual(db.execute("SELECT elapsed_ms FROM intervals").fetchone()[0], 1000)
            send(b"\t")
            # The native action picker filters descriptions; this exercises
            # Tab's registry-derived action metadata, not only colon dispatch.
            send(b"Mark task done\r")
            wait_for(lambda: rows() == [("Native task", "done")])
            # Notes are native editable provider buffers, with durable
            # multiline saves and zero-byte deletion.
            send(b" =n")
            send(b"iFirst line\rSecond line\x1b")
            send(b":write\r")
            def note_text():
                with closing(sqlite3.connect(database)) as db:
                    row = db.execute("SELECT text FROM notes").fetchone()
                    return row[0] if row else ""
            wait_for(lambda: "First line" in note_text() and "Second line" in note_text())
            send(b":buffer-close\r")
            send(b"::time\r")
            send(b"ggj")
            send(b"::time-note\r")
            send(b"%d")
            send(b":write\r")
            wait_for(lambda: note_text() == "")
            send(b":buffer-close\r")
            send(b"::time\r")
            send(b"ggj")
            send(b"\t")
            send("Add or edit this task’s note\r".encode())
            send(b"iMenu note\x1b")
            send(b":write\r")
            wait_for(lambda: "Menu note" in note_text())
            # Stop the provider and edit the retained document while saves are
            # unavailable. Reopening after restart must preserve those edits.
            send(b":plugin-stop time\r")
            send(b"ggiRebound \x1b")
            self.assertNotIn("Rebound", note_text())
            send(b":plugin-restart time\r")
            drain(1.0)
            send(b"::time\r")
            send(b"ggj")
            send(b"::time-note\r")
            send(b":reload\r")
            # Reload starts on Cancel; Ctrl-p selects Keep local edits. Physical
            # Enter adopts the provider's baseline without discarding our text.
            send(b"\x10\r")
            send(b":write\r")
            wait_for(lambda: "Rebound Menu note" in note_text())
            send(b":buffer-close\r")
            send(b"::time\r")
            send(b"ggj")
            send(b"::time-delete\r")
            send(b"\x1b")
            self.assertEqual(len(rows()), 1)
            send(b"::time-delete\r")
            send(b"\r")
            wait_for(lambda: rows() == [])
            # Adding from Tab needs no selected row, so it works on an empty list.
            send(b"\t")
            send(b"Add task here\r")
            send(b"Menu task\r")
            wait_for(lambda: rows() == [("Menu task", "todo")])
            send(b":plugin-stop time\r")
            os.write(master, b":quit-all!\r")
            child.wait(timeout=5)

    def test_persistent_detach_retains_timer_and_unsaved_note(self):
        with NativeEditor(self, persistent=True) as editor:
            editor.drain(1.5)
            editor.send(b"::time\r")
            editor.wait_for(editor.database.exists)
            editor.send(b"::time-add\r")
            editor.send(b"Persistent task\r")
            editor.wait_for(lambda: editor.rows() == [("Persistent task", "todo")])
            editor.send(b"ggj\r")
            editor.wait_for(lambda: editor.active() == 1)
            editor.send(b"::time-note\r")
            editor.send(b"iUnsaved across detach\x1b")
            # A live activity lease and a dirty provider document both remain
            # owned by the persistent host after the frontend leaves.
            editor.detach()
            self.assertEqual(editor.active(), 1)
            self.assertEqual(editor.query("SELECT text FROM notes"), [])
            editor.attach()
            editor.drain(1.0)
            editor.send(b":write\r")
            editor.wait_for(lambda: "Unsaved across detach" in str(editor.query("SELECT text FROM notes")))
            editor.send(b"::time-pause\r")
            editor.wait_for(lambda: editor.active() == 0)
            intervals = editor.query("SELECT COUNT(*), SUM(elapsed_ms) FROM intervals")
            self.assertEqual(intervals[0][0], 1, "Reattachment must not restart the timer")
            self.assertGreater(intervals[0][1], 0)
            editor.send(b":plugin-stop time\r")
            editor.detach()


if __name__ == "__main__":
    unittest.main()
