# W002: Compatibility registration checks can hang on incomplete output

Status: resolved.

The configuration/registration compatibility test independently used the same
selector-then-blocking-readline pattern as W001. A partial registration followed
by stalled output could therefore hang its five-second check indefinitely.

Extracted the W001 bounded JSON reader into `tests/wire_support.py` and reused it
for compatibility registration and all wire integration reads. Existing pipe
regressions now test the shared helper directly, avoiding duplicate timeout
logic or nested integration-test fixture construction.

Validation: all five compatibility tests and four wire-harness regression tests
pass. Notes wire validation is being updated independently under N002 to use
separate host job lifecycles for immutable note snapshots.
