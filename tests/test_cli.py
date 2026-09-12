# SPDX-License-Identifier: MPL-2.0
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ru_time.storage import Store, write_export

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_configuration_uses_current_checkout_and_no_database_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "time.sqlite3"
            result = subprocess.run([sys.executable, str(ROOT / "time_plugin.py"), "--database", str(database), "--print-config"],
                                    capture_output=True, text=True, check=True)
            plugin = json.loads(result.stdout)["plugins"][0]
            self.assertEqual(plugin["args"], [str(ROOT / "time_plugin.py"), "--database", str(database.resolve())])
            self.assertEqual(plugin["bindings"]["open"], "Space = =")
            self.assertFalse(database.exists())

    def test_export_import_across_different_roots_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, copied, exported = root / "original.sqlite3", root / "copy.sqlite3", root / "export.json"
            store = Store(original)
            try:
                store.add("Portable task")
            finally:
                store.close()
            def cli(*args):
                return subprocess.run([sys.executable, str(ROOT / "time_plugin.py"), *map(str, args)], capture_output=True, text=True)
            self.assertEqual(cli("--database", original, "--export", exported).returncode, 0)
            before = exported.read_bytes()
            self.assertEqual(cli("--database", original, "--export", exported).returncode, 1)
            self.assertEqual(exported.read_bytes(), before)
            self.assertEqual(cli("--database", copied, "--import", exported).returncode, 0)
            self.assertEqual(cli("--database", copied, "--import", exported).returncode, 1)
            store = Store(copied)
            try:
                self.assertEqual(store.snapshot()[0]["title"], "Portable task")
            finally:
                store.close()

    def test_failed_serialization_publishes_no_partial_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.json"
            with self.assertRaises(TypeError):
                write_export(path, {"unsupported": object()})
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
