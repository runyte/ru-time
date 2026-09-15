# Runyte protocol provenance

`vendor/application.py` is an unmodified copy of Runyte's maintained Python
application client. Its runtime uses only the Python 3.10+ standard library.
`tests/runyte-1.schema.json` and `tests/ranges.json` are byte-identical upstream
schema and shared range vectors. `tests/host_handshake.json` adapts the upstream
stable fixtures to this plugin's capabilities and command identity.

Source: https://github.com/runyte/runyte/tree/3fa28b0bf6bb418027265413745080505d798f09/docs/plugins

| Local artifact | Upstream path under `docs/plugins/` | SHA-256 |
| --- | --- | --- |
| `vendor/application.py` | `application.py` | `1efccca99c41066cb0f6700ad18b98f751a7aa22ac6d7ddc432d7e6120ce704f` |
| `tests/runyte-1.schema.json` | `runyte-1.schema.json` | `4ed166f3ba4f062cd84a08304ffbd3d6ccf6c27ece6a262cabd127e1e84adf9e` |
| `tests/ranges.json` | `compatibility/ranges.json` | `9b356c1d681e0220f90be0f07a88460786c50a2d8f79c911849398ee74f997ff` |

This is the first stable candidate source, not a published 0.3.0 release.
The native host inventory in `tests/runyte-hosts.json` records its honest
0.3.0 bootstrap construction and is independent of SDK provenance.
Source pins must be pushed and independently fetchable before CI/release acceptance.

License: MPL-2.0, retained in the client header and this repository's LICENSE.
The extension protocol is `runyte-1`. Supported host releases are authored once
in `ru_time/compatibility.py` and copied into configuration and registration.
An SDK source revision does not imply a minimum host release. Compatible host
updates do not require replacing this vendored client.

To update, review upstream protocol/client changes, copy the client, schema and
vectors unchanged from one immutable revision, refresh handshake fixtures,
record the full source SHA and digests here, and run standard-library, optional
schema and native oldest/newest-host acceptance. Never patch the vendored file
locally or use private bundled-client DTOs/the headless testing facade.
