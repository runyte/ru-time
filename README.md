# ru-time

A workspace task list and time tracker for [Runyte](https://github.com/runyte/runyte).
Python 3.10 or newer, standard library only. No pip install, service account,
network connection, or background daemon is needed. Supports Linux and macOS.

```text
Status           Time    Task
todo         00:00:00    Write release notes
in progress  01:24:36 >  Implement authentication
done         00:42:18    Fix startup crash
```

The native task buffer supports ordinary navigation, selection, search, copying,
and splits. Status and time have aligned widths; task titles retain their full
text and can use Runyte's ordinary soft wrapping. Hours can exceed 99.
`>` identifies the running task; `!` identifies an interrupted interval needing
review. Task order stays stable when status or elapsed time changes.

## Install

Use a Runyte build supporting `runyte-experimental-2`, native input, viewport
observations, and activity leases. This API is experimental; compatibility is
tested against Runyte's source revision recorded in [VENDOR.md](VENDOR.md).

```sh
git clone https://github.com/runyte/ru-time.git
cd ru-time
python3 time_plugin.py --print-config
```

The last command prints JSON, which is valid YAML, with this checkout's absolute
script path and the current interpreter. Add its plugin entry to the `plugins`
list in your Runyte configuration. Keep any other entries and settings.
The equivalent YAML is:

```yaml
plugins:
  - id: time
    enabled: true
    api: runyte-experimental-2
    executable: /absolute/path/to/python3
    args:
      - /absolute/path/to/ru-time/time_plugin.py
    capabilities: [views, interaction, activity]
    bindings:
      open: Space = =
      add: Space = a
      pause: Space = p
      delete: Space = d
```

Restart Runyte after changing configuration. An existing persistent session host
must be restarted too; `:plugin-restart time` reloads the script but keeps the
host's previously loaded configuration. A moved checkout requires regenerating
the configuration paths. The checkout is self-contained, including its Python
protocol client. It needs no Runyte source checkout beside it.

The only grants are native views, native input, and continuing activity.
The program accesses its local database directly as your user; Runyte plugins
are trusted programs, not sandboxed extensions.

## Keys and commands

Use these bindings in Normal or Select mode. `Space =` is a prefix and exposes
the available continuations through Runyte's native key hints.

| Key | Command | Behavior |
| --- | --- | --- |
| `Space = =` | `:plugin.time.open` | Open the retained task buffer in this pane |
| `Space = a` | `:plugin.time.add` | Prompt for a task title, from any document buffer |
| `Space = p` | `:plugin.time.pause` | Pause this workspace's running timer |
| `Space = d` | `:plugin.time.delete` | Confirm deletion of the selected task and its time |
| Enter, over one task | `:plugin.time.toggle` | Start or pause that task |
| Tab, in the task buffer | Native action menu | Status changes, rename, delete, recovery, and timer toggle |

Status actions are `:plugin.time.todo`, `:plugin.time.in-progress`, and
`:plugin.time.done`. Rename and recovery are `:plugin.time.rename` and
`:plugin.time.recover`. Commands retain Runyte's `plugin.<id>.` namespace;
there are no bare `:time` aliases. Bindings are configurable and Runyte refuses
collisions instead of replacing existing keys. Normal bindings do not intercept
input being sent to an integrated terminal.

Move onto a task row before pressing Enter or invoking a row action. The column
header is not a task. Row actions require exactly one selected task; a selection
across multiple tasks is refused. Adding from another buffer saves the new task
without changing focus; open the list with `Space = =` to see it.

Starting a todo or done task marks it **in progress**. Starting a different task
atomically pauses the previous task and starts the new one. Pausing leaves status
in progress. Marking a task todo or done pauses its timer; manually marking it in
progress does not start timing. Time is the sum of all intervals for the task.

Only one timer runs per workspace database. Different workspaces have independent
lists and may run timers simultaneously. There is no cross-workspace accounting
or cloud synchronization in this version.

Deletion requires physical confirmation and removes the task and all its
intervals. Escape cancels. Task changes are durable database operations; Runyte's
text undo does not undo them. Export before deleting history you want to retain.

## Timer lifetime and recovery

Switching buffers, closing the task buffer, or detaching from a persistent session
keeps the timer running. A renewable activity lease protects normal workspace
shutdown and idle retirement. Pause with `Space = p` before quitting, or cancel
the activity through Runyte's plugin manager. Cancellation pauses the timer
before releasing protection. Forced host/plugin termination can interrupt it.

Elapsed time uses Python's monotonic clock. Changing the system clock does not
add or subtract running time. Sleep/suspend behavior follows the platform's
monotonic clock; automatic idle detection and suspend correction are not provided.
UTC timestamps label intervals and allow manual recovery.

While running, the plugin checkpoints to SQLite every 15 seconds. Commands save
immediately. A clean transport EOF pauses at shutdown. After a crash, force-stop,
or restart following termination, the last checkpoint is retained and marked
interrupted. Downtime is never silently added; up to 15 seconds since the last
checkpoint may need manual recovery (long IO stalls can extend that window).

Enter on an interrupted task, or choose **Recover** from Tab. Keep the last
checkpoint, or enter an exact end time with a timezone, such as
`2026-09-12T15:30:00Z`. An explicit end time recalculates that interval using its
UTC start. Recovery never resumes a timer automatically. Multiple imported
interrupted intervals are reviewed one at a time.

The visible counter refreshes once per second while running, and updates pause
while a plugin prompt is open. Hidden timers still accumulate and checkpoint;
paused tasks need no periodic polling. Live refresh follows panes in which the
list was opened with `Space = =`. If you create a new split and hide all previously
observed panes, run `Space = =` in the new pane to enable its live refresh.
Elapsed-time accounting is unaffected.

## Storage and moving machines

Each canonical workspace root maps to a separate SQLite file under
`$XDG_DATA_HOME/ru-time/`, defaulting to `~/.local/share/ru-time/`. The filename is
the SHA-256 of that root. Task data is not stored in the plugin checkout or in
disposable `.runyte/` state. A database has one process owner; a second editor
using the same database is refused instead of interfering with its timer.

Find a workspace's database without creating it:

```sh
python3 time_plugin.py --workspace /path/to/project --print-database
```

Pause and stop the plugin (`:plugin-stop time`) before exporting, importing, or
copying its database. JSON export/import carries task IDs, statuses, and full
interval history across machines and different workspace paths:

```sh
python3 time_plugin.py --workspace /old/project --export tasks.json
python3 time_plugin.py --workspace /new/project --import tasks.json
```

Exports require a new output filename; imports require an empty destination
database. Both refuse to overwrite existing work. Import validates the complete
document before committing any task. Imported running intervals require recovery.
The export format is versioned and human-readable.

Alternatively, copy a stopped database and give its absolute path to
`--database`. Generate the corresponding configuration with:

```sh
python3 time_plugin.py --database /path/to/tasks.sqlite3 --print-config
```

Using the same explicit database for multiple workspaces makes them share a list,
but only one plugin process can open it at a time. Use the default workspace
databases for concurrent editing. Do not use live SQLite files as a cloud sync
format or place them on a filesystem without reliable local locking.

The initial limits are 1,000 tasks, 512 Unicode characters per title, and 100,000
intervals per database. Export and archive an old database before reaching these
limits. Credentials and file contents are never needed by the plugin.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The default suite uses only the standard library. It covers the storage and
controller, and drives the actual shipped process through its public JSON wire
protocol. All data is temporary. A real-editor test is opt-in:

```sh
RUNYTE_BIN=/absolute/path/to/runyte python3 -m unittest discover -s tests -p test_native.py -v
```

It launches an isolated terminal editor, exercises the configured Space prefix,
Enter, Tab status selection, pause, and deletion acceptance/cancellation, and
checks the persisted result. An optional development-only schema check uses
`jsonschema` when already installed:

```sh
RU_TIME_VALIDATE_SCHEMA=1 python3 -m unittest discover -s tests -p test_wire.py -v
```

CI runs the standard-library suite on Linux and macOS with Python 3.10 and 3.14.
The schema fixture and protocol client are pinned together; see [VENDOR.md](VENDOR.md).

License: [MPL-2.0](LICENSE).
