# SPDX-License-Identifier: MPL-2.0
"""Versioned SQLite notes exposed as ordinary editable Runyte documents."""
from collections import OrderedDict
import sqlite3
import threading

from .plugin import PluginError
from .storage import MAX_NOTE_BYTES, StorageError


class Notes:
    def __init__(self, app, storage, plugin_id):
        self.app, self.storage, self.plugin_id = app, storage, plugin_id
        self.lock = threading.RLock()
        self.registered = False
        self.staging = {}
        self.settled = OrderedDict()
        app.resource_handlers = {"resource." + name: self.guard(handler) for name, handler in {
            "stat": self.stat, "read": self.read, "reconcile": self.reconcile,
            "write.begin": self.begin, "write.chunk": self.chunk,
            "write.commit": self.commit, "write.abort": self.abort,
        }.items()}

    def guard(self, handler):
        def call(context):
            with self.lock:
                if "provider" in context and context["provider"] != "notes":
                    raise PluginError("not_found", "Unknown note provider")
                try:
                    return handler(context)
                except StorageError as error:
                    raise PluginError("conflict", str(error)) from None
                except sqlite3.Error:
                    raise PluginError("unavailable", "Could not access task notes") from None
        return call

    def open(self, context, key):
        # Control requests run outside the resource lock: the host may call
        # stat/read before acknowledging resource.open.
        if not self.registered:
            self.app.request("provider.register", name="notes", conditional_write=True, atomic_replace=True)
            self.registered = True
        result = self.app.request("resource.open", plugin=self.plugin_id, provider="notes", key=key,
                                  invocation=context["invocation"])
        return {"job": result["job"]}

    def content(self, context):
        return self.storage().note(context["key"])

    def metadata(self, context):
        title, text, version = self.content(context)
        label = ("Note · " + title).encode("utf-8")[:157].decode("utf-8", errors="ignore")
        return {"key": context["key"], "label": label, "syntax_hint": "markdown",
                "version": version, "encoding": "utf-8", "bytes": len(text.encode("utf-8"))}

    def stat(self, context):
        return {"kind": "stat", "value": self.metadata(context)}

    def read(self, context):
        _, text, version = self.content(context)
        if context["version"] != version:
            raise PluginError("stale", "Note changed while reading")
        data = text.encode("utf-8")
        offset, limit = context["offset"], context["limit"]
        if not 0 <= offset <= len(data) or not 1 <= limit <= 128 * 1024:
            raise PluginError("invalid_argument", "Invalid note range")
        end = min(len(data), offset + limit)
        while end < len(data) and data[end] & 0xc0 == 0x80:
            end -= 1
        try:
            chunk = data[offset:end].decode("utf-8")
        except UnicodeError:
            raise PluginError("invalid_argument", "Range splits UTF-8") from None
        if end == offset and end != len(data):
            raise PluginError("invalid_argument", "Range is too small for UTF-8")
        return {"kind": "read", "value": {"version": version, "offset": offset, "text": chunk, "eof": end == len(data)}}

    def remember(self, job, outcome):
        self.settled[job] = outcome
        while len(self.settled) > 128:
            self.settled.popitem(last=False)

    def begin(self, context):
        _, _, version = self.content(context)
        job = context["job"]
        if context["mode"] != "conditional" or context["encoding"] != "utf-8":
            raise PluginError("invalid_argument", "Unsupported note write")
        if job in self.staging or job in self.settled:
            raise PluginError("conflict", "Write already known")
        if len(self.staging) >= 2:
            raise PluginError("busy", "Two note saves are already staged")
        if not 0 <= context["bytes"] <= MAX_NOTE_BYTES:
            raise PluginError("limit_exceeded", "Note exceeds 8 MiB")
        if context["expected_version"] != version:
            raise PluginError("conflict", "Note changed; reload before saving")
        self.staging[job] = {"key": context["key"], "version": version, "bytes": context["bytes"], "data": bytearray()}
        return {"kind": "write_started", "value": {"upload": job}}

    def upload(self, context):
        item = self.staging.get(context["job"])
        if item is None or context["upload"] != context["job"]:
            raise PluginError("not_found", "Unknown note upload")
        return item

    def chunk(self, context):
        item = self.upload(context)
        data = context["text"].encode("utf-8")
        if len(data) > 128 * 1024 or context["offset"] != len(item["data"]) or len(item["data"]) + len(data) > item["bytes"]:
            raise PluginError("invalid_argument", "Invalid note upload range")
        item["data"].extend(data)
        return {"kind": "write_chunk", "value": {"offset": len(item["data"])}}

    def commit(self, context):
        item = self.upload(context)
        if context["mode"] != "conditional" or context["expected_version"] != item["version"] or len(item["data"]) != item["bytes"]:
            raise PluginError("conflict", "Note upload changed or is incomplete")
        try:
            version = self.storage().save_note(item["key"], item["data"].decode("utf-8"), item["version"])
        except StorageError as error:
            self.staging.pop(context["job"])
            self.remember(context["job"], "rejected")
            return {"kind": "write_rejected", "value": {"error": {"code": "conflict", "message": str(error)}}}
        self.staging.pop(context["job"])
        self.remember(context["job"], "committed")
        return {"kind": "write_committed", "value": {"version": version}}

    def abort(self, context):
        if self.settled.get(context["job"]) == "committed":
            raise PluginError("outcome_unknown", "Note already committed")
        self.staging.pop(context["job"], None)
        self.remember(context["job"], "aborted")
        return {"kind": "write_aborted", "value": {}}

    def reconcile(self, context):
        if context["previous_write"] in self.staging:
            raise PluginError("outcome_unknown", "Note save is still staged")
        # SQLite commits are synchronous under the same lock. No worker or
        # external server can finish an old write after this response.
        return {"kind": "reconciled", "value": {"metadata": self.metadata(context), "previous_write": context["previous_write"]}}
