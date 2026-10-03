# SPDX-License-Identifier: MPL-2.0
"""Make transport-test deadlines reliable even when a child emits bad output."""
import os
import threading
import unittest

from wire_support import JsonLineReader


class WireHarnessTests(unittest.TestCase):
    def setUp(self):
        read_fd, write_fd = os.pipe()
        self.reader = os.fdopen(read_fd, "rb", buffering=0)
        self.writer = os.fdopen(write_fd, "wb", buffering=0)
        self.addCleanup(self.reader.close)
        self.addCleanup(self.writer.close)
        self.receiver = JsonLineReader(self.reader)

    def test_partial_frame_times_out_and_can_then_be_completed(self):
        self.writer.write(b'{"value":')
        finished = threading.Event()
        outcomes = []
        def receive():
            try:
                self.receiver.receive(timeout=0.03)
            except BaseException as error:
                outcomes.append(error)
            finally:
                finished.set()
        worker = threading.Thread(target=receive, daemon=True)
        worker.start()
        try:
            self.assertTrue(finished.wait(1), "Partial frame bypassed the receive deadline")
            self.assertEqual(len(outcomes), 1)
            self.assertIsInstance(outcomes[0], AssertionError)
            self.assertIn("timed out", str(outcomes[0]))
            self.writer.write(b'1}\n')
            self.assertEqual(self.receiver.receive(), {"value": 1})
        finally:
            self.writer.close()
            worker.join(timeout=1)

    def test_coalesced_frames_are_retained_without_another_write(self):
        self.writer.write(b'{"value":1}\n{"value":2}\n')
        self.writer.close()
        self.assertEqual(self.receiver.receive(), {"value": 1})
        self.assertEqual(self.receiver.receive(), {"value": 2})

    def test_eof_inside_frame_is_reported(self):
        self.writer.write(b'{"value":')
        self.writer.close()
        with self.assertRaisesRegex(AssertionError, "exited unexpectedly"):
            self.receiver.receive()

    def test_frame_limit_is_enforced_before_waiting_for_more_bytes(self):
        self.receiver.pending.extend(b"x" * 1_048_576)
        with self.assertRaisesRegex(AssertionError, "exceeded frame limit"):
            self.receiver.receive()


if __name__ == "__main__":
    unittest.main()
