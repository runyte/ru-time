# W003: Optional wire schema checks did not validate simulated host messages

Status: resolved.

The schema lane validated only plugin output. Its simulated `resource.open`
response omitted the required job `progress` field, yet every schema test passed.
The missing validation also failed to detect initially incomplete release-event
fixtures added during N002 (those sequence fields have since been corrected).

Validate both directions against the pinned public contract whenever optional
schema checking is enabled, and make the job fixture conform to that contract.
Keep ordinary tests standard-library-only.

The optional lane now checks all sent host messages against `hostMessage` and all
received plugin messages against `pluginMessage`; the simulated job includes
`progress: 0`. The pinned client and schema remain unchanged.

Validation: `RU_TIME_VALIDATE_SCHEMA=1 python3 -m unittest discover -s tests
-p 'test_wire*.py' -v` passes all nine wire/harness tests with both directions
checked.
