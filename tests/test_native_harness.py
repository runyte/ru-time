# SPDX-License-Identifier: MPL-2.0
"""Native-harness synchronization without requiring a Runyte installation."""
import os
import pty
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_native import NativeEditor, Screen


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
        editor.screen = Screen(3, 60)
        editor.output.extend(b'old plugin.time.open entry')
        sent, waits = [], []
        def send(keys):
            sent.append(keys)
            if keys == b'::time':
                self.assertEqual(editor.output, bytearray())
                editor.output.extend(b'CMD :time')
        def wait_for(predicate):
            waits.append(predicate)
            if len(waits) == 1:
                self.assertEqual(sent, [b'::time'])
                self.assertFalse(predicate())
                editor.output.extend(b'plugin.time\x1b[38;5;37m.open')
                self.assertTrue(predicate())
        with patch.object(editor, 'send', side_effect=send), patch.object(editor, 'wait_for', side_effect=wait_for):
            editor.open_time()
        self.assertEqual(sent, [b'::time', b'\r'])
        self.assertEqual(len(waits), 2)

    def test_opening_waits_for_its_own_completion_even_when_the_view_is_visible(self):
        editor = NativeEditor(self)
        self.addCleanup(editor.__exit__, None)
        editor.screen = Screen(3, 60)
        # The view is already active and an earlier command's completion is drawn.
        editor.screen.feed('\x1b[1;1H┌ Time · 1 tasks\x1b[3;1H::time (Application command completed)'.encode())
        sent, waits = [], []
        def send(keys):
            sent.append(keys)
        def wait_for(predicate):
            waits.append(predicate)
            if len(waits) == 1:
                # Stale completion cannot stand in for the palette frame.
                editor.output.extend(b'plugin.time.open')
                self.assertFalse(predicate())
                editor.screen.feed(b'\x1b[3;1H:time\x1b[K')
                self.assertTrue(predicate())
            else:
                self.assertEqual(sent, [b'::time', b'\r'])
                self.assertFalse(predicate())
                # Only cells that changed are redrawn: the prefix and the title stay.
                editor.screen.feed(b'\x1b[3;1H::time (Application command completed)')
                self.assertTrue(predicate())
        with patch.object(editor, 'send', side_effect=send), patch.object(editor, 'wait_for', side_effect=wait_for):
            editor.open_time()
        self.assertEqual(len(waits), 2)

    def test_presenting_sends_nothing_further_until_the_marker_is_drawn(self):
        editor = NativeEditor(self)
        self.addCleanup(editor.__exit__, None)
        editor.screen = Screen(3, 40)
        sent = []
        def wait_for(predicate):
            self.assertEqual(sent, [b'::time-add\r'])
            self.assertFalse(predicate())
            editor.screen.feed('\x1b[2;5H┌ Add task ─'.encode())
            self.assertTrue(predicate())
        with patch.object(editor, 'send', side_effect=sent.append), patch.object(editor, 'wait_for', side_effect=wait_for):
            editor.present(b'::time-add\r', '┌ Add task ─')
        self.assertEqual(sent, [b'::time-add\r'])
        # A marker already on screen would prove nothing about these keys.
        with self.assertRaises(AssertionError):
            editor.present(b'::time-add\r', '┌ Add task ─')

    def test_screen_keeps_undrawn_cells_and_joins_split_reads(self):
        screen = Screen(3, 20)
        screen.feed(b'\x1b[2J\x1b[1;1HNote \xc2\xb7 Native task\x1b[2;1Hbody')
        # A later frame rewrites only the changed cells, split mid-sequence
        # and mid-character across reads, with colors that change no cells.
        for chunk in (b'\x1b[1', b';1H\x1b[38;5;37mTime', b' \xc2', b'\xb7\x1b[?25l\x1b', b'[1;8H1 tasks    '):
            screen.feed(chunk)
        rows = lambda: [row.rstrip() for row in screen.text().splitlines()]
        self.assertEqual(rows(), ['Time · 1 tasks', 'body', ''])
        # Erasing to the end of a line; a wide character covers the cell after it.
        screen.feed(b'\x1b[2;3H\x1b[K\x1b[3;1Hwide \xe7\x95\x8cx')
        self.assertEqual(rows(), ['Time · 1 tasks', 'bo', 'wide 界 x'])

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
