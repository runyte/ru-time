# SPDX-License-Identifier: MPL-2.0
"""Bounded JSON-line reads for subprocess protocol tests."""
import json
import os
import selectors
import time


class JsonLineReader:
    def __init__(self, stream):
        self.stream = stream
        self.pending = bytearray()

    def receive(self, timeout=3):
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.stream, selectors.EVENT_READ)
            while True:
                newline = self.pending.find(b"\n")
                if newline >= 0:
                    line = bytes(self.pending[:newline + 1])
                    del self.pending[:newline + 1]
                    return json.loads(line)
                if len(self.pending) >= 1_048_576:
                    raise AssertionError("Plugin response exceeded frame limit")
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise AssertionError("Plugin response timed out")
                chunk = os.read(self.stream.fileno(), min(65_536, 1_048_576 - len(self.pending)))
                if not chunk:
                    raise AssertionError("Plugin exited unexpectedly")
                self.pending.extend(chunk)
