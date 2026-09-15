# SPDX-License-Identifier: MPL-2.0
"""Vendored range semantics and the actual program's authored declaration."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest

from ru_time.compatibility import API, RUNYTE_RANGE, CAPABILITIES
from ru_time.plugin import COMMANDS
from application import ReleaseRange, _version

ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((ROOT / "tests/ranges.json").read_text())


class CompatibilityTests(unittest.TestCase):
    def test_shared_range_boundaries_and_normalization(self):
        for case in VECTORS["ranges"]:
            with self.subTest(range=case["range"]):
                if not case["valid"]:
                    with self.assertRaises(ValueError):
                        ReleaseRange(case["range"])
                    continue
                value = ReleaseRange(case["range"])
                for key, expected in [("accept", True), ("reject", False)]:
                    for version in case[key]:
                        self.assertEqual(value.contains(version), expected, version)
                if "normalized" in case:
                    self.assertEqual(value.normalized, case["normalized"])

    def test_shared_range_narrowing(self):
        for case in VECTORS["subsets"]:
            with self.subTest(configured=case["configured"], authored=case["authored"]):
                self.assertEqual(ReleaseRange(case["configured"]).is_subset_of(ReleaseRange(case["authored"])),
                                 case["expected"])

    def test_shared_malformed_host_versions(self):
        for version in VECTORS["invalid_versions"]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                _version(version)

    def test_shipped_plugin_refuses_unsupported_host_releases_before_registration(self):
        hello = json.loads((ROOT / "tests/host_handshake.json").read_text())[0]
        for host in ("0.2.99", "0.4.0", "1.0.0", "0.3.0-rc.1"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "tasks.sqlite3"
                result = subprocess.run([sys.executable, str(ROOT / "time_plugin.py"), "--database", str(database)],
                                        input=json.dumps({**hello, "host_version": host}) + "\n",
                                        cwd=directory, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "", "Unsupported hosts must receive no registration or requests")
                self.assertIn("outside " + RUNYTE_RANGE, result.stderr)
                self.assertFalse(database.exists())

    def test_generated_configuration_matches_actual_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "tasks.sqlite3"
            environment = {**os.environ, "HOME": str(root / "home")}
            for variable in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
                environment[variable] = str(root / variable.lower())
            printed = subprocess.run([sys.executable, str(ROOT / "time_plugin.py"), "--database", str(database),
                                      "--plugin-id", "acceptance-time", "--print-config"],
                                     cwd=root, env=environment, capture_output=True, text=True, check=True, timeout=5)
            config = json.loads(printed.stdout)["plugins"][0]
            self.assertEqual(config["id"], "acceptance-time")
            self.assertFalse(database.exists(), "Printing compatibility metadata must not initialize storage")
            child = subprocess.Popen([config["executable"], *config["args"]], cwd=root, env=environment,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            try:
                hello, registered = json.loads((ROOT / "tests/host_handshake.json").read_text())
                child.stdin.write((json.dumps(hello) + "\n").encode())
                child.stdin.flush()
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(5), "No registration from the shipped program")
                frame = child.stdout.readline(1_048_577)
                self.assertLessEqual(len(frame), 1_048_576)
                registration = json.loads(frame)
                self.assertEqual(registration["type"], "register")
                self.assertEqual(registration["version"], config["api"])
                self.assertEqual(registration["version"], API)
                self.assertEqual(registration["runyte"], config["runyte"])
                self.assertEqual(registration["runyte"], RUNYTE_RANGE)
                self.assertEqual(registration["required_capabilities"], config["capabilities"])
                self.assertEqual(registration["required_capabilities"], list(CAPABILITIES))
                self.assertEqual(registration["commands"], COMMANDS)
                self.assertEqual(registration["required_features"], [])
                self.assertEqual(registration["optional_features"], [])
                child.stdin.write((json.dumps(registered) + "\n").encode())
                child.stdin.flush()
                child.stdin.close()
                self.assertEqual(child.wait(timeout=5), 0, child.stderr.read().decode(errors="replace"))
                self.assertFalse(database.exists(), "Registration alone must not initialize storage")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()


if __name__ == "__main__":
    unittest.main()
