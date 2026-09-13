# SPDX-License-Identifier: MPL-2.0
"""Optional real-editor smoke test. Set RUNYTE_BIN to a built Runyte executable."""
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


@unittest.skipUnless(os.environ.get("RUNYTE_BIN"), "Set RUNYTE_BIN for real-editor acceptance")
class NativeTests(unittest.TestCase):
    def test_short_commands_and_space_pause_with_native_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            database = root / "tasks.sqlite3"
            config = configuration()
            config.update(lsp={"enable": False})
            config["plugins"][0]["args"] += ["--database", str(database)]
            config_path = root / "config" / "runyte" / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps(config))
            (project / "note.txt").write_text("Native plugin acceptance\n")
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
            environment = {**os.environ, "TERM": "xterm-256color", "XDG_DATA_HOME": str(root / "data"),
                           "XDG_CACHE_HOME": str(root / "cache"), "XDG_CONFIG_HOME": str(root / "config"),
                           "XDG_RUNTIME_DIR": str(root / "runtime")}
            child = subprocess.Popen([os.environ["RUNYTE_BIN"], "--config", str(config_path), "--init", str(project)],
                                     cwd=project, env=environment, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            output = bytearray()
            def drain(seconds=0.3):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    if select.select([master], [], [], min(0.05, end - time.monotonic()))[0]:
                        try:
                            chunk = os.read(master, 65536)
                            output.extend(chunk)
                        except OSError:
                            break
                self.assertIsNone(child.poll(), output[-4000:].decode(errors="replace"))
            def send(keys):
                os.write(master, keys)
                drain()
            def wait_for(predicate):
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    drain(0.1)
                self.fail(output[-8000:].decode(errors="replace"))
            def rows():
                if not database.exists():
                    return []
                with closing(sqlite3.connect(database)) as db:
                    try:
                        return db.execute("SELECT title,status FROM tasks").fetchall()
                    except sqlite3.OperationalError:
                        return []
            def active():
                with closing(sqlite3.connect(database)) as db:
                    return db.execute("SELECT COUNT(*) FROM intervals WHERE end_ms IS NULL").fetchone()[0]
            try:
                drain(1.5)
                send(b"::time\r")
                wait_for(database.exists)
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
                send(b"::time-delete\r")
                send(b"\x1b")
                self.assertEqual(len(rows()), 1)
                send(b"::time-delete\r")
                send(b"\r")
                wait_for(lambda: rows() == [])
                send(b":plugin-stop time\r")
                os.write(master, b":quit-all!\r")
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=3)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
