# SPDX-License-Identifier: MPL-2.0
"""Exercise the shipped program over its real bounded JSON stdio transport."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
HANDSHAKE = json.loads((ROOT / "tests/host_handshake.json").read_text())


class WireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "tasks.sqlite3"
        self.child = subprocess.Popen([sys.executable, str(ROOT / "time_plugin.py"), "--database", str(self.database)],
                                      cwd=self.tmp.name, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(self.close)
        self.validator = None
        if os.environ.get("RU_TIME_VALIDATE_SCHEMA"):
            from jsonschema import Draft202012Validator
            schema = json.loads((ROOT / "tests/runyte-experimental-2.schema.json").read_text())
            self.validator = Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/pluginMessage"})
        self.send(HANDSHAKE[0])
        self.registration = self.receive()
        self.send(HANDSHAKE[1])
        self.serial = self.model_revision = 0
        self.model = None
        self.surface = None
        self.requests = []

    def close(self):
        if self.child.poll() is None:
            self.child.stdin.close()
            try:
                self.child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=4)
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            stream.close()

    def send(self, message):
        self.child.stdin.write((json.dumps(message) + "\n").encode())
        self.child.stdin.flush()

    def receive(self, timeout=3):
        with selectors.DefaultSelector() as selector:
            selector.register(self.child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout), "Plugin response timed out")
        line = self.child.stdout.readline(1_048_577)
        self.assertTrue(line, "Plugin exited unexpectedly")
        self.assertLessEqual(len(line), 1_048_576)
        message = json.loads(line)
        if self.validator:
            self.validator.validate(message)
        return message

    def respond(self, message):
        method, params = message["method"], message["params"]
        self.requests.append(method)
        if method in ("view.create", "view.publish"):
            if method == "view.publish":
                self.assertEqual(params["expected_revision"], f"m:{self.model_revision}")
            self.model_revision += 1
            self.model = params["model"]
            result = {"view": "v:g:1", "revision": f"m:{self.model_revision}", "model": self.model}
        elif method == "event.subscribe":
            result = {"subscription": "s:g:1", "sequence": "e:0", "sources": [
                {"source": params["sources"][0], "revision": "o:0", "state": {"kind": "viewport", "visible": False, "model_revision": None, "top": None, "bottom": None}}]}
        elif method.startswith("ui."):
            self.surface = "u:g:1"
            result = {"surface": self.surface}
        elif method in ("activity.acquire", "activity.renew"):
            result = {"lease": "a:g:1", "title": "Tracking task time", "state": "active", "duration_seconds": 600}
        elif method in ("pane.show", "activity.release", "event.unsubscribe"):
            result = {}
        else:
            self.fail("Unexpected plugin request: " + method)
        self.send({"type": "response", "id": message["id"], "result": result})

    def call(self, method="command.invoke", **params):
        self.serial += 1
        request = f"h:{self.serial}"
        self.send({"type": "request", "id": request, "method": method, "params": params})
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            message = self.receive()
            if message["type"] == "request":
                self.respond(message)
            else:
                self.assertEqual(message["id"], request)
                return message
        self.fail("Command did not complete")

    def command(self, command, *, row=None, revision=None):
        return self.call(command=command, context="view" if row else "workspace", pane="p:g:1",
                         view="v:g:1" if self.model is not None else None,
                         model_revision=revision or f"m:{self.model_revision}", rows=[row] if row else [],
                         arguments={}, selection_revision="q:0", buffer="b:g:1", buffer_revision="r:0")

    def submit(self, accepted=True, **values):
        return self.call("ui.submit", surface=self.surface, accepted=accepted, values=values)

    def add(self, title):
        self.assertIn("result", self.command("add"))
        self.assertIn("result", self.submit(value=title))
        return self.model["rows"][-1]["id"]

    def test_registration_open_add_track_pause_status_delete_and_restart(self):
        self.assertEqual(self.registration["required_capabilities"], ["views", "interaction", "activity"])
        self.assertIn("result", self.command("open"))
        key = self.add("Task 猫 with a long title that exceeds thirty-two columns")
        self.assertIn("result", self.command("toggle", row=key))
        self.assertIn("activity.acquire", self.requests)
        self.assertIn(">", self.model["rows"][0]["text"])
        self.assertIn("result", self.command("pause"))
        self.assertIn("activity.release", self.requests)
        self.assertIn("result", self.command("done", row=key))
        self.assertTrue(self.model["rows"][0]["text"].startswith("done"))
        self.command("delete", row=key)
        self.submit(False)
        self.assertEqual(len(self.model["rows"]), 1)
        self.command("rename", row=key)
        self.submit(value="Renamed")
        self.assertTrue(self.model["rows"][0]["text"].endswith("Renamed"))
        self.command("delete", row=key)
        self.submit(confirmed=True)
        self.assertEqual(self.model["rows"], [])
        self.close()
        self.assertEqual(self.child.returncode, 0)

    def test_stale_command_refused_without_mutation(self):
        self.command("open")
        key = self.add("Stale")
        self.assertEqual(self.command("toggle", row=key, revision="m:0")["error"]["code"], "stale")
        self.assertNotIn("activity.acquire", self.requests)

    def test_force_stop_preserves_checkpoint_and_recovery_is_explicit(self):
        self.command("open")
        key = self.add("Interrupted")
        self.command("toggle", row=key)
        self.child.kill()
        self.child.wait(timeout=3)
        from ru_time.storage import Store
        store = Store(self.database)
        try:
            row = store.snapshot()[0]
            self.assertTrue(row["interrupted"])
            self.assertFalse(row["running"])
        finally:
            store.close()

    def test_idle_process_emits_no_unsolicited_messages(self):
        self.command("open")
        with selectors.DefaultSelector() as selector:
            selector.register(self.child.stdout, selectors.EVENT_READ)
            self.assertEqual(selector.select(0.2), [])


if __name__ == "__main__":
    unittest.main()
