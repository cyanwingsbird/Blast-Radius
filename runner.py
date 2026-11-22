"""Run a few sqlglot tests against a patched tree, inside a forked child.

The parent imports the pinned sqlglot and the test modules once.  Each commit
gets a forked child that patches the already-imported code in place (swapping
function code objects) or, when module- or class-level code changed, drops
every sqlglot/tests module and re-imports them from the patched sources.
Process pools in the tests are replaced by an in-process executor so the
patched code is what runs, and no interpreters have to be spawned.
"""

import builtins
import concurrent.futures as cf
import gc
import importlib.abc
import importlib.machinery
import importlib.util
import io
import json
import os
import pickle
import signal
import sys
import time
import types
import unittest

ROOT = os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot")
CodeType = types.CodeType
FunctionType = types.FunctionType


# ------------------------------------------------------------ sync pool

class SyncExecutor(cf.Executor):
    """Runs submitted calls immediately, with arguments and results passed
    through pickle as a process pool would."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_kw):
        if initializer is not None:
            # a worker gets its own copy of these; this child is thrown away after its tests,
            # and what the initializer is given is only read by the queries (the executor
            # tests' tables), so the copy is skipped for the big ones
            if _big(initargs):
                initializer(*initargs)
            else:
                initializer(*pickle.loads(pickle.dumps(initargs, protocol=pickle.HIGHEST_PROTOCOL)))

    def submit(self, fn, /, *args, **kwargs):
        f = cf.Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs), protocol=pickle.HIGHEST_PROTOCOL))
            res = fn(*a, **k)
            res = pickle.loads(pickle.dumps(res, protocol=pickle.HIGHEST_PROTOCOL))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        else:
            f.set_result(res)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def _big(args):
    try:
        return any(isinstance(a, dict) and len(a) > 5 and
                   all(type(v).__name__ == "Table" for v in a.values()) for a in args)
    except Exception:
        return False


def install_sync_pools():
    cf.ProcessPoolExecutor = SyncExecutor
    import concurrent.futures.process as cfp
    cfp.ProcessPoolExecutor = SyncExecutor
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.startswith("tests") and getattr(mod, "ProcessPoolExecutor", None) is not None:
            mod.ProcessPoolExecutor = SyncExecutor


# ------------------------------------------------------------ overlays

class PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, sources):
        self.sources = sources  # module name -> (path, text)

    def find_spec(self, name, path=None, target=None):
        got = self.sources.get(name)
        if got is None:
            return None
        fpath, text = got
        is_pkg = fpath.endswith("__init__.py")
        loader = PatchedLoader(fpath, text)
        spec = importlib.util.spec_from_file_location(
            name, fpath, loader=loader,
            submodule_search_locations=[os.path.dirname(fpath)] if is_pkg else None)
        return spec


class PatchedLoader(importlib.abc.Loader):
    def __init__(self, path, text):
        self.path = path
        self.text = text

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = compile(self.text, self.path, "exec", dont_inherit=True)
        exec(code, module.__dict__)


def modname(rel):
    m = rel[:-3].replace("/", ".")
    if m.endswith(".__init__"):
        m = m[: -len(".__init__")]
    return m


def install_data_overlay(data):
    """data: {abs path: text}"""
    if not data:
        return
    real_open = io.open

    def patched_open(file, mode="r", *a, **k):
        try:
            p = os.path.abspath(os.fspath(file)) if isinstance(file, (str, bytes, os.PathLike)) else None
        except Exception:
            p = None
        if isinstance(p, bytes):
            p = p.decode()
        if p is not None and p in data and not any(c in mode for c in "wax+"):
            text = data[p]
            if "b" in mode:
                return io.BytesIO(text.encode("utf-8"))
            return io.StringIO(text)
        return real_open(file, mode, *a, **k)

    builtins.open = patched_open
    io.open = patched_open


# ------------------------------------------------------------ code swaps

def _subtree_differs(a, b, sig):
    if sig(a) != sig(b):
        return True
    ka = [x for x in a.co_consts if isinstance(x, CodeType)]
    kb = [x for x in b.co_consts if isinstance(x, CodeType)]
    if len(ka) != len(kb):
        return True
    return any(_subtree_differs(x, y, sig) for x, y in zip(ka, kb))


def plan_swaps(old, new, sig):
    """Pairs (old_code, new_code) for function code objects to replace, or
    None when a module- or class-body changed (needs a re-import)."""
    pairs = []

    def walk(a, b):
        ka = [x for x in a.co_consts if isinstance(x, CodeType)]
        kb = [x for x in b.co_consts if isinstance(x, CodeType)]
        if len(ka) != len(kb):
            return False
        if not (a.co_flags & 1):           # module / class body
            if sig(a) != sig(b):
                return False
        for x, y in zip(ka, kb):
            if (x.co_flags & 1) and _subtree_differs(x, y, sig):
                if (x.co_name != y.co_name or len(x.co_freevars) != len(y.co_freevars)):
                    return False
                pairs.append((x, y))
                # nested code is reached through the new parent's constants,
                # but closures already made from nested code need swapping too
                if not walk_nested(x, y):
                    return False
            elif not (x.co_flags & 1):
                if not walk(x, y):
                    return False
        return True

    def walk_nested(a, b):
        ka = [x for x in a.co_consts if isinstance(x, CodeType)]
        kb = [x for x in b.co_consts if isinstance(x, CodeType)]
        if len(ka) != len(kb):
            return False
        for x, y in zip(ka, kb):
            if _subtree_differs(x, y, sig):
                if len(x.co_freevars) != len(y.co_freevars):
                    return False
                pairs.append((x, y))
                if not walk_nested(x, y):
                    return False
        return True

    if not walk(old, new):
        return None
    return pairs


# ------------------------------------------------------------ the runner

def load_case(test_id):
    f, cls, meth = test_id.split("::")
    mod = importlib.import_module(f[:-3].replace("/", "."))
    return getattr(mod, cls), meth


LOADERS = ("load_sql_fixture_pairs", "load_sql_fixtures")


def _filter_fixtures(test_id, allowed):
    """Make the test's fixture loaders yield only the allowed (file, index) items."""
    mod = importlib.import_module(test_id.split("::")[0][:-3].replace("/", "."))
    saved = {}
    dialects = [v for kind, v in allowed if kind == "__dialects__"]
    if test_id.endswith("::test_generator_memory"):
        # its items are the dialects it loops over
        orig_d = getattr(mod, "Dialects")
        saved["Dialects"] = orig_d
        keep = set(dialects)
        setattr(mod, "Dialects", [d for d in orig_d if d.value in keep])
    for name in LOADERS:
        orig = getattr(mod, name, None)
        if orig is None:
            continue
        saved[name] = orig

        def make(orig):
            def gen(filename, *a, **k):
                for idx, item in enumerate(orig(filename, *a, **k)):
                    if (filename, idx) in allowed:
                        yield item
            return gen

        setattr(mod, name, make(orig))

    def restore():
        for name, orig in saved.items():
            setattr(mod, name, orig)

    return restore


VALIDATE = ("validate_identity", "validate_all", "validate_transpile")
QUIET_MIN = 3


class _Lazy:
    """Stands in for the result of a validate_* call that was not run.
    The call runs the moment anything looks at the result."""

    __slots__ = ("_br_f", "_br_v", "_br_done")

    def __init__(self, f):
        object.__setattr__(self, "_br_f", f)
        object.__setattr__(self, "_br_done", False)

    def _br_get(self):
        if not object.__getattribute__(self, "_br_done"):
            v = object.__getattribute__(self, "_br_f")()
            object.__setattr__(self, "_br_v", v)
            object.__setattr__(self, "_br_done", True)
        return object.__getattribute__(self, "_br_v")

    @property
    def __class__(self):
        return self._br_get().__class__

    def __getattr__(self, name):
        return getattr(self._br_get(), name)

    def __setattr__(self, name, value):
        setattr(self._br_get(), name, value)

    def __eq__(self, other):
        return self._br_get() == other

    def __ne__(self, other):
        return self._br_get() != other

    def __hash__(self):
        return hash(self._br_get())

    def __repr__(self):
        return repr(self._br_get())

    def __str__(self):
        return str(self._br_get())

    def __bool__(self):
        return bool(self._br_get())

    def __iter__(self):
        return iter(self._br_get())

    def __len__(self):
        return len(self._br_get())

    def __getitem__(self, k):
        return self._br_get()[k]

    def __contains__(self, k):
        return k in self._br_get()

    def __call__(self, *a, **k):
        return self._br_get()(*a, **k)


def _filter_calls(allowed):
    """Run only the allowed top-level validate_* calls of a Validator test; the
    others return a lazy stand-in that runs them if their result is used."""
    V = importlib.import_module("tests.dialects.test_dialect").Validator
    state = {"n": 0, "depth": 0}
    saved = {}

    def run(orig, self, a, k):
        state["depth"] += 1
        try:
            return orig(self, *a, **k)
        finally:
            state["depth"] -= 1

    for name in VALIDATE:
        orig = V.__dict__.get(name)
        if orig is None:
            continue
        saved[name] = orig

        def make(orig):
            def wrapper(self, *a, **k):
                if state["depth"]:
                    return orig(self, *a, **k)
                i = state["n"]
                state["n"] += 1
                if i in allowed:
                    return run(orig, self, a, k)
                return _Lazy(lambda: run(orig, self, a, k))
            return wrapper

        setattr(V, name, make(orig))

    def restore():
        for name, orig in saved.items():
            setattr(V, name, orig)

    return restore


def _probe_case(tid, any_failed=False):
    """Cheap stand-ins for the two slowest tests.

    PROBE:tpcds:<i,j,...>  runs test_execute_tpcds on only those queries.
    PROBE:max_nodes        runs test_max_nodes' checks on a smaller query."""
    kind, _, arg = tid[len("PROBE:"):].partition(":")
    if kind == "tpcds":
        te = importlib.import_module("tests.test_executor")
        klass = te.TestExecutor
        basic, _, extra = arg.partition("|")
        extra = extra.split("|")[0]
        idx = [int(x) for x in basic.split(",") if x]
        if any_failed:
            idx += [int(x) for x in extra.split(",") if x]

        def _br_probe(self):
            sub = [type(self).tpcds_sqls[i] for i in idx]
            self.tpcds_sqls = sub
            self._mp_execute(te.TPCDS_SCHEMA, self.tpcds_tables, sub, False)

        klass._br_probe = _br_probe
        return klass, "_br_probe"
    if kind == "max_nodes":
        tp = importlib.import_module("tests.test_parser")
        klass = tp.TestParser
        klass._br_probe = _max_nodes_probe
        return klass, "_br_probe"
    if kind == "setup":
        te = importlib.import_module("tests.test_executor")
        te.TestExecutor._br_probe = lambda self: None
        return te.TestExecutor, "_br_probe"
    if kind == "tpcds2":
        te = importlib.import_module("tests.test_executor")
        parts = arg.split("|")
        parse_idx = [int(x) for x in parts[0].split(",") if x]
        exec_idx = [int(x) for x in parts[1].split(",") if x]

        class _SmallTpcds(unittest.TestCase):
            def runTest(self):
                _small_tpcds_probe(self, te, parse_idx, exec_idx)

        return _SmallTpcds, "runTest"
    if kind == "genmem":
        tg = importlib.import_module("tests.test_generator")
        klass = tg.TestGenerator
        klass._br_probe = _genmem_probe
        return klass, "_br_probe"
    raise ValueError(tid)


SMALL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "small.bin")


def load_small():
    import zlib
    with open(SMALL_FILE, "rb") as f:
        return pickle.loads(zlib.decompress(f.read()))


SMALL_LIMIT = float(os.environ.get("BR_SMALL_LIMIT", "1.5"))


class _Blowup(AssertionError):
    pass


def _blowup(signum, frame):
    raise _Blowup("query takes far longer than at the pinned commit")


def _small_tpcds_probe(self, te, parse_idx, exec_idx):
    """test_execute_tpcds on the first rows of each table.  What the pinned code answers
    there was recorded with small.bin; the test passes at the pinned commit, so a query
    that still gets the same answer, from the same duckdb SQL, is taken to still agree
    with duckdb.  (On the fixtures' own tables nearly every query's answer is empty
    already: their date table ends decades before the sales.)"""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table

    small = load_small()
    sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    for i in parse_idx:
        parse_one(sqls[i][1])
    tables = {}
    for i in exec_idx:
        meta, sql, _ = sqls[i]
        want = small["q"][i]
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        if not meta.get("execute"):
            raise AssertionError("query %d is no longer executed" % i)
        use = {}
        for t in find_tables(expression):
            name = t.name
            if name not in tables:
                columns, rows = small["tables"][name]
                tables[name] = Table(columns=columns, rows=list(rows))
            use[name] = tables[name]
        # a plan that explodes (a join gone cross) kills the real test's worker on the
        # full tables; here it shows as a query taking many times what it did
        # (CPU time of this process, so a stalled machine does not look like a blow-up)
        signal.signal(signal.SIGVTALRM, _blowup)
        signal.setitimer(signal.ITIMER_VIRTUAL, max(SMALL_LIMIT, 10.0 * want["time"]))
        try:
            got = execute(expression, schema=te.TPCDS_SCHEMA, tables=use)
        finally:
            signal.setitimer(signal.ITIMER_VIRTUAL, 0)
        if transpile(sql, write="duckdb")[0] != want["duck"]:
            raise AssertionError("query %d: duckdb is sent other SQL" % i)
        if tuple(got.columns) == want["columns"] and list(got.rows) == want["rows"]:
            continue
        import numpy as np
        import pandas as pd
        from pandas.testing import assert_frame_equal

        a = pd.DataFrame(((np.nan if c is None else c for c in r) for r in want["rows"]), columns=want["columns"])
        b = pd.DataFrame(((np.nan if c is None else c for c in r) for r in got.rows), columns=got.columns)
        assert_frame_equal(a, b, check_dtype=False, check_index_type=False)


def _genmem_probe(self):
    """test_generator_memory with a quarter of the measured loop and of the allowance.
    The loop still crosses a growth step of every container kind (a dict's at 170 and
    341 entries, a set's at 307, a list's every few), so a leak per call shows."""
    import tracemalloc
    from sqlglot.dialects.dialect import Dialect, Dialects

    for name in Dialects:
        with self.subTest(dialect=name):
            dialect = Dialect.get_or_raise(name)
            for _ in range(100):
                dialect.generator()
            gc.collect()
            tracemalloc.start()
            try:
                before = tracemalloc.get_traced_memory()[0]
                for _ in range(250):
                    dialect.generator()
                gc.collect()
                after = tracemalloc.get_traced_memory()[0]
            finally:
                tracemalloc.stop()
            self.assertLess(after - before, 256)


def _max_nodes_probe(self):
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    self.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
    cols = [f"col{i}" for i in range(500)]
    self.assertIsInstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select)
    width = 100
    depth = 100
    expr = "x" + "=x" * depth
    sql = f"SELECT {','.join(expr for _ in range(width))}"
    self.assertIsInstance(parse_one(sql, max_nodes=10_000_000), exp.Select)
    with self.assertRaises(ParseError) as ctx:
        parse_one(sql, max_nodes=10_000)
    self.assertIn("Maximum number of AST nodes", str(ctx.exception))
    with self.assertRaises(ParseError) as ctx:
        parse_one(sql, max_nodes=500)
    self.assertIn("(500)", str(ctx.exception))
    with self.assertRaises(ParseError):
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    self.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad = False

    def addFailure(self, test, err):
        self.bad = True

    def addError(self, test, err):
        self.bad = True

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.bad = True

    def addUnexpectedSuccess(self, test):
        self.bad = True


def _blind_cost(loads_ms, n, total):
    """Approximate cost (ms) of scheduling a set unverified: best worker count, LPT bound."""
    if n == 0:
        return 0
    mx = max(loads_ms)
    return min(max(mx, -(-total // k)) + 2000 * k for k in range(1, min(8, n) + 1))


MASS_N = int(os.environ.get("BR_MASS_N", "3"))
MASS_F = float(os.environ.get("BR_MASS_F", "0.5"))


def run_tests(order, out_fd, mass_stop=True, dyn=None):
    """order: list of (num, test_id).  Writes 'num status secs' lines to out_fd.
    Classes are set up once each and torn down at the end of their run."""
    w = os.fdopen(out_fd, "w", buffering=1)
    ready = []
    any_failed = False
    n_done = 0
    n_bad = 0
    kept = list(dyn["kept"]) if dyn else []      # recorded ms of what is already scheduled
    for num, tid in order:
        is_probe = isinstance(tid, str) and tid.startswith("PROBE:")
        if is_probe and tid.endswith("|quiet") and not any_failed and n_done >= QUIET_MIN:
            # at least QUIET_MIN other tests ran against the patch and none failed: skip the probe
            w.write("%d P 0\n" % num)
            continue
        if dyn and n_bad and not is_probe and num in dyn["ms"]:
            # a test already failed, so this commit pays for workers anyway: keep a test
            # unverified when that adds less than running it would cost
            t_ms = dyn["ms"][num]
            before = _blind_cost(kept, len(kept), sum(kept))
            after = _blind_cost(kept + [t_ms], len(kept) + 1, sum(kept) + t_ms)
            if (after - before) / 1000.0 < dyn["rc"].get(num, 0.0):
                kept.append(t_ms)
                w.write("%d S 0\n" % num)
                continue
        is_setup = isinstance(tid, tuple) and tid[0] == "SETUP"
        if mass_stop and n_bad >= MASS_N and n_bad >= MASS_F * n_done and not is_probe and not is_setup:
            w.write("%d S 0\n" % num)
            continue      # a mass breakage: leave the rest scheduled, but still run the probes
        t0 = time.process_time()
        restore = None
        try:
            if is_setup:                           # ("SETUP", test id, None): only its class setup matters
                klass, meth = load_case(tid[1])
            elif isinstance(tid, tuple) and tid[0] == "CALLS":   # ("CALLS", test id, allowed call numbers)
                klass, meth = load_case(tid[1])
                restore = _filter_calls(tid[2])
            elif isinstance(tid, tuple):            # ("FILTER", test id, allowed items)
                klass, meth = load_case(tid[1])
                restore = _filter_fixtures(tid[1], tid[2])
            elif tid.startswith("PROBE:"):
                klass, meth = _probe_case(tid, any_failed)
            else:
                klass, meth = load_case(tid)
        except BaseException:
            any_failed = True
            n_done += 1
            n_bad += 1
            if dyn and num in dyn["ms"]:
                kept.append(dyn["ms"][num])
            w.write("%d F %.4f\n" % (num, time.process_time() - t0))
            continue
        if klass not in ready:
            ready.append(klass)
            klass._br_setup_failed = False
            _DEFER_DUCK["setup"] = klass.__module__ in _DEFER_CLASSES
            try:
                klass.setUpClass()
            except BaseException:
                klass._br_setup_failed = True
            _DEFER_DUCK["setup"] = False
        if getattr(klass, "_br_setup_failed", False):
            any_failed = True
            n_done += 1
            n_bad += 1
            if dyn and num in dyn["ms"]:
                kept.append(dyn["ms"][num])
            w.write("%d F %.4f\n" % (num, time.process_time() - t0))
            continue
        if is_setup:
            n_done += 1
            w.write("%d P %.4f\n" % (num, time.process_time() - t0))
            continue
        res = _Result()
        try:
            case = klass(meth)
            case.run(res)
            bad = res.bad
        except BaseException:
            bad = True
        if restore is not None:
            restore()
        any_failed = any_failed or bad
        n_done += 1
        n_bad += bool(bad)
        if bad and dyn and num in dyn["ms"]:
            kept.append(dyn["ms"][num])
        w.write("%d %s %.4f\n" % (num, "F" if bad else "P", time.process_time() - t0))
    for klass in ready:
        _teardown(klass)
    w.write("DONE\n")
    w.flush()


def _teardown(klass):
    try:
        klass.tearDownClass()
    except BaseException:
        pass
    try:
        klass.doClassCleanups()
    except BaseException:
        pass


def _ours(name):
    return name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests.")


def _module_refs(loaded, registry):
    """For every loaded module: the names of the modules its globals point into
    (through classes, their bases and attributes, containers and defaults)."""
    names = set(loaded)
    containers = (dict, list, tuple, set, frozenset)
    # module-level data objects (and what they directly hold) belong to their module:
    # another module holding one of them shares data with it
    owner = {}
    for n, m in loaded.items():
        for k, v in list(vars(m).items()):
            if isinstance(v, containers) and v:
                owner.setdefault(id(v), n)
                items = list(v.values()) if isinstance(v, dict) else list(v)[:5000]
                for x in items:
                    if isinstance(x, containers) and x:
                        owner.setdefault(id(x), n)

    def add(out, mod):
        if mod in names:
            out.add(mod)

    def walk(v, out, depth, seen):
        if id(v) in seen:
            return
        seen.add(id(v))
        o = owner.get(id(v))
        if o is not None:
            out.add(o)
        if isinstance(v, types.ModuleType):
            add(out, v.__name__)
            return
        if isinstance(v, type):
            try:
                for c in v.__mro__:
                    add(out, getattr(c, "__module__", None))
            except Exception:
                pass
        elif isinstance(v, (types.FunctionType, types.BuiltinFunctionType)):
            add(out, getattr(v, "__module__", None))
        else:
            add(out, getattr(type(v), "__module__", None))
        if depth >= 2:
            return
        if isinstance(v, dict):
            for k, x in list(v.items()):
                walk(k, out, depth + 1, seen)
                walk(x, out, depth + 1, seen)
        elif isinstance(v, (list, tuple, set, frozenset)):
            for x in list(v)[:5000]:
                walk(x, out, depth + 1, seen)
        elif isinstance(v, type):
            for k, x in list(vars(v).items()):
                if v is registry and k == "_classes":
                    continue
                walk(x, out, depth + 1, seen)
        elif isinstance(v, types.FunctionType):
            for x in (v.__defaults__ or ()):
                walk(x, out, depth + 1, seen)

    refs = {}
    for n, m in loaded.items():
        out = set()
        seen = set()
        for k, v in list(vars(m).items()):
            if isinstance(v, types.ModuleType) and v.__name__.startswith(n + "."):
                continue      # a package's own submodule: handled by scrubbing, not a dependency
            walk(v, out, 0, seen)
        out.discard(n)
        refs[n] = out
    return refs


def _partial_purge(changed, refs):
    """Drop the changed modules and every module that holds a reference into them,
    keeping everything else (above all, the dialects the change cannot reach) loaded.
    Returns False when a full purge is needed instead."""
    if refs is None:
        return False
    loaded = {n: m for n, m in list(sys.modules.items()) if m is not None and _ours(n)}
    purge = {n for n in changed if n in loaded}
    if not purge:
        return True
    if any(n == "sqlglot" or n.startswith(("sqlglot.expressions", "sqlglot.dialects.dialect")) for n in purge):
        return False
    if any(n not in refs for n in loaded):
        return False           # something was imported after the map was taken
    try:
        registry = importlib.import_module("sqlglot.dialects.dialect")._Dialect
    except Exception:
        return False
    is_pkg = {n for n, m in loaded.items() if hasattr(m, "__path__")}
    if purge & is_pkg:
        return False           # a package's own code changed: re-import everything
    # packages with code of their own are re-executed like modules; only the lazy
    # dialect package is scrubbed (its attributes are a cache it rebuilds on demand)
    scrub_only = {"sqlglot.dialects"}
    grew = True
    while grew:
        grew = False
        for n in loaded:
            if n in purge or n in scrub_only:
                continue
            if n == "tests" or n.startswith("tests.") or refs[n] & purge:
                if n == "sqlglot":
                    return False
                purge.add(n)
                grew = True
        if len(purge) > 0.6 * len(loaded):
            return False

    def from_purged(v):
        if isinstance(v, types.ModuleType):
            return v.__name__ in purge
        if isinstance(v, (type, types.FunctionType)):
            return getattr(v, "__module__", None) in purge
        return getattr(type(v), "__module__", None) in purge

    for n in purge:
        sys.modules.pop(n, None)
    for n in is_pkg:
        if n in purge:
            continue
        m = loaded[n]
        for k, v in list(vars(m).items()):
            if not k.startswith("__") and from_purged(v):
                try:
                    delattr(m, k)
                except Exception:
                    pass
    reg = registry._classes
    for k, v in list(reg.items()):
        if from_purged(v):
            del reg[k]
    return True


_REALPATH = {}


def _ckey(c):
    f = c.co_filename
    r = _REALPATH.get(f)
    if r is None:
        r = _REALPATH[f] = os.path.realpath(f)
    return (r, c.co_qualname, c.co_firstlineno)


_DEFER_DUCK = {"on": False, "setup": False}
_DEFER_CLASSES = ("tests.test_optimizer",)


class _DeferredConn:
    """A duckdb connection opened in a class setup, made on first use by a test.
    The statements the setup ran on it are kept and run then: they are plain duckdb
    SQL from the test file, so they do the same whenever they run, and most tests of
    the class never touch the connection."""

    def __init__(self, make):
        self.__dict__["_br_make"] = make
        self.__dict__["_br_conn"] = None
        self.__dict__["_br_queue"] = []

    def _br_real(self):
        conn = self.__dict__["_br_conn"]
        if conn is None:
            conn = self.__dict__["_br_make"]()
            for a, k in self.__dict__["_br_queue"]:
                conn.execute(*a, **k)
            self.__dict__["_br_conn"] = conn
        return conn

    def execute(self, *a, **k):
        if self.__dict__["_br_conn"] is None and _DEFER_DUCK["setup"]:
            self.__dict__["_br_queue"].append((a, k))
            return self
        return self._br_real().execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self._br_real(), name)


def _limit_duckdb_threads():
    """One duckdb thread: the host may have many cores and every thread's CPU is billed."""
    try:
        import duckdb
    except Exception:
        return
    real = duckdb.connect
    if getattr(real, "_br_wrapped", False):
        return

    def connect(*a, **k):
        cfg = dict(k.pop("config", None) or {})
        cfg.setdefault("threads", 1)
        if _DEFER_DUCK["on"] and _DEFER_DUCK["setup"]:
            return _DeferredConn(lambda: real(*a, config=cfg, **k))
        return real(*a, config=cfg, **k)

    connect._br_wrapped = True
    duckdb.connect = connect


class Warm:
    """State prepared once in the parent."""

    def __init__(self, test_files, need_refs=False, exec_cache=True):
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        os.chdir(ROOT)
        # third-party modules the tests use
        for m in ("duckdb", "numpy", "pandas", "pandas.testing", "csv", "gzip", "decimal", "datetime"):
            try:
                importlib.import_module(m)
            except Exception:
                pass
        _limit_duckdb_threads()
        import sqlglot  # noqa: F401
        for f in sorted(test_files):
            try:
                importlib.import_module(f[:-3].replace("/", "."))
            except Exception:
                pass
        # load every dialect so lazily imported modules exist here, once
        try:
            from sqlglot.dialects.dialect import Dialects, Dialect
            for d in Dialects:
                if d.value:
                    try:
                        Dialect.get_or_raise(d.value)
                    except Exception:
                        pass
        except Exception:
            pass
        install_sync_pools()
        self.exec_cache = None
        self.exec_loaded = False
        if exec_cache and "tests/test_executor.py" in test_files:
            self.load_exec()
        self.refs = None
        if need_refs:
            # every sqlglot module, so that a data diff never meets one it has no pinned copy of
            for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "sqlglot")):
                for f in sorted(files):
                    if not f.endswith(".py") or f == "__main__.py":
                        continue
                    rel = os.path.relpath(os.path.join(dirpath, f), ROOT)
                    name = modname(rel)
                    if name not in sys.modules:
                        try:
                            importlib.import_module(name)
                        except Exception:
                            pass
            try:
                loaded = {n: m for n, m in list(sys.modules.items()) if m is not None and _ours(n)}
                self.refs = _load_refs(set(loaded))
                if self.refs is None:
                    registry = importlib.import_module("sqlglot.dialects.dialect")._Dialect
                    self.refs = _module_refs(loaded, registry)
            except Exception:
                self.refs = None
        self.table_mod = sys.modules.get("sqlglot.executor.table")
        self.funcs = {}
        for o in gc.get_objects():
            if type(o) is FunctionType:
                c = o.__code__
                if c.co_filename.startswith(ROOT):
                    self.funcs.setdefault(_ckey(c), []).append(o)
        gc.freeze()

    def load_exec(self):
        """The executor tests' tables and recorded duckdb answers (20 MB to unpickle:
        only when one of those tests will run)."""
        if self.exec_loaded or "tests.test_executor" not in sys.modules:
            return
        self.exec_loaded = True
        try:
            self.exec_cache = _load_exec_cache() or _build_exec_cache()
            _load_duck_cache()
        except Exception:
            self.exec_cache = None

    def apply(self, py, data, sig, load_old, use_cache=True):
        """In the child: make the patched sources live.  py: [(rel, text, new_code)]."""
        reimport = False
        swaps = []
        for rel, text, new_code in py:
            name = modname(rel)
            if new_code is None or name not in sys.modules:
                reimport = True
                continue
            old_code = load_old(rel)
            pairs = plan_swaps(old_code, new_code, sig)
            if pairs is None:
                reimport = True
                continue
            swaps.extend(pairs)
        if reimport:
            sources = {modname(rel): (os.path.join(ROOT, rel), text) for rel, text, _c in py}
            if os.environ.get("BR_FULL_PURGE") or not _partial_purge(set(sources), self.refs):
                for name in list(sys.modules):
                    if name == "sqlglot" or name.startswith(("sqlglot.", "tests.")) or name == "tests":
                        del sys.modules[name]
            sys.meta_path.insert(0, PatchedFinder(sources))
            importlib.invalidate_caches()
        else:
            for old, new in swaps:
                for fn in self.funcs.get(_ckey(old), ()):
                    if fn.__code__.co_code == old.co_code:
                        fn.__code__ = new
        install_data_overlay({os.path.join(ROOT, rel): text for rel, text in data})
        sql_only = bool(data) and all(rel in SQL_FIXTURES for rel, _t in data) and not os.environ.get("BR_NO_SQLONLY")
        if not reimport and self.exec_cache is not None and (not data or sql_only):
            touched = {_ckey(old) for old, _new in swaps}
            if not (touched & self.exec_cache["codes"]):
                # (an edited query file: the tables are the same, the queries are read again)
                _use_exec_cache(self.exec_cache, reload_sqls=sql_only)
        elif reimport and use_cache and self.exec_cache is not None and not data:
            # the tables are still good when nothing their setup ran changed and their
            # class was not re-imported
            changed = {os.path.realpath(os.path.join(ROOT, rel)) for rel, _t, _c in py}
            setup_files = {c[0] for c in self.exec_cache["codes"]}
            if not (changed & setup_files) and self.table_mod is not None and \
                    sys.modules.get("sqlglot.executor.table") is self.table_mod:
                try:
                    _use_exec_cache(self.exec_cache)
                except Exception:
                    pass
        return reimport


def child_main(warm, job, out_fd):
    """job: dict with py, data, order."""
    # fewer young-generation passes: the tests build many short-lived trees
    a, b, c = (int(x) for x in os.environ.get("BR_GC", "50000,20,100").split(","))
    gc.set_threshold(a, b, c)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        import analyze
        reimport = warm.apply(job["py"], job["data"], analyze._sig,
                              lambda rel: analyze.load_old_code(ROOT, rel, open(os.path.join(ROOT, rel)).read()))
        if reimport:
            install_sync_pools()
            # re-imported test modules need the sync pool too
            for _num, tid in job["order"]:
                try:
                    load_case(tid)
                except BaseException:
                    pass
            install_sync_pools()
        # (a patch to the tests themselves may change what the class setup runs)
        _DEFER_DUCK["on"] = not os.environ.get("BR_NO_DEFER") and \
            not any(rel.startswith("tests/") for rel, _t, _c in job["py"])
        run_tests(job["order"], out_fd, mass_stop=job.get("stop_on_fail", True), dyn=job.get("dyn"))
    except BaseException:
        pass
    os._exit(0)


def diff_main(warm, py, out_fd):
    """Child: re-import with the patch and report how module and class data changed."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    res = None
    try:
        import analyze
        import datadiff
        old = {n: m for n, m in list(sys.modules.items()) if m is not None and _ours(n)}
        changed = {modname(rel) for rel, _t, _c in py}
        before = _global_state()
        if all(n in old for n in changed):
            reimport = warm.apply(py, [], analyze._sig,
                                  lambda rel: analyze.load_old_code(ROOT, rel, open(os.path.join(ROOT, rel)).read()),
                                  use_cache=False)
            if reimport:
                purged = [n for n in old if n not in sys.modules and n != "tests" and not n.startswith("tests.")]
                pairs = {}
                for n in purged:
                    pairs[n] = (old[n], importlib.import_module(n))
                if all(n in pairs for n in changed) and _global_state() == before:
                    res = datadiff.describe(datadiff.diff_modules(pairs, set(pairs)), ROOT)
    except BaseException:
        res = None
    try:
        data = pickle.dumps(res)
    except BaseException:
        data = pickle.dumps(None)
    view = memoryview(data)
    while view:
        try:
            n = os.write(out_fd, view)
        except BlockingIOError:
            time.sleep(0.001)
            continue
        view = view[n:]
    os._exit(0)


def _global_state():
    """Interpreter state an import could change outside any module's namespace."""
    import logging
    import warnings
    import decimal
    loggers = {}
    for name, lg in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(lg, logging.Logger):
            loggers[name] = (lg.level, lg.disabled, lg.propagate, len(lg.handlers), len(lg.filters))
    root = logging.getLogger()
    return (sys.getrecursionlimit(), dict(os.environ), list(sys.path), repr(warnings.filters),
            (root.level, len(root.handlers)), loggers, logging.root.manager.disable,
            repr(decimal.getcontext()), sys.getswitchinterval(), sys.int_info, sys.get_int_max_str_digits())


def run_diffs(warm, items, parallel=2, deadline=None):
    """items: [(cid, py)].  Returns {cid: entries or None}."""
    import select
    pending = list(items)
    if not os.environ.get("BR_NO_FREEZE"):
        gc.freeze()     # what this process built since warming up is not the children's garbage
    active = {}
    out = {}
    while pending or active:
        if deadline is not None and time.time() > deadline:
            for fd, (cid, pid, _buf) in list(active.items()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                os.waitpid(pid, 0)
                os.close(fd)
                out[cid] = None
            active = {}
            for cid, _py in pending:
                out[cid] = None
            break
        while pending and len(active) < parallel:
            cid, py = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                diff_main(warm, py, w)
                os._exit(0)
            os.close(w)
            active[r] = (cid, pid, [])
        ready, _, _ = select.select(list(active), [], [], 0.05)
        for fd in ready:
            cid, pid, buf = active[fd]
            chunk = os.read(fd, 1 << 20)
            if chunk:
                buf.append(chunk)
                continue
            os.close(fd)
            os.waitpid(pid, 0)
            del active[fd]
            try:
                out[cid] = pickle.loads(b"".join(buf))
            except Exception:
                out[cid] = None
    return out


class Running:
    def __init__(self, cid, pid, rfd, order, deadline_for):
        self.cid = cid
        self.pid = pid
        self.rfile = os.fdopen(rfd, "r")
        self.order = order
        self.results = {}
        self.done = False
        self.deadline_for = deadline_for
        self.started = time.monotonic()
        self.buf = ""


CHILD_CPU = {}
TEST_SECS = {}


def run_jobs(warm, jobs, parallel=2, log=None, deadline=None):
    """jobs: list of (cid, job dict incl. 'order' and 'budget' seconds per test).
    Returns {cid: {num: 'P'|'F'|'T'}} - tests missing from a result were not run."""
    import select
    pending = list(jobs)
    if not os.environ.get("BR_NO_FREEZE"):
        gc.freeze()     # what this process built since warming up is not the children's garbage
    active = []
    out = {}
    while pending or active:
        if deadline is not None and time.time() > deadline:
            # out of time: whatever is unverified stays scheduled
            for a in active:
                a.done = True
            pending = []
        while pending and len(active) < parallel:
            cid, job = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                child_main(warm, job, w)
                os._exit(0)
            os.close(w)
            os.set_blocking(r, False)
            st = Running(cid, pid, r, job["order"], job["limits"])
            st.next_idx = 0
            st.stop_on_fail = job.get("stop_on_fail", True)
            st.t_last = time.monotonic()
            active.append(st)
        rlist = [a.rfile for a in active]
        ready, _, _ = select.select(rlist, [], [], 0.05)
        now = time.monotonic()
        for a in list(active):
            if a.rfile in ready:
                try:
                    chunk = os.read(a.rfile.fileno(), 65536).decode()
                except BlockingIOError:
                    chunk = None
                if chunk == "":
                    a.done = True
                elif chunk:
                    a.buf += chunk
                    while "\n" in a.buf:
                        line, a.buf = a.buf.split("\n", 1)
                        if line == "DONE":
                            a.done = True
                            continue
                        num, st, _secs = line.split()
                        if os.environ.get("BR_TIMES"):
                            sys.stderr.write("T %s %s %s %s\n" % (a.cid, num, st, _secs))
                        a.results[int(num)] = st
                        try:
                            TEST_SECS[(a.cid, int(num))] = float(_secs)
                        except ValueError:
                            pass
                        a.next_idx += 1
                        a.t_last = now
                        if st == "T":
                            a.done = True
            if not a.done:
                # time limit for the test in progress
                if a.next_idx < len(a.order):
                    num = a.order[a.next_idx][0]
                    lim = a.deadline_for.get(num, 5.0)
                    if now - a.t_last > lim:
                        a.results[num] = "T"
                        a.done = True
            if a.done:
                try:
                    os.kill(a.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                cpu = -1.0
                try:
                    _p, _st, ru = os.wait4(a.pid, 0)
                    cpu = ru.ru_utime + ru.ru_stime
                    CHILD_CPU[a.cid] = cpu
                except ChildProcessError:
                    pass
                a.rfile.close()
                out[a.cid] = a.results
                active.remove(a)
                if log:
                    log("ran %s: %d/%d in %.2fs cpu %.2f" % (a.cid, len(a.results), len(a.order), now - a.started, cpu))
    return out


# ------------------------------------------------- executor test fixtures

class _NoConn:
    def execute(self, *a, **k):
        return self

    def close(self):
        pass


EXEC_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "execcache.pickle")


REFS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refs.pickle")


def _load_refs(loaded):
    """The module reference map made while the image was built, if it covers
    every module loaded now (it was made with at least as many loaded)."""
    try:
        with open(REFS_FILE, "rb") as f:
            data = pickle.load(f)
        if data["root"] != os.path.realpath(ROOT) or not loaded <= set(data["refs"]):
            return None
        return data["refs"]
    except Exception:
        return None


def save_refs(path=REFS_FILE):
    """Build time: load sqlglot and every test module and map module references."""
    tests = set()
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "tests")):
        for f in files:
            if f.startswith("test_") and f.endswith(".py"):
                tests.add(os.path.relpath(os.path.join(dirpath, f), ROOT))
    global REFS_FILE
    keep, REFS_FILE = REFS_FILE, "/nonexistent"
    try:
        warm = Warm(tests, need_refs=True)
    finally:
        REFS_FILE = keep
    if warm.refs is None:
        return
    with open(path, "wb") as f:
        pickle.dump({"root": os.path.realpath(ROOT), "refs": warm.refs}, f, protocol=5)


def _load_exec_cache():
    """The executor tables, built from the pinned commit while the image was built."""
    try:
        with open(EXEC_CACHE_FILE, "rb") as f:
            data = f.read()
        enabled = gc.isenabled()
        gc.disable()
        try:
            cache = pickle.loads(data)
        finally:
            if enabled:
                gc.enable()
        if cache.pop("root", None) != os.path.realpath(ROOT):
            return None
        return cache
    except Exception:
        return None


def save_exec_cache(path=EXEC_CACHE_FILE):
    """Build time: run the executor tests' setup once and store what it builds."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    _limit_duckdb_threads()
    cache = _build_exec_cache()
    cache["root"] = os.path.realpath(ROOT)
    with open(path, "wb") as f:
        pickle.dump(cache, f, protocol=5)


def _build_exec_cache():
    """Run TestExecutor.setUpClass once here, without duckdb, and remember
    the tables it builds and every function it ran."""
    import duckdb
    import tests.test_executor as te
    mon = sys.monitoring
    tool = 2
    codes = set()

    def on_start(code, _off):
        if code.co_filename.startswith(ROOT):
            codes.add(_ckey(code))
        return mon.DISABLE

    mon.use_tool_id(tool, "br-setup")
    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.set_events(tool, mon.events.PY_START)
    real = duckdb.connect
    duckdb.connect = lambda *a, **k: _NoConn()
    try:
        te.TestExecutor.setUpClass()
    finally:
        duckdb.connect = real
        mon.set_events(tool, 0)
        mon.register_callback(tool, mon.events.PY_START, None)
        mon.free_tool_id(tool)
    k = te.TestExecutor
    cache = {name: getattr(k, name) for name in ("tpch_tables", "tpcds_tables", "tpch_sqls", "tpcds_sqls")}
    for name in ("tpch_conn", "tpcds_conn", "cache") + tuple(cache):
        try:
            delattr(k, name)
        except AttributeError:
            pass
    cache["codes"] = codes
    return cache


DUCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duckcache.pickle")
_DUCK_CACHE = {}
_DUCK_RECORD = None


class _CachedResult:
    """What duckdb returned for this exact query at the pinned commit."""

    def __init__(self, df):
        self._df = df

    def fetchdf(self):
        return self._df

    def df(self):
        return self._df


def _load_duck_cache():
    global _DUCK_CACHE
    try:
        with open(DUCK_FILE, "rb") as f:
            data = pickle.load(f)
        if data.get("root") == os.path.realpath(ROOT):
            _DUCK_CACHE = data["results"]
    except Exception:
        _DUCK_CACHE = {}


def save_duck_cache(path=DUCK_FILE):
    """Build time: run the executor tests and keep every duckdb result they fetch."""
    global _DUCK_RECORD
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    _limit_duckdb_threads()
    install_sync_pools()
    cache = _load_exec_cache() or _build_exec_cache()
    _use_exec_cache(cache)
    install_sync_pools()
    import tests.test_executor as te
    _DUCK_RECORD = {}
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(te.TestExecutor)
    res = unittest.TestResult()
    suite.run(res)
    if res.failures or res.errors:
        return
    with open(path, "wb") as f:
        pickle.dump({"root": os.path.realpath(ROOT), "results": _DUCK_RECORD}, f, protocol=5)


class _LazyConn:
    """A duckdb connection made on first use, whose CSV-backed views are created
    only for the tables a query names (any other use creates them all)."""

    def __init__(self, directory, schema):
        object.__setattr__(self, "_br_dir", directory)
        object.__setattr__(self, "_br_schema", schema)
        object.__setattr__(self, "_br_conn", None)
        object.__setattr__(self, "_br_made", set())

    def _br_get(self, sql=None):
        import duckdb
        import re
        c = object.__getattribute__(self, "_br_conn")
        if c is None:
            c = duckdb.connect()
            object.__setattr__(self, "_br_conn", c)
        made = object.__getattribute__(self, "_br_made")
        schema = object.__getattribute__(self, "_br_schema")
        directory = object.__getattribute__(self, "_br_dir")
        for table, columns in schema.items():
            if table in made:
                continue
            if sql is not None and not re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(table),
                                                 sql, re.IGNORECASE):
                continue
            file_name = f"{directory}{table}.csv.gz"
            c.execute(
                f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
            )
            made.add(table)
        return c

    def execute(self, query, *a, **k):
        if isinstance(query, str) and not a and not k:
            key = (object.__getattribute__(self, "_br_dir"), query)
            if _DUCK_RECORD is not None:
                res = self._br_get(query).execute(query)
                df = res.fetchdf()
                _DUCK_RECORD[key] = df.copy(deep=True)
                return _CachedResult(df)
            if key in _DUCK_CACHE:
                return _CachedResult(_DUCK_CACHE[key].copy(deep=True))
        return self._br_get(query if isinstance(query, str) else None).execute(query, *a, **k)

    def __getattr__(self, name):
        if name == "close" and object.__getattribute__(self, "_br_conn") is None:
            return lambda: None
        return getattr(self._br_get(), name)

    def __setattr__(self, name, value):
        setattr(self._br_get(), name, value)

    def __enter__(self):
        return self._br_get().__enter__()

    def __exit__(self, *a):
        return self._br_get().__exit__(*a)


SQL_FIXTURES = ("tests/fixtures/optimizer/tpc-h/tpc-h.sql", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql")


def _use_exec_cache(cache, reload_sqls=False):
    import duckdb
    import tests.test_executor as te
    from tests.helpers import TPCH_SCHEMA, TPCDS_SCHEMA

    def setUpClass(cls):
        # the duckdb side is set up only as far as the tests ask for it
        cls.tpch_conn = _LazyConn(te.DIR_TPCH, TPCH_SCHEMA)
        cls.tpcds_conn = _LazyConn(te.DIR_TPCDS, TPCDS_SCHEMA)
        cls.tpch_tables = cache["tpch_tables"]
        cls.tpcds_tables = cache["tpcds_tables"]
        cls.cache = {}
        if reload_sqls:
            cls.tpch_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
            cls.tpcds_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
        else:
            cls.tpch_sqls = cache["tpch_sqls"]
            cls.tpcds_sqls = cache["tpcds_sqls"]

    te.TestExecutor.setUpClass = classmethod(setUpClass)
