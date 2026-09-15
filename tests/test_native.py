# SPDX-License-Identifier: MPL-2.0
"""Optional real-editor acceptance. Set RUNYTE_BIN to a compatible Runyte build."""
import codecs
import errno
import fcntl
from contextlib import closing
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import sqlite3
import struct
import subprocess
import tempfile
import termios
import time
import unicodedata
import unittest

from ru_time.__main__ import configuration


CONTROL = re.compile(rb"\x1b\[([0-?]*)[ -/]*([@-~])")
PARTIAL_CONTROL = re.compile(rb"\x1b(\[[0-?]*[ -/]*)?")
COMPLETED = "(Application command completed)"


class Screen:
    """The character most recently drawn in each cell of the editor's terminal.

    Ratatui writes only the cells that changed since its previous frame, so
    fresh output cannot show what is on screen: reopening a view that is
    already visible writes nothing at all. The editor positions every write,
    so cursor moves and clears are the only controls that change the cells.
    """
    def __init__(self, rows, columns):
        self.rows, self.columns = rows, columns
        self.cells = [[" "] * columns for _ in range(rows)]
        self.row = self.column = 0
        self.pending = b""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, data):
        # A control sequence or a character may be split across reads.
        data, self.pending = self.pending + data, b""
        position = 0
        while position < len(data):
            escape = data.find(b"\x1b", position)
            self.write(self.decoder.decode(data[position:len(data) if escape < 0 else escape]))
            if escape < 0:
                break
            control = CONTROL.match(data, escape)
            if control is None:
                if PARTIAL_CONTROL.fullmatch(data, escape):
                    self.pending = data[escape:]
                    break
                position = escape + 2
                continue
            self.control(control.group(1), control.group(2))
            position = control.end()

    def write(self, text):
        for character in text:
            if character == "\r":
                self.column = 0
            elif character == "\n":
                self.row = min(self.row + 1, self.rows - 1)
            elif character >= " ":
                if self.column < self.columns:
                    self.cells[self.row][self.column] = character
                self.column += 2 if unicodedata.east_asian_width(character) in "WF" else 1

    def control(self, parameters, final):
        # Private modes (cursor visibility, keyboard flags) and colors leave cells alone.
        if not re.fullmatch(rb"[0-9;]*", parameters):
            return
        values = [int(value) if value else 0 for value in parameters.split(b";")]
        if final in (b"H", b"f"):
            row, column = (values + [0])[:2]
            self.row = min(max(row, 1), self.rows) - 1
            self.column = min(max(column, 1), self.columns) - 1
        elif final == b"J":
            start = 0 if values[0] in (2, 3) else self.row * self.columns + self.column
            for index in range(start, self.rows * self.columns):
                self.cells[index // self.columns][index % self.columns] = " "
        elif final == b"K":
            start = 0 if values[0] == 2 else self.column
            self.cells[self.row][start:] = [" "] * (self.columns - start)

    def text(self):
        return "\n".join("".join(row) for row in self.cells)


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
        self.child = self.master = self.screen = None

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
        self.output.clear()
        self.screen = Screen(32, 110)
        self.wait_for(lambda: self.sees(b" NOR "))

    def sees(self, text):
        # Strip formatting from the observed output, including escape sequences
        # split across reads. These checks identify fresh, rendered status/command
        # text. Whether something is on screen now is a question for `shows`.
        return text in re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", self.output)

    def shows(self, text):
        return text in self.screen.text()

    def read_output(self, seconds):
        if not select.select([self.master], [], [], seconds)[0]:
            return True
        try:
            chunk = os.read(self.master, 65536)
        except OSError as error:
            # Linux reports EIO when the last slave closes; macOS returns EOF.
            if error.errno != errno.EIO:
                raise
            return False
        self.output.extend(chunk)
        if self.screen is not None:
            self.screen.feed(chunk)
        del self.output[:-65536]
        return bool(chunk)

    def drain(self, seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not self.read_output(max(0, min(0.05, end - time.monotonic()))):
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
        self.test.fail(self.screen.text() if self.screen is not None else self.output[-8000:].decode(errors="replace"))

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

    def open_time(self):
        # Registration is asynchronous. A startup/restart delay cannot establish
        # that Enter will accept a registered plugin command. Observe its native
        # palette entry first, using only output from this opening of the palette.
        self.output.clear()
        self.send(b"::time")
        # The palette input replaces the feedback line, so completion drawn
        # after Enter belongs to this command and not to an earlier one.
        self.wait_for(lambda: self.sees(b"plugin.time.open") and not self.shows(COMPLETED))
        self.send(b"\r")
        # A view that is already active redraws nothing when shown again, so
        # wait for the command to finish rather than for the view alone.
        self.wait_for(lambda: self.shows("┌ Time · ") and self.shows(COMPLETED))

    def present(self, keys, marker):
        # The host refuses to show a plugin's view, prompt or document once
        # later input has changed the foreground. Send nothing else until what
        # these keys ask for is on screen.
        self.test.assertFalse(self.shows(marker), self.screen.text())
        self.send(keys)
        self.wait_for(lambda: self.shows(marker))

    def wait_for_exit(self, timeout=5):
        # Keep the PTY sink active through the final screen/terminal restoration.
        # Darwin can wait for terminal output to drain before exposing child exit.
        deadline = time.monotonic() + timeout
        while self.child.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.child.args, timeout,
                                                output=bytes(self.output))
            if not self.read_output(min(0.05, remaining)):
                # EOF may arrive just before waitpid observes termination.
                time.sleep(min(0.01, remaining))
        return self.child.returncode

    def detach(self):
        os.write(self.master, b":detach\r")
        self.test.assertEqual(self.wait_for_exit(), 0)
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
            try:
                # On failure, close the terminal sink before killing/reaping its
                # writer. An undrained master must not trap cleanup on macOS either.
                if self.master is not None:
                    os.close(self.master)
                    self.master = None
                if self.child is not None and self.child.poll() is None:
                    try:
                        os.killpg(self.child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.child.wait(timeout=3)
            finally:
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
            drain, send, wait_for, present = editor.drain, editor.send, editor.wait_for, editor.present
            database, rows, active = editor.database, editor.rows, editor.active
            editor.open_time()
            wait_for(database.exists)
            # Cancel an unanswered native prompt, then prove the next command
            # is admitted instead of leaving the plugin permanently busy.
            present(b"::time-add\r", "┌ Add task ─")
            send(b"Cancelled title\x1b")
            self.assertEqual(rows(), [])
            present(b"::time-add\r", "┌ Add task ─")
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
            present(b" =n", "┌ [remote] Note · Native task")
            send(b"iFirst line\rSecond line\x1b")
            send(b":write\r")
            def note_text():
                with closing(sqlite3.connect(database)) as db:
                    row = db.execute("SELECT text FROM notes").fetchone()
                    return row[0] if row else ""
            wait_for(lambda: "First line" in note_text() and "Second line" in note_text())
            send(b":buffer-close\r")
            editor.open_time()
            send(b"ggj")
            present(b"::time-note\r", "┌ [remote] Note · Native task")
            send(b"%d")
            send(b":write\r")
            wait_for(lambda: note_text() == "")
            send(b":buffer-close\r")
            editor.open_time()
            send(b"ggj")
            send(b"\t")
            present("Add or edit this task’s note\r".encode(), "┌ [remote] Note · Native task")
            send(b"iMenu note\x1b")
            send(b":write\r")
            wait_for(lambda: "Menu note" in note_text())
            # Stop the provider and edit the retained document while saves are
            # unavailable. Reopening after restart must preserve those edits.
            send(b":plugin-stop time\r")
            send(b"ggiRebound \x1b")
            self.assertNotIn("Rebound", note_text())
            send(b":plugin-restart time\r")
            editor.open_time()
            send(b"ggj")
            present(b"::time-note\r", "┌ [remote] Note · Native task")
            send(b":reload\r")
            # Reload starts on Cancel; Ctrl-p selects Keep local edits. Physical
            # Enter adopts the provider's baseline without discarding our text.
            send(b"\x10\r")
            send(b":write\r")
            wait_for(lambda: "Rebound Menu note" in note_text())
            send(b":buffer-close\r")
            editor.open_time()
            send(b"ggj")
            present(b"::time-delete\r", "┌ Delete task ─")
            send(b"\x1b")
            self.assertEqual(len(rows()), 1)
            present(b"::time-delete\r", "┌ Delete task ─")
            send(b"\r")
            wait_for(lambda: rows() == [])
            # Adding from Tab needs no selected row, so it works on an empty list.
            send(b"\t")
            present(b"Add task here\r", "┌ Add task ─")
            send(b"Menu task\r")
            wait_for(lambda: rows() == [("Menu task", "todo")])
            send(b":plugin-stop time\r")
            os.write(editor.master, b":quit-all!\r")
            self.assertEqual(editor.wait_for_exit(), 0)

    def test_persistent_detach_retains_timer_and_unsaved_note(self):
        with NativeEditor(self, persistent=True) as editor:
            editor.open_time()
            editor.wait_for(editor.database.exists)
            editor.present(b"::time-add\r", "┌ Add task ─")
            editor.send(b"Persistent task\r")
            editor.wait_for(lambda: editor.rows() == [("Persistent task", "todo")])
            editor.send(b"ggj\r")
            editor.wait_for(lambda: editor.active() == 1)
            editor.present(b"::time-note\r", "┌ [remote] Note · Persistent task")
            editor.send(b"iUnsaved across detach\x1b")
            # A live activity lease and a dirty provider document both remain
            # owned by the persistent host after the frontend leaves.
            editor.detach()
            self.assertEqual(editor.active(), 1)
            self.assertEqual(editor.query("SELECT text FROM notes"), [])
            editor.attach()
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
