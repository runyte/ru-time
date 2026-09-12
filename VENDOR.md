# Runyte protocol provenance

`vendor/application.py` is an unmodified copy of Runyte's standard-library
Python application client. `tests/runyte-experimental-2.schema.json` and
`tests/host_handshake.json` provide the matching public schema and handshake
fixtures; the latter selects this plugin's capabilities and command name.

Source: https://github.com/runyte/runyte/tree/3eea992a98e5be1853810af240ef450999f24df9/docs/plugins

License: MPL-2.0, retained in the client header and this repository's LICENSE.
The upstream client was last changed by `f67289c` at that source revision.
The extension epoch is `runyte-experimental-2`.

To update, review the public application contract and client changes, copy the
client and schema together, refresh handshake fixtures, update this provenance,
and run storage, controller, public-wire, schema, and real-editor acceptance
tests. Do not import private bundled-client DTOs or the headless test facade.
