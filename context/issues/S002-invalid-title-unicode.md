# S002: Invalid Unicode titles escape validation

Status: resolved.

Python JSON decoding accepts escaped lone surrogate code points, but SQLite's
UTF-8 text encoder cannot store them. `title_text()` accepted these strings, so
adding, renaming, or importing such a title raised raw `UnicodeEncodeError`
instead of the storage validation error handled by callers. Import also reached
database writes after claiming to validate the complete document.

Reproduction: call `store.add("bad\ud800")` in a temporary database, or import
an otherwise valid export whose title contains an escaped lone surrogate.

Fix: require UTF-8 encodability during title validation, with regression
coverage for add/rename/import and preservation of existing and empty databases.

Validation: `python3 -m unittest discover -s tests -p test_storage.py -v` passes.
