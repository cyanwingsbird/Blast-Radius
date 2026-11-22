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
   - **Executor tests** reuse what never depends on the patch: the TPC-H/TPC-DS
     tables (built once while the image builds), duckdb connections and views
     made only when a query needs them, and duckdb's answer for every query the
     tests send at the pinned commit (recorded while the image builds and used
     only when the query text is exactly the same). The tables survive a
     re-import as long as nothing their setup ran changed and their class was not
     re-imported; when a fixture is edited, the tests set everything up for real.
   - **Source read as text.** When a test reads a source file as text (for
     example, `test_typing`), it is served the patched text.
   - **The optimizer tests' duckdb tables** are created when a test first
     uses the connection, not in the class setup: the setup statements are
     plain duckdb SQL from the test file, and most of the class's tests never
     touch the connection. (Not when the patch changes the tests themselves.)
   - **`test_execute_tpcds` on reduced tables** (`small.bin`). The test runs
     57 queries on the sqlglot executor and compares each with duckdb. The
     probe runs the queries that reach the change on the first 1000 rows of
     each table and compares with what the pinned code answers there (and
     with the duckdb SQL the test would send), recorded in `small.bin`. All
     such queries when they are few, otherwise the cheapest ones, some with
     non-empty answers, and the heaviest. A query taking ten times its
     recorded time counts as a failure: a join gone cross kills the real
     test's worker. A changed line only the full tables reach falls back to
     the older probe.
   - **`test_generator_memory`** with a quarter of the measured loop and of
     the allowance.
   - **The two slowest tests** (`probes.bin`) are stood in for by probes.
     `test_execute_tpcds` runs only a few of the TPC-DS queries that execute
     the changed lines. `test_max_nodes` runs its own checks on a smaller
     query. If a changed line is run by the real test but neither by the
     probe nor by at least 20 other tests, the real test is kept. An edit to
     the TPC-DS fixture probes exactly the queries that changed.
3. **Data diff** (`datadiff.py`, `narrow.py`, `keymap.bin`). A change to a
   module or class body mostly changes data that sqlglot builds as it imports:
   a flag, a dict entry, a set member. When the candidates are worth it, a
   forked child re-imports the patched modules and compares every module- and
   class-level value with the pinned one, so it knows exactly which attributes,
   and which keys of a dict or members of a set, differ (a function counts as
   changed only if its code, defaults, closure or attributes differ). A test can
   only notice such a value
   - through source that names the attribute, run while the test runs (call-time
     coverage of the line; for test modules, also while they load);
   - through code that reads attributes by computed names (`getattr` with a
     computed name, `vars()`, `__dict__`, `dir()`);
   - for a dict entry or set member: by looking that key up. `keymap.bin`
     records, per test, the keys looked up at every keyed read of a constant
     (`d[k]`, `d.get(k)`, `k in d`, also through a local alias) and every key
     looked up in a constant passed as an argument (through a recording proxy).
     Any other read of the constant counts as reading all of it. For keys that
     are classes, a test must also have used the class.
   The candidates are cut to the tests that can see a changed value. If
   anything cannot be compared, the static reach stands.
   For changed dict entries keyed by a class, tests kept only for using that
   class run only the fixture items or `validate_*` calls that use it.
   A module-level name is matched by module, not only by spelling: a file
   that binds its own global of the same name (and never imports one) can
   mean the changed one only through `x.NAME` or a string. The packages' lazy
   `__getattr__` hooks read only the names their tables list, and
   `test_lazy_load`, which only runs `import sqlglot` in a new interpreter, is
   dropped when the patch is outside the files that import loads.
4. **Comparison operators** (`analyze.py`, `cmpmap.bin`). When a patch only
   swaps the operator of comparisons inside functions (`is` for `==`, `<` for
   `<=`, ...), a test can only notice if it compares a pair of values that the
   old and the new operator answer differently for. `cmpmap.bin` records, per
   test and per comparison in sqlglot, which kinds of pairs were compared:
   identical or not, and equal, less, greater, unequal or unordered. The
   candidates are cut to the tests that saw a pair the two operators disagree
   on. Pairs whose comparison could run sqlglot code (two expressions of the
   same class, for example) count as disagreeing, and so does any pair compared
   while modules import. A swap such as `x is None` to `x == None` that no test
   can tell apart costs nothing.
5. **Cost model** (`selector.py`). Per commit, verify the candidates whose CPU
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
| `keymap.bin` | `keyrec.py` with `brrec.py` (records the keys each test looks up, on an instrumented copy whose lines do not move) |
| `cmpmap.bin` | `cmprec.py` with `cmprt.py` (records the kinds of operand pairs each comparison sees, on an instrumented copy); `cmpcheck.py` runs the tests the narrowing drops on random operator swaps |
| `small.bin` | `buildsmall.py`; `tpcheck.py` compares the probe with the real test on mutants |
| `narrow.idx` (in the image) | built by `narrow.py` during the image build |
| `execcache.pickle`, `duckcache.pickle`, `refs.pickle` (in the image) | built by `runner.py` during the image build: executor tables, duckdb results of the pinned queries, module reference map |

`mkmut.py` and `groundtruth.py` make one-line mutants and label them by
running the tests; `ddtest.py` and `ddeval.py` check the data-diff narrowing
against those labels. They were used only to check the selector for misses.
