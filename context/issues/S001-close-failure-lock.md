# S001: Failed timer shutdown retains the database owner lock

Status: resolved

`Store.close()` called `pause()` before closing the SQLite connection and owner
file. If SQLite rejected the final checkpoint (for example because storage became
read-only or full), the exception skipped both closes. Reopening the database in
the same process then failed with "already open", and shutdown leaked resources.

Reproduction: start a timer, enable SQLite `PRAGMA query_only=ON`, and call
`close()`. The operation raises `sqlite3.OperationalError` and previously left
`store.owner.closed` false.

The shutdown path now releases the connection and owner lock in `finally`
blocks while preserving the checkpoint error. The durable running interval stays
available for explicit recovery on the next open.

Validation: the regression test uses a real temporary SQLite database, confirms
that the original error propagates, both resources close, a second close is safe,
and reopening retains the last checkpoint as an interrupted interval.
