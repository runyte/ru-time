# C001: Configuration generation discards an explicitly selected workspace

Status: resolved.

`--workspace /chosen --print-config` emits only the entrypoint argument. Running
that configuration from another directory consequently opens that directory's
database instead of `/chosen`'s database. Existing tasks appear missing and new
tasks are saved into an unintended workspace.

Preserve an explicitly supplied workspace as an absolute argument in generated
configuration. Configuration generated without `--workspace` must keep the
existing dynamic behavior of using the host working directory. An explicit
`--database` continues to take precedence.

The parser now distinguishes an omitted workspace from an explicit one and
emits the canonical explicit path only when needed. Home-relative workspace
paths are expanded consistently before selecting or preserving their identity.

Validation: five CLI tests pass, including subprocess round trips from different
working directories for explicit and default workspace configuration. Tests use
temporary HOME/XDG paths and verify that printing creates no application data.
