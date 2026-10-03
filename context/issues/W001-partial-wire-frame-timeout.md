# W001: Wire integration tests can hang forever after a partial frame

Status: resolved.

The wire harness waits for the first readable byte with a selector, then calls
blocking `readline()`. A plugin that writes part of a JSON frame and stalls
therefore defeats the advertised receive timeout and hangs the entire test job.
This hides exactly the transport regressions the integration tests should catch.

Read bounded chunks under one absolute receive deadline, retaining partial and
coalesced frames between calls. Add pipe-based harness regression tests that
cannot themselves hang indefinitely when the old behavior returns.

The harness now uses selector-controlled bounded `os.read` calls and retains
unconsumed bytes. Command dispatch passes its remaining overall deadline to
receive. The idle assertion checks retained bytes as well as unread pipe data.

Validation: all five schema-validated wire tests and four new harness regressions
pass. Coverage includes a stalled partial frame, completing it after timeout,
coalesced frames, EOF mid-frame and the maximum frame boundary.
