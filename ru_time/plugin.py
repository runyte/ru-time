# SPDX-License-Identifier: MPL-2.0
"""Native task buffer and input surfaces; the database is authoritative."""
from collections import deque
from pathlib import Path
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))
from application import Application, PluginError
from .storage import Store, StorageError, parse_end, utc_text

CAPABILITIES = ["views", "interaction", "activity"]
COMMANDS = [
    {"name": "open", "description": "Open time tracker", "context": "workspace"},
    {"name": "add", "description": "Add a task", "context": "workspace"},
    {"name": "pause", "description": "Pause the running timer", "context": "workspace"},
    {"name": "toggle", "description": "Start or pause this task", "context": "view", "primary": True},
    {"name": "todo", "description": "Mark task todo and pause its timer", "context": "view"},
    {"name": "in-progress", "description": "Mark task in progress", "context": "view"},
    {"name": "done", "description": "Mark task done and pause its timer", "context": "view"},
    {"name": "rename", "description": "Rename this task", "context": "view"},
    {"name": "delete", "description": "Delete this task and its recorded time", "context": "view"},
    {"name": "recover", "description": "Resolve an interrupted time interval", "context": "view"},
]


def duration(milliseconds):
    seconds = max(0, milliseconds // 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def task_model(tasks):
    width = max([8] + [len(duration(t["elapsed_ms"])) for t in tasks])
    rows = []
    for task in tasks:
        marker = ">" if task["running"] else "!" if task["interrupted"] else " "
        rows.append({"id": task["id"], "text": (
            f'{task["status"]:<11}  {duration(task["elapsed_ms"]):>{width}} {marker}  {task["title"]}'),
            "role": "warning" if task["interrupted"] else "muted" if task["status"] == "done" else "ordinary"})
    return {"title": f"Time · {len(tasks)} tasks", "purpose": "list", "rows": rows,
            "status": {"text": f'{"Status":<11}  {"Time":>{width}}    Task' if tasks else "No tasks. Use Space = a to add one.",
                       "role": "heading" if tasks else "muted"}}


class TimePlugin:
    def __init__(self, database, app=None, *, store_factory=Store):
        self.app = app or Application("Time", COMMANDS, CAPABILITIES)
        self.database, self.store_factory, self.store = database, store_factory, None
        self.lock = threading.RLock()
        self.lease_lock = threading.Lock()
        self.view = self.revision = None
        self.published = None
        self.pending = {}
        self.watches = {}  # pane -> (subscription, visible)
        self.lease = None
        self.cancelled = deque(maxlen=16)
        self.last_renewal = self.last_checkpoint = 0
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.worker = None
        self.app.handlers = {c["name"]: self.invoke for c in COMMANDS}
        self.app.on_input = self.submitted
        self.app.on_event = self.event

    def storage(self):
        if self.store is None:
            try:
                self.store = self.store_factory(self.database)
            except (StorageError, OSError, sqlite3.Error) as error:
                raise PluginError("unavailable", str(error) if isinstance(error, StorageError) else "Cannot open task database") from None
        return self.store

    def ensure_worker(self):
        if self.worker is None:
            self.worker = threading.Thread(target=self.background, name="ru-time-clock", daemon=True)
            self.worker.start()
        self.wake.set()

    def selected(self, context):
        if context.get("view") != self.view or context.get("model_revision") != self.revision or self.view is None:
            raise PluginError("stale", "Task view changed; invoke the action again")
        rows = context.get("rows", [])
        if len(rows) != 1:
            raise PluginError("invalid_argument", "Select exactly one task")
        for task in self.storage().snapshot():
            if task["id"] == rows[0]:
                return task
        raise PluginError("stale", "Task no longer exists")

    def publish(self):
        if self.view is None:
            return
        model = task_model(self.storage().snapshot())
        if model == self.published:
            return
        try:
            result = self.app.publish_model(self.view, self.revision, model)
            self.revision, self.published = result["revision"], model
        except PluginError as error:
            # Model updates never roll back durable task changes. On a proven
            # revision refusal, read the view before the next explicit command.
            if error.code == "stale":
                self.revision = self.app.request("view.get", view=self.view)["revision"]
                self.published = None
            elif error.code in ("closed", "not_found"):
                self.view = self.revision = self.published = None
            raise

    def open(self, context):
        self.storage()
        if self.view is None:
            # Create a small model first. Large restored lists use the client's
            # bounded staging path rather than exceeding the 1 MiB wire frame.
            model = task_model([])
            result = self.app.request("view.create", model=model)
            self.view, self.revision, self.published = result["view"], result["revision"], model
        self.publish()
        self.app.request("pane.show", invocation=context["invocation"], view=self.view)
        pane = context.get("pane")
        if pane and pane not in self.watches and len(self.watches) < 16:
            result = self.app.subscribe([{"kind": "viewport", "view": self.view, "pane": pane}], self.observed)
            self.watches[pane] = (result["subscription"], False)
        self.ensure_worker()

    def prompt(self, context, operation, task=None, interval=None):
        if self.pending:
            raise PluginError("busy", "Finish or cancel the current time-tracker prompt")
        if operation == "delete":
            result = self.app.request("ui.confirm", invocation=context["invocation"], title="Delete task",
                                      message=f'Delete "{task["title"][:80]}" and all its recorded time?')
        elif operation == "recover":
            result = self.app.request("ui.pick", invocation=context["invocation"], title="Recover interrupted timer",
                                      choices=["Keep last checkpoint", "Set end time"])
        else:
            label = "End time (ISO date, time, timezone)" if operation == "end-time" else "Task"
            result = self.app.request("ui.prompt", invocation=context["invocation"],
                                      title={"add": "Add task", "rename": "Rename task", "end-time": "Interval started " + utc_text(interval["start_ms"]) if interval else "End time"}[operation],
                                      field={"id": "value", "label": label, "kind": "text", "required": True, "maximum_length": 512})
        self.pending[result["surface"]] = (operation, task, interval, self.storage().generation)

    def acquire(self):
        with self.lease_lock:
            if self.lease is not None:
                return
        lease = self.app.acquire_activity("Tracking task time")["lease"]
        with self.lease_lock:
            if lease in self.cancelled:
                raise PluginError("cancelled", "Time tracking was cancelled")
            self.lease = lease
            self.last_renewal = time.monotonic()

    def release_if_paused(self):
        if self.store is not None and self.store.active is not None:
            return
        with self.lease_lock:
            lease, self.lease = self.lease, None
        if lease is not None:
            self.app.release_activity(lease)

    def invoke(self, context):
        with self.lock:
            try:
                store = self.storage()
                command = context["command"]
                if command == "open":
                    self.open(context)
                    return
                if command == "add":
                    self.prompt(context, "add")
                    return
                if command == "pause":
                    store.pause()
                else:
                    task = self.selected(context)
                    key = task["id"]
                    if command in ("delete", "rename"):
                        self.prompt(context, command, task)
                        return
                    if command == "recover" or (command == "toggle" and task["interrupted"]):
                        interval = store.interrupted(key)
                        if interval is None:
                            raise PluginError("invalid_argument", "This task has no interrupted interval")
                        self.prompt(context, "recover", task, interval)
                        return
                    if command == "toggle":
                        if not task["running"]:
                            self.acquire()
                        # Cancellation stops storage independently of host IO.
                        with self.lease_lock:
                            if self.lease is None:
                                raise PluginError("cancelled", "Time tracking was cancelled")
                            store.toggle(key)
                    elif command in ("todo", "in-progress", "done"):
                        store.status(key, "in progress" if command == "in-progress" else command)
                    else:
                        raise PluginError("not_found", "Unknown time command")
                self.release_if_paused()
                self.ensure_worker()
                self.publish()
            except StorageError as error:
                self.release_if_paused()
                raise PluginError("invalid_argument", str(error)) from None
            except sqlite3.Error:
                self.release_if_paused()
                raise PluginError("unavailable", "Could not save task changes; inspect storage before continuing") from None

    def submitted(self, context):
        with self.lock:
            pending = self.pending.pop(context["surface"], None)
            if pending is None or not context["accepted"]:
                self.wake.set()
                return
            operation, task, interval, generation = pending
            try:
                store = self.storage()
                if generation != store.generation:
                    raise PluginError("stale", "Tasks changed while input was open; invoke the action again")
                values = context["values"]
                if operation == "add":
                    store.add(values["value"])
                elif operation == "rename":
                    store.rename(task["id"], values["value"])
                elif operation == "delete":
                    if values.get("confirmed") is True:
                        store.delete(task["id"])
                elif operation == "recover":
                    if values.get("choice") == "Set end time":
                        self.prompt(context, "end-time", task, interval)
                        return
                    if values.get("choice") != "Keep last checkpoint":
                        raise PluginError("invalid_argument", "Unknown recovery choice")
                    store.recover(interval["id"])
                elif operation == "end-time":
                    store.recover(interval["id"], parse_end(values["value"]))
                self.release_if_paused()
                self.publish()
                self.ensure_worker()
            except StorageError as error:
                raise PluginError("invalid_argument", str(error)) from None

    def observed(self, event, sequence, data):
        if event == "event.resync_required":
            self.app.resync(data["subscription"])
            return
        with self.lock:
            for item in data.get("sources", []):
                source, state = item["source"], item["state"]
                if source.get("view") != self.view:
                    continue
                pane = source.get("pane")
                if pane in self.watches:
                    self.watches[pane] = (self.watches[pane][0], bool(state.get("visible")))
            self.wake.set()

    def event(self, name, data):
        if name == "activity.cancel_requested":
            lease = data["lease"]
            # Never wait for the UI lock: its owner may be waiting on a host
            # response while the lease cancellation has a two-second deadline.
            with self.lease_lock:
                self.cancelled.append(lease)
                if self.lease in (None, lease):
                    if self.store is not None:
                        self.store.pause()
                    self.lease = None
            self.app.release_activity(lease)
            self.wake.set()
        elif name == "view.closed":
            with self.lock:
                if data["view"] != self.view:
                    return
                self.view = self.revision = self.published = None
                watches, self.watches = self.watches, {}
                self.pending.clear()
            for subscription, _ in watches.values():
                self.app.unsubscribe(subscription)
            self.wake.set()

    def tick(self):
        """One bounded maintenance turn; never replay a failed lease renewal."""
        with self.lock:
            if self.store is None:
                return None
            running = self.store.active is not None
            now = time.monotonic()
            if running and now - self.last_checkpoint >= 15:
                self.store.checkpoint()
                self.last_checkpoint = now
            with self.lease_lock:
                lease = self.lease
            if running and lease and now - self.last_renewal >= 300:
                try:
                    self.app.renew_activity(lease)
                except PluginError:
                    self.store.pause()
                    self.release_if_paused()
                    raise PluginError("unavailable", "Timer paused because activity protection could not be renewed") from None
                self.last_renewal = now
            visible = any(visible for _, visible in self.watches.values())
            if visible and not self.pending:
                self.publish()
            return (1 if visible and not self.pending else 15) if running else None

    def background(self):
        delay = None
        while not self.stopping.is_set():
            self.wake.wait(delay)
            self.wake.clear()
            if self.stopping.is_set():
                break
            try:
                delay = self.tick()
            except PluginError as error:
                if error.code in ("busy", "stale", "closed", "not_found"):
                    delay = 1 if self.store and self.store.active else None
                    continue
                # Losing the host or protection pauses time. The stored task
                # changes remain durable even if presentation failed.
                if self.store:
                    self.store.pause()
                try:
                    self.release_if_paused()
                except PluginError:
                    pass
                self.app._disconnect()
                return
            except (StorageError, sqlite3.Error, OSError):
                # Stop the process rather than run a timer that cannot be saved.
                self.app._disconnect()
                return

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=12)
        with self.lock:
            if self.store:
                self.store.close()

    def run(self):
        try:
            self.app.run()
        finally:
            self.close()
