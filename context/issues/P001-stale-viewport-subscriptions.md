# P001: Retired viewport subscriptions disable live timer refresh

Status: resolved.

`TimePlugin.publish()` forgets a missing/closed view without clearing its pane
subscriptions. Reopening in the same pane therefore skips subscription, and
callbacks for the old view are ignored forever. Likewise, closed pane sources
remain in the sixteen-entry watch map, so routine pane churn can exhaust live
refresh capacity. This affects presentation; stored elapsed time is unaffected.

View retirement now consistently clears/unsubscribes old pane observations on
publication, revision-refresh, display and close failures. Terminal pane sources
are removed; delayed callbacks from retired subscriptions cannot modify their
replacement. Cleanup errors cannot restore stale state or hide durable changes.

Validation: controller regression tests cover a missing view followed by reopening,
missing revision/display targets, twenty successive pane closures, and stale
callbacks. `python3 -m unittest discover -s tests -p test_plugin.py -v` passes.
