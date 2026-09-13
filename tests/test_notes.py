# SPDX-License-Identifier: MPL-2.0
import copy
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ru_time.plugin import TimePlugin, PluginError
from ru_time.storage import Store, StorageError
from test_plugin import Host


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tasks.sqlite3"
        self.host = Host()
        self.plugin = TimePlugin(self.path, self.host, plugin_id="my-time")
        self.plugin.ensure_worker = lambda: None
        self.addCleanup(self.plugin.close)
        self.store = self.plugin.storage()
        self.key = self.store.add("Task 猫")
        self.empty = hashlib.sha256(b"").hexdigest()
        self.context = {"provider": "notes", "key": self.key, "job": "job:1"}

    def call(self, method, **params):
        context = {**self.context, **params}
        if method in ("write.chunk", "write.commit", "write.abort"):
            context.pop("provider")
            context.pop("key")
        return self.host.resource_handlers["resource." + method](context)

    def upload(self, text, version=None):
        self.call("write.begin", mode="conditional", encoding="utf-8", bytes=len(text.encode()), expected_version=version or self.empty)
        self.call("write.chunk", upload=self.context["job"], offset=0, text=text)
        return self.call("write.commit", mode="conditional", upload=self.context["job"], expected_version=version or self.empty)

    def test_note_command_uses_stable_identity_and_configured_owner(self):
        self.plugin.invoke({"command": "open", "invocation": "h:1"})
        context = {"command": "note", "invocation": "h:2", "view": self.plugin.view,
                   "model_revision": self.plugin.revision, "rows": [self.key]}
        self.plugin.invoke(context)
        self.plugin.invoke(context)
        self.assertEqual(sum(m == "provider.register" for m, _ in self.host.calls), 1)
        method, params = self.host.calls[-1]
        self.assertEqual(method, "resource.open")
        self.assertEqual(params, {"plugin": "my-time", "provider": "notes", "key": self.key, "invocation": "h:2"})
        self.assertNotIn("note", self.store.export_data()["tasks"][0])

    def test_multiline_unicode_save_read_empty_delete_and_restart(self):
        text = "# Notes\n\n猫 🦀\tand detail\n"
        result = self.upload(text)
        self.assertEqual(result["kind"], "write_committed")
        version = result["value"]["version"]
        self.assertEqual(self.call("stat")["value"]["bytes"], len(text.encode()))
        self.assertEqual(self.call("read", version=version, offset=0, limit=128 * 1024)["value"]["text"], text)
        self.plugin.close()
        with_store = Store(self.path)
        try:
            self.assertEqual(with_store.note(self.key)[1], text)
            with_store.save_note(self.key, "", version)
            self.assertEqual(with_store.export_data()["notes"], [])
        finally:
            with_store.close()

    def test_conflicting_save_and_deleted_task_never_recreate_data(self):
        self.call("write.begin", mode="conditional", encoding="utf-8", bytes=1, expected_version=self.empty)
        self.call("write.chunk", upload="job:1", offset=0, text="x")
        self.store.save_note(self.key, "new", self.empty)
        result = self.call("write.commit", mode="conditional", upload="job:1", expected_version=self.empty)
        self.assertEqual(result["kind"], "write_rejected")
        self.assertEqual(self.store.note(self.key)[1], "new")
        self.store.delete(self.key)
        with self.assertRaises(PluginError):
            self.call("stat")
        self.assertEqual(self.store.export_data()["notes"], [])

    def test_abort_and_reconcile_cannot_revive_a_write(self):
        self.call("write.begin", mode="conditional", encoding="utf-8", bytes=1, expected_version=self.empty)
        with self.assertRaises(PluginError):
            self.call("reconcile", previous_write="job:1")
        self.call("write.abort")
        with self.assertRaises(PluginError):
            self.call("write.begin", mode="conditional", encoding="utf-8", bytes=1, expected_version=self.empty)
        self.assertEqual(self.call("reconcile", previous_write="job:1")["kind"], "reconciled")
        self.context["job"] = "job:2"
        self.upload("a")
        with self.assertRaises(PluginError):
            self.call("write.abort")
        self.assertEqual(self.store.note(self.key)[1], "a")

    def test_upload_ranges_and_utf8_boundaries(self):
        self.upload("é猫x")
        version = self.store.note(self.key)[2]
        self.assertEqual(self.call("read", version=version, offset=0, limit=3)["value"]["text"], "é")
        with self.assertRaises(PluginError):
            self.call("read", version=version, offset=1, limit=4)
        with self.assertRaises(PluginError):
            self.call("read", version=version, offset=0, limit=1)
        self.context["job"] = "job:2"
        self.call("write.begin", mode="conditional", encoding="utf-8", bytes=3, expected_version=version)
        with self.assertRaises(PluginError):
            self.call("write.chunk", upload="job:2", offset=1, text="abc")
        with self.assertRaises(PluginError):
            self.call("write.chunk", upload="job:2", offset=0, text="abcd")
        self.assertEqual(self.store.note(self.key)[1], "é猫x")

    def test_export_import_includes_notes_and_rejects_orphans_atomically(self):
        self.store.save_note(self.key, "one\ntwo\n", self.empty)
        data = self.store.export_data()
        other = Store(Path(self.tmp.name) / "other.sqlite3")
        self.addCleanup(other.close)
        broken = copy.deepcopy(data)
        broken["notes"][0]["task_id"] = "0" * 32
        with self.assertRaises(StorageError):
            other.import_data(broken)
        self.assertEqual(other.snapshot(), [])
        other.import_data(data)
        self.assertEqual(other.export_data(), data)

    def test_version_one_migration_and_import(self):
        self.plugin.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE notes")
            db.execute("PRAGMA user_version=1")
        migrated = Store(self.path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.note(self.key)[1], "")
        data = migrated.export_data()
        data.pop("notes")
        data["version"] = 1
        other = Store(Path(self.tmp.name) / "import.sqlite3")
        self.addCleanup(other.close)
        other.import_data(data)
        self.assertEqual(other.note(self.key)[1], "")
        self.assertEqual(other.db.execute("PRAGMA user_version").fetchone()[0], 2)
