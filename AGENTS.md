# ru-time development

ru-time is a Runyte plugin, not a Codex plugin. Keep runtime and default tests
Python 3.10+ standard-library only, with Linux/macOS support. Read README.md and
VENDOR.md before changing the protocol integration.

Use the public `runyte-experimental-2` contract. Stdout belongs exclusively to
the JSON wire protocol during plugin execution. The vendored client is pinned;
update it deliberately with its provenance and schema.

Keep storage in `ru_time/storage.py`, native integration in `ru_time/plugin.py`,
and CLI/configuration in `ru_time/__main__.py`. The database is authoritative;
publication failures must never silently roll back durable task changes. Timers
use monotonic elapsed time, and interrupted intervals require explicit recovery.
Cancellation must not wait behind a lock held during host IO.

Inspect Git status and preserve unrelated changes. Tests use temporary storage
and isolated configuration, never personal application data or caches. Retain
SPDX headers. Before handoff run:

```sh
python3 -m unittest discover -s tests -v
```

For integration changes also run the optional schema and native-editor checks
documented in README.md, recording any unavailable checks accurately.
