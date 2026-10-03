# N003: SQLite save failures leave notes permanently unreconcilable

- Severity: high; failure recovery and retained save capacity.
- Reproduction: stage a note, enable SQLite `query_only`, and commit. The provider
  reports an access failure but retains the upload. Restoring database access and
  explicitly reconciling that write still reports `outcome_unknown` forever;
  two failed writes also exhaust both staging slots.
- Resolution: retire staging after the synchronous SQLite attempt returns an
  error. Keep the error response uncertain, fence the settled job identity, and
  allow explicit reconciliation to read the authoritative database. Never claim
  a rejected mutation merely because SQLite raised an exception.
- Validation: exercise a real SQLite read-only failure and an injected error
  after a durable save; reconciliation must report the actual stored version
  in both cases, and late chunks/commits must not revive the old upload.
