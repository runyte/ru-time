# SPDX-License-Identifier: MPL-2.0
"""Native-harness synchronization without requiring a Runyte installation."""
import os
import pty
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_native import NativeEditor


# The checked-in test supplies code directly to the installed interpreter;
# no executable file is generated. Output exceeds Linux and macOS PTY capacity.
WRITER = """import os, sys
remaining = memoryview(b'x' * (2 * 1024 * 1024))
while remaining:
    remaining = remaining[os.write(1, remaining):]
sys.exit(7)
"""


class NativeHarnessTests(unittest.TestCase):
    def editor(self, program):
        editor = NativeEditor(self)
        self.addCleanup(editor.__exit__, None)
        editor.master, slave = pty.openpty()
        try:
            editor.child = subprocess.Popen([sys.executable, '-c', program],
                                            stdin=slave, stdout=slave, stderr=slave,
                                            env=editor.environment, cwd=editor.project,
                                            start_new_session=True)
        finally:
            os.close(slave)
        return editor

    def test_exit_drains_output_larger_than_the_terminal_buffer_and_keeps_status(self):
        editor = self.editor(WRITER)
        self.assertEqual(editor.wait_for_exit(), 7)
        self.assertTrue(editor.output)
        self.assertLessEqual(len(editor.output), 65536)
        self.assertEqual(set(editor.output), {ord('x')})

    def test_exit_deadline_still_refuses_a_live_process(self):
        editor = self.editor("import os; os.write(1, b'waiting'); os.read(0, 1)")
        editor.wait_for(lambda: b'waiting' in editor.output)
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            editor.wait_for_exit(timeout=0.05)
        self.assertEqual(caught.exception.timeout, 0.05)
        self.assertIn(b'waiting', caught.exception.output)
        self.assertIsNone(editor.child.poll())

    def test_cleanup_closes_the_terminal_before_reaping_a_blocked_writer(self):
        editor = self.editor(WRITER)
        editor.read_output(5)
        self.assertTrue(editor.output)
        self.assertIsNone(editor.child.poll())
        original_wait = editor.child.wait
        def wait(*args, **kwargs):
            self.assertIsNone(editor.master)
            return original_wait(*args, **kwargs)
        with patch.object(editor.child, 'wait', side_effect=wait):
            editor.__exit__(None)
        self.assertIsNotNone(editor.child.poll())
        self.assertFalse(editor.root.exists())

    def test_palette_acceptance_requires_fresh_registration_evidence(self):
        editor = NativeEditor(self)
        self.addCleanup(editor.__exit__, None)
        editor.output.extend(b'old plugin.time.open entry')
        sent = []
        def send(keys):
            sent.append(keys)
            if keys == b'::time':
                self.assertEqual(editor.output, bytearray())
                editor.output.extend(b'CMD :time')
        def wait_for(predicate):
            self.assertEqual(sent, [b'::time'])
            self.assertFalse(predicate())
            editor.output.extend(b'plugin.time\x1b[38;5;37m.open')
            self.assertTrue(predicate())
        with patch.object(editor, 'send', side_effect=send), patch.object(editor, 'wait_for', side_effect=wait_for):
            editor.open_time()
        self.assertEqual(sent, [b'::time', b'\r'])

    def test_persistent_stop_is_attempted_even_if_frontend_reaping_fails(self):
        editor = NativeEditor(self, persistent=True)
        # Synthetic process failures exercise cleanup ordering without starting
        # a real detached host or changing the external acceptance deadline.
        from unittest.mock import Mock
        editor.child = Mock(pid=123, poll=Mock(return_value=None))
        editor.child.wait.side_effect = subprocess.TimeoutExpired(['fixture'], 3)
        with patch.dict(os.environ, {'RUNYTE_BIN': '/unused/runyte'}), patch('test_native.os.killpg'), patch('test_native.subprocess.run') as stop:
            stop.return_value = subprocess.CompletedProcess([], 0, '', '')
            with self.assertRaises(subprocess.TimeoutExpired):
                editor.__exit__(None)
            stop.assert_called_once()
            self.assertIn('--session-stop', stop.call_args.args[0])
        self.assertFalse(editor.root.exists())


if __name__ == '__main__':
    unittest.main()
