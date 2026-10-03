# N001: Invalid UTF-8 ranges can acknowledge an empty chunk

- Severity: medium; note provider correctness.
- Reproduction: save `é猫x`, then read byte offset 3 with limit 1. The end-boundary
  loop backs up past the requested offset and returns empty text with `eof=false`
  instead of rejecting the split scalar. A lone surrogate in an upload chunk
  also escapes as `UnicodeEncodeError` instead of a typed protocol error.
- Resolution: stop boundary adjustment at the requested offset, reject invalid
  integer bounds and malformed UTF-8 upload text before modifying staging.
- Validation: regression tests cover every starting byte and small chunk limit
  across mixed Unicode text, plus malformed upload text and unchanged staging.
