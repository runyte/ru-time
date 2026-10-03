# T002: Preserve the deterministic storage stress experiment

Status: resolved; validation aid, not a newly discovered product defect.

The cache review included 3,000 random operations compared against independently
computed SQLite task aggregates. Preserve this experiment as an opt-in helper so
future storage changes can reproduce the review without slowing default tests.

`tests/stress_storage.py` uses only temporary SQLite data and standard-library
modules. It mixes task and note mutations, time jumps, checkpoints, deliberately
rejected commits, interrupted reopen, and recovery, checking every resulting
snapshot against an uncached query. A fixed default seed makes choices repeatable.

Validation: `python3 tests/stress_storage.py --steps 3000 --seed 20261003` passes.
This stress check supplements, rather than replaces, the focused regression tests.
