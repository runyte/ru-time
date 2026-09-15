# SPDX-License-Identifier: MPL-2.0
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .compatibility import API, RUNYTE_RANGE, CAPABILITIES
from .storage import Store, StorageError, default_database, load_export, write_export


def configuration():
    return {"plugins": [{"id": "time", "enabled": True, "api": API, "runyte": RUNYTE_RANGE,
                         "executable": sys.executable, "args": [str(Path(__file__).resolve().parents[1] / "time_plugin.py")],
                         "capabilities": list(CAPABILITIES),
                         "bindings": {"open": "Space = =", "add": "Space = a", "pause": "Space = p", "delete": "Space = d", "note": "Space = n"}}]}


def main():
    parser = argparse.ArgumentParser(description="Runyte task timer (Python standard library only)")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace identity (defaults to the plugin working directory)")
    parser.add_argument("--database", type=Path, help="Explicit SQLite path, useful when moving task history")
    parser.add_argument("--plugin-id", default="time", help="Configured plugin ID (default: time)")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--print-config", action="store_true", help="Print configuration with paths for this checkout and interpreter")
    actions.add_argument("--print-database", action="store_true", help="Print the resolved database path without creating it")
    actions.add_argument("--export", type=Path, metavar="FILE", help="Export JSON to a new file (stop the plugin first)")
    actions.add_argument("--import", dest="import_file", type=Path, metavar="FILE", help="Import JSON into an empty database (stop the plugin first)")
    args = parser.parse_args()
    database = (args.database or default_database(args.workspace)).expanduser().resolve()
    try:
        if args.print_config:
            config = configuration()
            config["plugins"][0]["id"] = args.plugin_id
            if args.plugin_id != "time":
                config["plugins"][0]["args"] += ["--plugin-id", args.plugin_id]
            if args.database:
                config["plugins"][0]["args"] += ["--database", str(database)]
            print(json.dumps(config, indent=2))
        elif args.print_database:
            print(database)
        elif args.export or args.import_file:
            # Read and decode an import before creating the destination database.
            data = load_export(args.import_file) if args.import_file else None
            store = Store(database)
            try:
                if args.export:
                    write_export(args.export, store.export_data())
                else:
                    store.import_data(data)
            finally:
                store.close()
        else:
            from .plugin import TimePlugin
            TimePlugin(database, plugin_id=args.plugin_id).run()
    except (StorageError, OSError, sqlite3.Error) as error:
        print(f"ru-time: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
