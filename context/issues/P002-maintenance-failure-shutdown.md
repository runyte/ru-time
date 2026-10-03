# P002: Secondary storage failures can leave a timer running without maintenance

Status: resolved.

The background handler pauses storage after a fatal host error, but that pause is
outside its storage-error handler. If saving also fails (for example, a full or
unwritable database), the worker exits without disconnecting. The host/plugin
remain active while the timer no longer checkpoints or renews its activity.
Similarly, a storage failure during activity cancellation escapes the callback
without stopping the connection, leaving time running after cancellation.

Fatal maintenance/cancellation failures must retire the transport even when the
final pause cannot be saved. The last durable checkpoint remains available for
explicit interruption recovery. No activity release should claim a successful
pause after the database rejects it.

Cancellation now disconnects if its durable pause fails. Background shutdown
also catches secondary storage failures and always retires the transport.
Regression tests inject SQLite write failures in both paths and verify transport
retirement without a false successful activity release.

Validation: `python3 -m unittest discover -s tests -p test_plugin.py -v` passes
(21 tests at this commit).
