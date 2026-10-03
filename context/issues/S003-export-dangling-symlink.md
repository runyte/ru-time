# S003: Export follows an existing dangling output symlink

Status: resolved.

`write_export()` resolved the complete output path before using `os.link()` to
publish without replacement. An existing symlink to a missing file was therefore
followed and its target was created. This violated the documented requirement
that exports use a new filename, and wrote data somewhere other than the supplied
directory entry.

Reproduction: create `existing.json -> missing.json`, then export to
`existing.json`. The previous implementation succeeded and created `missing.json`.

Fix: canonicalize the parent directory while retaining the final path
component. The existing atomic link operation can then reject every existing
destination entry, including dangling symlinks.

Validation: the temporary-filesystem regression verifies `FileExistsError`, the
unchanged symlink, absent target, and cleanup of the temporary export file.
`python3 -m unittest discover -s tests -p test_storage.py -v` passes.
