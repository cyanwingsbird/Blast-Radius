# Blast Radius entry

Picks the sqlglot tests a patch can break, and splits them across workers.

## How it decides

1. **Static reach** (`analyze.py`, `map.bin`). The map was built from the
   pinned sqlglot commit by running every test in its own process and
   recording, per source line, which tests executed it. Module- and class-body
   lines (definition time) are kept apart from function-body lines (call
   time). Worker processes spawned by the tests are included, and data files
   are mapped to the tests that open them. A patch is compiled and compared
   with the pinned bytecode, so comment-only and other no-op edits reach
   nothing.
   - A changed function is narrowed to the tests that ran the changed
     statements.
   - A changed class body is narrowed to the tests that used that class or a
     related class (subclasses, nested classes, a dialect's
     tokenizer/parser/generator). The exception is a class that is
     instantiated while modules import (recorded with its ancestors). For
     those, every test that loads the module is in reach.
2. **Verification** (`runner.py`). Candidate tests are run in the container
   against the patched code. The pinned sqlglot and its tests are imported
   once, and each commit gets a forked child. The child swaps in the changed
   function bodies, or re-imports from the patched sources when module or
   class bodies changed. A re-import drops only the changed modules and the
   modules that hold references into them. The parent maps those references
   once: through classes, their bases and attributes, containers, defaults and
   shared module-level data. Everything else, such as unaffected dialects,
   stays loaded. Process pools in the tests run in-process. Tests that
   pass are dropped. Tests that fail, time out or are never reached are kept.
   - **Fixture-driven tests** (`casemap.bin`), such as `check_file`, identity
     and annotate fixtures, run only the fixture items whose recorded coverage
     or class usage reaches the change. When a fixture file itself is edited,
     it is split the way the test helpers split it, and only the items that
     differ are run. `test_generator_memory` loops over every dialect, so it
     runs only the dialects whose iteration reaches the change.
   - **Dialect tests** (`callmap.bin`) are mostly long runs of
     `validate_identity` / `validate_all` calls. They run only the calls whose
     recorded coverage or class usage reaches the change. Each other call
     returns a lazy stand-in that runs the real call if the test uses its
     result. If a changed line or class is used by most of a test's calls, or
     outside them, the whole test runs.
   - **Source read as text.** When a test reads a source file as text (for
     example, `test_typing`), it is served the patched text.
   - **The two slowest tests** (`probes.bin`) are stood in for by probes.
     `test_execute_tpcds` runs only a few of the TPC-DS queries that execute
     the changed lines. `test_max_nodes` runs its own checks on a smaller
     query. If a changed line is run by the real test but neither by the
     probe nor by at least 20 other tests, the real test is kept. An edit to
     the TPC-DS fixture probes exactly the queries that changed.
3. **Cost model** (`selector.py`). Per commit, verify the candidates whose CPU
   (`costs.json`) is less than what scheduling them blind would cost. After a
   failure, verification carries on unless at least half of what has run
   failed. Everything kept is packed longest-first onto however many workers
   costs least.

Safety nets:
- A wall-clock budget: whatever is unverified when it runs out stays
  scheduled.
- A per-patch fallback: a patch that cannot be analysed schedules every test.
- Unusual patches are handled narrowly rather than by scheduling everything:
  - a permission-only change reaches nothing;
  - a binary data file keeps only the tests that open it;
  - a new source file is served to verification (tests that list its
    package's directory are kept);
  - CRLF patches are normalised.
- A global failsafe: if the selector itself fails, every test is printed on
  every commit.

Nothing in the repository depends on any particular patch. The maps, class
relations, probes and costs all come from running the pinned commit's own
tests.

## Building the data files

`tools/` holds the scripts that built them from a checkout of the pinned
commit:

| Data file | Built by |
| --- | --- |
| `map.bin` | `covone.py` + `sitecustomize.py` (per-test line coverage), then `buildmap.py` and `packmap.py` |
| class bits in `map.bin` | `typeuse.py` (classes each test uses), `family.py`, `buildcls.py` |
| `casemap.bin` | `casecov.py`, `buildcases.py`, `gencov.py` |
| `callmap.bin` | `callcov.py`, `skipall.py` (drops tests that cannot run with calls skipped) |
| `probes.bin` | `tpcds_cov.py`, `probecal.py`, `probe_check.py`, `buildprobes.py` |
| `costs.json` | `measure.py` |

`mkmut.py` and `groundtruth.py` make one-line mutants and label them by
running the tests. They were used only to check the selector for misses.
