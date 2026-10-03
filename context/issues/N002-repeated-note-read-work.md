# N002: Chunked note reads repeatedly load and hash the full document

- Severity: medium; avoidable CPU, allocations, and database traffic.
- Reproduction: reading an 8 MiB note in 64 protocol chunks performs 64 full
  SQLite reads, UTF-8 encodings, and SHA-256 calculations. Local baseline:
  approximately 338 ms for one complete read, processing at least 512 MiB.
- Resolution: retain at most two immutable encoded snapshots, pinned to each
  read job's key and version, as allowed by the public `runyte-1` contract.
  Forward reliable `resource.released` events without the UI lock; release
  retained bytes and fence late requests using bounded job tombstones.
- Validation: count storage reads across a complete 8 MiB transfer; prove an
  existing job retains its version after a save, new jobs see new content,
  snapshot capacity is bounded, and release before or after reads frees/fences
  the job. Re-run protocol/schema and optional native acceptance at handoff.
- Result: local 8 MiB transfer dropped from approximately 338 ms to 8.4 ms;
  twelve note tests and five schema-validated public-wire tests pass.
