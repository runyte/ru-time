# N004: The note migration test leaves a SQLite connection open

- Severity: low; test isolation and resource cleanup.
- Reproduction: the migration fixture uses a SQLite transaction context as if
  it closes the connection. It does not, producing `ResourceWarning` on modern
  Python and retaining a handle to temporary storage until garbage collection.
- Resolution: explicitly close the fixture connection around its transaction.
- Validation: note tests run with `ResourceWarning` promoted to an error.
