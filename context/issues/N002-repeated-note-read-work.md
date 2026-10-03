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

Status: resolved; independent follow-up review is clean. Final schema checks and
both native tests against local Runyte 0.3.5 passed. The cache retains at most
16 MiB of encoded note content and relies on reliable read-release notifications;
released-job tombstones prevent queued handlers from recreating retired state.

A five-sample alternating benchmark includes the initial stat/snapshot load and
all 64 chunks, using a fresh provider for every sample. On Linux x86_64,
Python 3.14.7 and SQLite 3.51.2, reviewer medians were 498.853 ms before and
7.866 ms after. An independent repeat measured 604.547 ms and 8.554 ms. Timing
varies with system load; these are workload measurements, not a universal speedup.
The deterministic regression verifies one storage read rather than 65.

Reproduce from the repository root with only temporary SQLite data:

```sh
python3 - <<'PY'
import hashlib
import platform
import sqlite3
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from ru_time.storage import Store
from ru_time.notes import Notes

source = subprocess.check_output(
    ['git', 'show', '4e586d9:ru_time/notes.py'], text=True)
namespace = {'__name__': 'ru_time.notes_baseline', '__package__': 'ru_time'}
exec(compile(source, 'baseline-notes.py', 'exec'), namespace)
Baseline = namespace['Notes']

class Host:
    pass

with tempfile.TemporaryDirectory() as directory:
    store = Store(Path(directory) / 'benchmark.sqlite3')
    try:
        key = store.add('8 MiB benchmark')
        version = store.save_note(
            key, 'x' * (8 * 1024 * 1024), hashlib.sha256(b'').hexdigest())
        samples = {'baseline': [], 'current': []}
        for _ in range(5):
            for label, cls in (('baseline', Baseline), ('current', Notes)):
                provider = cls(Host(), lambda: store, 'time')
                context = {'provider': 'notes', 'key': key, 'job': 'read:1'}
                start = time.perf_counter()
                provider.stat(context)
                for offset in range(0, 8 * 1024 * 1024, 128 * 1024):
                    provider.read(dict(context, version=version,
                                       offset=offset, limit=128 * 1024))
                samples[label].append((time.perf_counter() - start) * 1000)
        print(platform.platform(), platform.python_version(), sqlite3.sqlite_version)
        for label, values in samples.items():
            print(label, 'median_ms=', statistics.median(values),
                  'samples_ms=', values)
    finally:
        store.close()
PY
```
- Result: local 8 MiB transfer dropped from approximately 338 ms to 8.4 ms;
  twelve note tests and five schema-validated public-wire tests pass.
