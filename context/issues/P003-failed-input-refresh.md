# P003: Failed prompt submissions leave live counters suspended

Status: resolved.

Input surfaces suspend live refresh, so the worker waits up to fifteen seconds
between maintenance turns. Accepted submissions remove their pending surface,
but validation, staleness, storage or publication exceptions skip the worker
wakeup. The counter therefore appears stuck even though its prompt is gone.
SQLite failures also escape the input handler as generic internal errors rather
than the unavailable-storage response used by ordinary commands.

Wake maintenance whenever an accepted surface finishes, including all failure
paths, and translate SQLite errors consistently without undoing durable changes.

The input handler now wakes maintenance in `finally` and reports SQLite failures
as unavailable storage. Regression tests cover invalid input, stale generation,
SQLite failure and publication refusal, verifying pending-surface removal,
immediate wakeup and durable successful mutation despite publication failure.

Validation: all 22 controller tests pass.
