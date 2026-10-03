# SPDX-License-Identifier: MPL-2.0
import copy
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from ru_time.plugin import TimePlugin, PluginError, task_model


class Host:
    def __init__(self):
        self.calls = []
        self.serial = 0
        self.models = []
        self.failure = None
        self.disconnected = False

    def _disconnect(self):
        self.disconnected = True

    def request(self, method, **params):
        self.calls.append((method, params))
        if method == self.failure:
            raise PluginError("stale", "Changed")
        self.serial += 1
        if method in ("view.create", "view.publish"):
            self.models.append(copy.deepcopy(params["model"]))
            return {"view": "v:1", "revision": str(self.serial)}
        if method == "view.get":
            return {"revision": str(self.serial)}
        if method == "resource.open":
            return {"job": "j:1", "title": "Open resource", "state": "running"}
        if method.startswith("ui."):
            return {"surface": "input:1"}
        return {}

    def subscribe(self, sources, callback):
        self.calls.append(("event.subscribe", sources))
        return {"subscription": "sub:1"}

    def publish_model(self, view, expected_revision, model):
        return self.request("view.publish", view=view, expected_revision=expected_revision, model=model)

    def unsubscribe(self, subscription):
        self.calls.append(("event.unsubscribe", subscription))

    def acquire_activity(self, title):
        self.calls.append(("activity.acquire", title))
        return {"lease": "lease:1"}

    def release_activity(self, lease):
        self.calls.append(("activity.release", lease))

    def renew_activity(self, lease):
        self.calls.append(("activity.renew", lease))


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = Host()
        self.plugin = TimePlugin(Path(self.tmp.name) / "tasks.sqlite3", self.host)
        self.plugin.ensure_worker = lambda: None
        self.addCleanup(self.plugin.close)
        self.plugin.invoke(self.context("open"))

    def context(self, command, key=None):
        return {"invocation": "h:1", "command": command, "view": self.plugin.view,
                "model_revision": self.plugin.revision, "rows": [key] if key else [], "pane": "pane:1"}

    def task(self, title="Task"):
        key = self.plugin.storage().add(title)
        self.plugin.publish()
        return key

    def submit(self, accepted=True, **values):
        self.plugin.submitted({"invocation": "h:input", "surface": "input:1", "accepted": accepted, "values": values})

    def test_open_reuses_one_buffer(self):
        self.plugin.invoke(self.context("open"))
        self.assertEqual(sum(method == "view.create" for method, _ in self.host.calls), 1)

    def test_add_cancel_and_accept_from_workspace(self):
        self.plugin.invoke(self.context("add"))
        self.submit(False)
        self.assertEqual(self.plugin.store.snapshot(), [])
        self.plugin.invoke(self.context("add"))
        self.submit(value="New task")
        self.assertEqual(self.plugin.store.snapshot()[0]["title"], "New task")

    def test_new_view_action_adds_without_a_selected_row(self):
        self.plugin.invoke(self.context("open"))
        self.plugin.invoke(self.context("new"))
        self.submit(value="From menu")
        self.assertEqual([t["title"] for t in self.plugin.store.snapshot()], ["From menu"])

    def test_stale_or_multiple_rows_cannot_start_timer(self):
        key = self.task()
        context = self.context("toggle", key)
        context["model_revision"] = "old"
        with self.assertRaises(PluginError):
            self.plugin.invoke(context)
        context = self.context("toggle", key)
        context["rows"] *= 2
        with self.assertRaises(PluginError):
            self.plugin.invoke(context)
        self.assertIsNone(self.plugin.store.active)

    def test_toggle_lease_lifetime_and_done(self):
        key = self.task()
        self.plugin.invoke(self.context("toggle", key))
        self.assertEqual(self.plugin.lease, "lease:1")
        self.plugin.invoke(self.context("done", key))
        self.assertIsNone(self.plugin.lease)
        self.assertIsNone(self.plugin.store.active)
        self.assertEqual(self.plugin.store.snapshot()[0]["status"], "done")

    def test_delete_requires_confirmation_and_captures_identity(self):
        first, second = self.task("First"), self.task("Second")
        self.plugin.invoke(self.context("delete", first))
        self.submit(False)
        self.assertEqual(len(self.plugin.store.snapshot()), 2)
        self.plugin.invoke(self.context("delete", first))
        self.submit(confirmed=True)
        self.assertEqual([t["id"] for t in self.plugin.store.snapshot()], [second])

    def test_changed_tasks_invalidate_pending_input(self):
        key = self.task()
        self.plugin.invoke(self.context("delete", key))
        self.plugin.store.rename(key, "Changed")
        with self.assertRaises(PluginError):
            self.submit(confirmed=True)
        self.assertEqual(len(self.plugin.store.snapshot()), 1)

    def test_display_failure_does_not_undo_durable_status(self):
        key = self.task()
        self.host.failure = "view.publish"
        with self.assertRaises(PluginError):
            self.plugin.invoke(self.context("done", key))
        self.assertEqual(self.plugin.store.snapshot()[0]["status"], "done")
        self.host.failure = None
        self.plugin.invoke(self.context("open"))
        self.assertIn("done", self.host.models[-1]["rows"][0]["text"])

    def test_clock_ticks_do_not_invalidate_pending_confirmation(self):
        key = self.task()
        self.plugin.invoke(self.context("toggle", key))
        self.plugin.invoke(self.context("delete", key))
        self.plugin.store.checkpoint()
        self.submit(confirmed=True)
        self.assertEqual(self.plugin.store.snapshot(), [])

    def test_idle_has_no_periodic_deadline_and_hidden_does_not_publish(self):
        self.assertIsNone(self.plugin.tick())
        key = self.task()
        self.plugin.invoke(self.context("toggle", key))
        before = len(self.host.models)
        self.assertEqual(self.plugin.tick(), 15)
        self.assertEqual(len(self.host.models), before)
        self.plugin.invoke(self.context("pause"))
        self.assertIsNone(self.plugin.tick())

    def test_failed_lease_renewal_pauses_and_is_not_replayed(self):
        key = self.task()
        self.plugin.invoke(self.context("toggle", key))
        self.plugin.last_renewal -= 301
        calls = []
        def refused(lease):
            calls.append(lease)
            raise PluginError("busy", "Unavailable")
        self.host.renew_activity = refused
        with self.assertRaises(PluginError):
            self.plugin.tick()
        self.assertIsNone(self.plugin.store.active)
        self.assertIsNone(self.plugin.tick())
        self.assertEqual(calls, ["lease:1"])

    def test_cancellation_does_not_wait_for_ui_lock(self):
        key = self.task()
        self.plugin.invoke(self.context("toggle", key))
        done = threading.Event()
        def cancel():
            self.plugin.event("activity.cancel_requested", {"lease": "lease:1"})
            done.set()
        with self.plugin.lock:
            worker = threading.Thread(target=cancel)
            worker.start()
            self.assertTrue(done.wait(1))
        worker.join()
        self.assertIsNone(self.plugin.store.active)
        self.assertIn(("activity.release", "lease:1"), self.host.calls)

    def test_early_cancellation_cannot_start_work(self):
        key = self.task()
        def acquire(title):
            self.plugin.event("activity.cancel_requested", {"lease": "early"})
            return {"lease": "early"}
        self.host.acquire_activity = acquire
        with self.assertRaises(PluginError):
            self.plugin.invoke(self.context("toggle", key))
        self.assertIsNone(self.plugin.store.active)

    def test_cancellation_save_failure_retires_connection_without_release(self):
        key = self.task()
        self.plugin.invoke(self.context("toggle", key))
        with patch.object(self.plugin.store, "pause", side_effect=sqlite3.OperationalError("disk full")):
            self.plugin.event("activity.cancel_requested", {"lease": "lease:1"})
        self.assertTrue(self.host.disconnected)
        self.assertNotIn(("activity.release", "lease:1"), self.host.calls)
        self.assertIsNotNone(self.plugin.store.active)

    def test_background_save_failure_cannot_bypass_transport_shutdown(self):
        key = self.task()
        self.plugin.invoke(self.context("toggle", key))
        self.plugin.wake.set()
        with patch.object(self.plugin, "tick", side_effect=PluginError("unavailable", "Host failed")), \
             patch.object(self.plugin.store, "pause", side_effect=sqlite3.OperationalError("disk full")):
            self.plugin.background()
        self.assertTrue(self.host.disconnected)
        self.assertNotIn(("activity.release", "lease:1"), self.host.calls)

    def test_closing_view_keeps_timer_and_reopening_uses_new_view(self):
        key = self.task()
        self.plugin.invoke(self.context("toggle", key))
        self.plugin.event("view.closed", {"view": self.plugin.view})
        self.assertIsNotNone(self.plugin.store.active)
        self.assertIsNone(self.plugin.view)
        self.plugin.invoke(self.context("open"))
        self.assertEqual(sum(method == "view.create" for method, _ in self.host.calls), 2)

    def test_missing_view_during_publish_retires_watches_before_reopening(self):
        key = self.task()
        publish = self.host.publish_model
        def missing(*args):
            raise PluginError("not_found", "View was closed")
        self.host.publish_model = missing
        with self.assertRaises(PluginError):
            self.plugin.invoke(self.context("done", key))
        self.assertEqual(self.plugin.watches, {})
        self.assertIsNone(self.plugin.view)
        self.assertIn(("event.unsubscribe", "sub:1"), self.host.calls)
        self.host.publish_model = publish
        self.plugin.invoke(self.context("open"))
        self.assertEqual(sum(method == "event.subscribe" for method, _ in self.host.calls), 2)

    def test_closed_panes_free_watch_capacity(self):
        for index in range(20):
            pane = "pane:1" if index == 0 else f"pane:{index + 1}"
            context = self.context("open")
            context["pane"] = pane
            self.plugin.invoke(context)
            self.assertIn(pane, self.plugin.watches)
            self.plugin.observed("event.closed", str(index), {
                "subscription": "sub:1", "sources": [{
                    "source": {"view": self.plugin.view, "pane": pane},
                    "state": {"kind": "closed"}}]})
            self.assertNotIn(pane, self.plugin.watches)
        self.assertEqual(sum(method == "event.unsubscribe" for method, _ in self.host.calls), 20)

    def test_missing_view_during_revision_refresh_or_show_is_retired(self):
        key = self.task()
        request = self.host.request
        for failed_method in ("view.get", "pane.show"):
            with self.subTest(method=failed_method):
                def missing(method, **params):
                    if method == failed_method:
                        raise PluginError("not_found", "View was closed")
                    if failed_method == "view.get" and method == "view.publish":
                        raise PluginError("stale", "View changed")
                    return request(method, **params)
                self.host.request = missing
                with self.assertRaises(PluginError):
                    if failed_method == "view.get":
                        self.plugin.invoke(self.context("done", key))
                    else:
                        self.plugin.invoke(self.context("open"))
                self.assertIsNone(self.plugin.view)
                self.assertEqual(self.plugin.watches, {})
                self.host.request = request
                self.plugin.invoke(self.context("open"))

    def test_retired_subscription_cannot_update_replacement_watch(self):
        self.plugin.watches["pane:1"] = ("sub:new", True)
        self.plugin.observed("event.closed", "e:1", {
            "subscription": "sub:old", "sources": [{
                "source": {"view": self.plugin.view, "pane": "pane:1"},
                "state": {"kind": "closed"}}]})
        self.assertEqual(self.plugin.watches["pane:1"], ("sub:new", True))

    def test_alignment_long_hours_and_unicode_title_not_clipped(self):
        rows = [{"id": "one", "status": "todo", "elapsed_ms": 0, "running": False, "interrupted": False, "title": "猫" * 100},
                {"id": "two", "status": "in progress", "elapsed_ms": 1000 * 3600 * 1000, "running": True, "interrupted": False, "title": "Other"}]
        model = task_model(rows)
        self.assertTrue(model["rows"][0]["text"].endswith("猫" * 100))
        self.assertEqual(model["rows"][0]["text"].index("猫"), model["rows"][1]["text"].index("Other"))


if __name__ == "__main__":
    unittest.main()
