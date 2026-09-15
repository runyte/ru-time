# SPDX-License-Identifier: MPL-2.0
"""CI runner: absent, renamed or skipped native tests fail acceptance."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

REQUIRED = {
    'test_native.NativeTests.test_short_commands_and_space_pause_with_native_actions',
    'test_native.NativeTests.test_persistent_detach_retains_timer_and_unsaved_note',
}


def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test


def validate_suite(suite):
    discovered = {case.id() for case in cases(suite)}
    if not REQUIRED.issubset(discovered):
        raise SystemExit('Missing required native tests: ' + ', '.join(sorted(REQUIRED - discovered)))
    return suite.countTestCases()


def validate_result(result, count):
    if count < len(REQUIRED) or not result.wasSuccessful() or result.skipped or result.testsRun != count:
        raise SystemExit('Native acceptance failed, skipped or did not execute every test')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-version')
    args = parser.parse_args()
    value = os.environ.get('RUNYTE_BIN')
    if not value:
        raise SystemExit('RUNYTE_BIN must name an executable host')
    binary = Path(value).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit('RUNYTE_BIN must name an executable host')
    result = subprocess.run([str(binary), '--version'], check=True, capture_output=True, text=True, timeout=10)
    match = re.fullmatch(r'runyte ([^\s]+)\s*', result.stdout)
    if match is None or len(result.stdout) > 256 or (args.host_version is not None and match[1] != args.host_version):
        raise SystemExit('RUNYTE_BIN does not match the declared acceptance host version')
    from ru_time.compatibility import RUNYTE_RANGE
    from vendor.application import ReleaseRange
    if not ReleaseRange(RUNYTE_RANGE).contains(match[1]):
        raise SystemExit('RUNYTE_BIN lies outside the authored support range')
    os.environ['RUNYTE_BIN'] = str(binary)
    print('Native acceptance against Runyte ' + match[1], flush=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_native.py')
    count = validate_suite(suite)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    validate_result(result, count)



if __name__ == '__main__':
    main()
