# T001: High-risk SQLite limits and commit failures lacked direct coverage

Status: resolved with regression coverage.

This is a test coverage issue, not a claim of an additional product failure. The
original suite did not exercise the supported task/note byte limits, Unicode
byte accounting, simultaneous conditional saves, or SQLite commit failures in
pause/status/save/import/recovery. These paths are central to preserving durable
data and timer state when storage fails.

Coverage uses temporary real SQLite databases, the actual configured
limits, and SQLite's authorizer to reject commits without mocking transaction
semantics. No personal configuration, data, or external services are involved.

Added tests cover the 1,000-task limit and freed capacity, maximum elapsed-time
capping, 8 MiB per-note UTF-8 byte limits, 16 MiB aggregate note accounting,
replacement/deletion capacity, simultaneous conditional saves with one winner,
and actual failed commits during pause/status/note-save/import/recovery. Recovery
also verifies that multiple imported interrupted intervals must each be resolved.

Validation: `python3 -m unittest discover -s tests -p test_storage.py -v` passes
30 tests. Maximum interval history and task-mutation rollback tests were added
with S004; this commit broadens coverage without changing runtime behavior.
