# SPDX-License-Identifier: MPL-2.0
"""Offline release-gate regressions; inventories use explicitly synthetic pins."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import check_hosts
import require_native


class HostInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ru-time-host-inventory-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        self.path = self.root / 'tests' / 'runyte-hosts.json'
        self.first = hashlib.sha1(b'offline-host-floor-fixture').hexdigest()
        self.second = hashlib.sha1(b'offline-host-newest-fixture').hexdigest()
        self.base = {'version': 1, 'hosts': [{
            'revision': self.first, 'host_version': '0.3.0',
            'mode': 'bootstrap-candidate', 'roles': ['oldest', 'newest'],
        }]}

    def load(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')
        return check_hosts.load(self.path)

    def test_bootstrap_runs_both_python_endpoints_and_exact_hosts_keep_the_floor(self):
        rows = check_hosts.matrix(self.load(self.base))['include']
        self.assertEqual([row['python'] for row in rows], ['3.10', '3.14'])
        self.assertEqual({row['revision'] for row in rows}, {self.first})
        self.assertTrue(all(row['mode'] == 'bootstrap-candidate' for row in rows))
        value = {'version': 1, 'hosts': [
            {'revision': self.first, 'host_version': '0.3.0+build.7', 'mode': 'exact', 'roles': ['oldest']},
            {'revision': self.second, 'host_version': '0.3.1', 'mode': 'exact', 'roles': ['newest']},
        ]}
        rows = check_hosts.matrix(self.load(value))['include']
        self.assertEqual([(row['host_version'], row['python']) for row in rows],
                         [('0.3.0+build.7', '3.10'), ('0.3.0+build.7', '3.14'), ('0.3.1', '3.14')])

    def test_missing_malformed_floating_and_duplicate_source_pins_fail(self):
        for mutate in (
            lambda value: value.update(hosts=[]),
            lambda value: value.update(version=True),
            lambda value: value['hosts'][0].pop('revision'),
            lambda value: value['hosts'][0].update(revision='main'),
            lambda value: value['hosts'][0].update(revision='abc123'),
            lambda value: value['hosts'][0].update(revision='0' * 40),
            lambda value: value['hosts'].append(copy.deepcopy(value['hosts'][0])),
        ):
            value = copy.deepcopy(self.base)
            mutate(value)
            with self.subTest(inventory=value), self.assertRaises(ValueError):
                self.load(value)
        with self.assertRaises(FileNotFoundError):
            check_hosts.load(self.root / 'missing.json')

    def test_floor_roles_ranges_and_bootstrap_modes_cannot_weaken_acceptance(self):
        for updates in (
            {'host_version': '0.3.1', 'mode': 'exact'},
            {'host_version': '0.2.99', 'mode': 'exact'},
            {'host_version': '0.4.0', 'mode': 'exact'},
            {'host_version': '0.3.0-rc.1', 'mode': 'exact'},
            {'roles': ['newest']},
            {'roles': ['oldest', 'oldest']},
            {'roles': ['convenient']},
            {'mode': 'runtime-override'},
        ):
            value = copy.deepcopy(self.base)
            value['hosts'][0].update(updates)
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.load(value)
        value = copy.deepcopy(self.base)
        value['hosts'][0]['roles'] = ['oldest']
        value['hosts'].append({'revision': self.second, 'host_version': '0.3.1', 'mode': 'exact', 'roles': ['newest']})
        with self.assertRaisesRegex(ValueError, 'bootstrap'):
            self.load(value)

    def test_duplicate_json_keys_and_oversized_inventory_fail(self):
        for content in ('{"version":1,"version":1,"hosts":[]}',
                        '{"version":1,"hosts":[{"revision":"first","revision":"second"}]}',
                        ' ' * (64 * 1024 + 1)):
            self.path.write_text(content, encoding='utf-8')
            with self.assertRaises(ValueError):
                check_hosts.load(self.path)

    def test_failed_inventory_never_emits_a_successful_workflow_output(self):
        self.path.write_text('{"version":1,"hosts":[]}', encoding='utf-8')
        output = self.root / 'github-output'
        with patch.object(check_hosts, 'ROOT', self.root), patch.object(sys, 'argv', ['check_hosts.py', '--github-output', str(output)]):
            with self.assertRaises(ValueError):
                check_hosts.main()
        self.assertFalse(output.exists())
        self.load(self.base)
        with patch.object(check_hosts, 'ROOT', self.root), patch.object(sys, 'argv', ['check_hosts.py', '--github-output', str(output)]), contextlib.redirect_stdout(io.StringIO()):
            check_hosts.main()
        name, payload = output.read_text().strip().split('=', 1)
        self.assertEqual(name, 'matrix')
        self.assertEqual(len(json.loads(payload)['include']), 2)


class NativeGateTests(unittest.TestCase):
    def suite(self, identifiers=None, *, skipped=False):
        # Synthetic in-memory tests exercise the runner without spawning a host.
        class Case(unittest.TestCase):
            def __init__(self, identifier):
                super().__init__()
                self.identifier = identifier

            def id(self):
                return self.identifier

            def runTest(self):
                if skipped:
                    self.skipTest('unavailable native dependency')
        return unittest.TestSuite(Case(identifier) for identifier in (identifiers if identifiers is not None else sorted(require_native.REQUIRED)))

    def test_exact_native_identities_are_required_even_when_test_count_is_unchanged(self):
        suite = self.suite()
        self.assertEqual(require_native.validate_suite(suite), 2)
        for identifiers in ([], ['test_native.NativeTests.test_renamed', 'test_native.NativeTests.test_other'], [next(iter(require_native.REQUIRED))]):
            with self.subTest(identifiers=identifiers), self.assertRaisesRegex(SystemExit, 'Missing required native'):
                require_native.validate_suite(self.suite(identifiers))

    def test_skipped_missing_and_failed_execution_cannot_pass_the_native_gate(self):
        for skipped in (False, True):
            suite = self.suite(skipped=skipped)
            count = require_native.validate_suite(suite)
            result = unittest.TestResult()
            suite.run(result)
            if skipped:
                with self.assertRaisesRegex(SystemExit, 'skipped'):
                    require_native.validate_result(result, count)
            else:
                require_native.validate_result(result, count)
                with self.assertRaisesRegex(SystemExit, 'did not execute'):
                    require_native.validate_result(result, count + 1)
                result.errors.append((None, 'synthetic failed native test'))
                with self.assertRaisesRegex(SystemExit, 'failed'):
                    require_native.validate_result(result, count)
        with self.assertRaises(SystemExit):
            require_native.validate_result(unittest.TestResult(), 0)


if __name__ == '__main__':
    unittest.main()
