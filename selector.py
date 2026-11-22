"""Blast Radius selector.

For every commit:
  1. Static reach: which tests can execute the changed code (a coverage map
     built from the pinned commit, line by line, plus which tests open which
     data files).
  2. Verification: tests that are cheap to run are run here, against the
     patched code, in a forked child.  Only those that fail are kept.
  3. Tests that are too expensive to verify are scheduled blind.
  4. What is kept is packed onto the number of workers that costs least.
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DATA = os.environ.get("BR_DATA", "/data")
ROOT = os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot")
os.environ["SQLGLOT_ROOT"] = ROOT

WORKER_MS = 2000
TIME_BUDGET = float(os.environ.get("BR_TIME_BUDGET", "140"))   # seconds of wall time for verification
MAX_WORKERS = 8
# tests that need a fresh interpreter with the patched tree: never verified here
NO_VERIFY = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}
# the files `import sqlglot` loads in a new interpreter (at the pinned commit)
LAZY_CLOSURE = {"sqlglot/" + x for x in (
    "__init__.py _typing.py dialects/__init__.py dialects/dialect.py diff.py errors.py expressions/__init__.py "
    "expressions/aggregate.py expressions/array.py expressions/builders.py expressions/constraints.py "
    "expressions/core.py expressions/datatypes.py expressions/ddl.py expressions/dml.py expressions/functions.py "
    "expressions/json.py expressions/math.py expressions/properties.py expressions/query.py expressions/string.py "
    "expressions/temporal.py generator.py helper.py jsonpath.py optimizer/__init__.py optimizer/scope.py parser.py "
    "parsers/__init__.py parsers/base.py schema.py time.py tokenizer_core.py tokens.py trie.py typing/__init__.py "
    "_version.py").split()}
GENMEM_EST = 0.12
CHILD_OVERHEAD = 0.03   # fork + patch + exit, swap mode
REIMPORT_OVERHEAD = 0.6  # extra when module- or class-level code changed
WARM_COST = 0.7
EXEC_CLASS = "tests/test_executor.py::TestExecutor"
HEAVY = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds",
    "tests/test_parser.py::TestParser::test_max_nodes": "max_nodes",
    "tests/test_generator.py::TestGenerator::test_generator_memory": "genmem",
}
if os.environ.get("BR_NO_GENMEM"):
    del HEAVY["tests/test_generator.py::TestGenerator::test_generator_memory"]
MIN_SUPPORT = 20       # other tests that must also run a changed line for a probe to stand in
PROBE_QUERIES = int(os.environ.get("BR_PROBE_QUERIES", "5"))
PROBE_EXTRA = int(os.environ.get("BR_PROBE_EXTRA", "2"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def pack(ms_by_test, k):
    """Longest first onto the least loaded worker."""
    loads = [0] * k
    bins = [[] for _ in range(k)]
    for num, ms in sorted(ms_by_test.items(), key=lambda x: (-x[1], x[0])):
        i = loads.index(min(loads))
        loads[i] += ms
        bins[i].append(num)
    return max(loads), [b for b in bins if b]


def best_split(ms_by_test):
    if not ms_by_test:
        return 0, []
    best = None
    for k in range(1, min(MAX_WORKERS, len(ms_by_test)) + 1):
        mx, bins = pack(ms_by_test, k)
        cost = mx + WORKER_MS * len(bins)
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best


FAILED = {}
REACH = {}
UNRUN = {}
BLIND = {}
HEAVY_NUMS = {}
WARM = {}


def main():
    if not os.environ.get("BR_NO_PGC"):
        import gc
        gc.set_threshold(200000, 50, 100)      # this process mostly loads tables and modules
    t_start = time.time()
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    num_by_id = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    commits = sorted(f[:-6] for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch"))

    import analyze
    mp = analyze.Map(os.path.join(HERE, "map.bin"))
    costs = json.load(open(os.path.join(HERE, "costs.json")))
    run_cost = {}
    for t in tests:
        c = costs["cost"].get(t["id"])
        run_cost[t["number"]] = c if c is not None else t["seconds"] * 1.5 + 0.01
    # after a re-import the executor tests set up duckdb for real: their old costs
    run_cost_reimport = dict(run_cost)
    for t in tests:
        c = costs.get("cost_reimport", {}).get(t["id"])
        if c is not None:
            run_cost_reimport[t["number"]] = c
    setup_cost = costs["setup"]
    import pickle
    import zlib
    with open(os.path.join(HERE, "probes.bin"), "rb") as f:
        probes = pickle.loads(zlib.decompress(f.read()))
    probes["max_nodes"]["lines"] = set(probes["max_nodes"]["lines"])
    heavy = {}
    for t in tests:
        if t["id"] in HEAVY:
            heavy[t["number"]] = HEAVY[t["id"]]
            HEAVY_NUMS[t["number"]] = HEAVY[t["id"]]
    ctx = {"mp": mp, "probes": probes, "heavy": heavy, "map_index": {i: n for n, i in enumerate(mp.ids)},
           "casemap": None, "casemap_path": os.path.join(HERE, "casemap.bin"),
           "callmap": None, "callmap_path": os.path.join(HERE, "callmap.bin"), "num_by_id": num_by_id}
    map_nums = [num_by_id.get(i) for i in mp.ids]
    unmapped = [t["number"] for t in tests if t["id"] not in set(mp.ids)]
    by_num = {t["number"]: t for t in tests}

    answers = {}
    plans = {}
    reach = {}
    fix_cand = {}
    later = []
    for cid in commits:
        text = open(os.path.join(DATA, "commits", cid + ".patch"), encoding="utf-8", errors="replace").read()
        try:
            r = analyze.analyze(text, ROOT, mp)
        except Exception as e:  # never let one patch take the run down
            log("analysis failed for %s: %r" % (cid, e))
            r = analyze.Reach()
            r.all("analysis failed")
            r.runnable = False
        if r.everything:
            cand = set(ms)
        else:
            cand = set()
            b = r.bits
            i = 0
            while b:
                if b & 1 and map_nums[i] is not None:
                    cand.add(map_nums[i])
                b >>= 1
                i += 1
            if cand or r.py or r.data:
                if r.py and unmapped:
                    cand |= set(unmapped)
        if not cand:
            answers[cid] = []
            continue
        if r.data and len(cand) <= FIX_BLIND and r.runnable and not r.everything and \
                all(_real_fixture_edit(p, t) for p, t in r.data) and _same_code(r):
            fix_cand[cid] = sorted(cand)       # (decided below, once sqlglot is loaded)
        reimport = any(c is None for _p, _t, c in r.py) or _needs_reimport(r)
        # (an edited fixture changes the queries, so recorded duckdb results do not apply either)
        exec_setup = (r.data and not _sql_only(r.data)) or \
            any(p.startswith(("sqlglot/executor/", "tests/", "sqlglot/helper")) for p, _t, _c in r.py)
        costs_here = run_cost_reimport if exec_setup else run_cost
        reach[cid] = (r, cand, reimport, costs_here)
        REACH[cid] = r
        if _diffable(r, cand, reimport, run_cost) and not os.environ.get("BR_EAGER_PLAN"):
            later.append(cid)      # (planned after the data diff, most likely for far fewer tests)
        else:
            plans[cid] = plan(cid, cand, r, ms, costs_here, setup_cost, by_num, reimport, ctx)

    warm = None
    try:
        warm = narrow_by_data(plans, reach, answers, tests, map_nums, unmapped, ms, run_cost, setup_cost,
                              by_num, ctx, t_start)
    except Exception as e:  # the static plans stand
        log("data diff failed: %r" % (e,))
    for cid in later:
        if cid not in plans and cid not in answers:      # no narrowing: every candidate
            r, cand, reimport, costs_here = reach[cid]
            plans[cid] = plan(cid, cand, r, ms, costs_here, setup_cost, by_num, reimport, ctx)

    # an edited fixture statement and the one test that reads it: when the statement no
    # longer parses, or parses to something else, the test all but always fails, and
    # running it first costs more than it can save
    try:
        for cid, keep in fix_cand.items():
            if cid in plans and plans[cid]["verify"] and all(_breaks_statement(p, t) for p, t in REACH[cid].data):
                del plans[cid]
                answers[cid] = keep
                log("%s: broken fixture statement, %d test(s) scheduled unverified" % (cid, len(keep)))
    except Exception as e:  # the plans stand
        log("fixture check failed: %r" % (e,))

    jobs = []
    for cid, p in plans.items():
        if p["verify"]:
            jobs.append((cid, p))
        else:
            answers[cid] = p["blind"]
    results = {}
    if os.environ.get("BR_PROF"):
        log("prof: before jobs parent %.2f" % time.process_time())
    if jobs:
        import runner
        t_w = time.process_time()
        need_refs = any(p.get("reimport") for _c, p in jobs)
        if warm is None or (need_refs and warm.refs is None):
            warm = runner.Warm({by_num[n]["file"] for _c, p in jobs for n, _ in p["order"]}, need_refs=need_refs,
                               exec_cache=False, lazy_pandas=LAZY_PANDAS)
        # pandas itself only when a job runs a test that is known to use it: an executor
        # test, or an optimizer test that hands duckdb's answers to pandas (run whole, or
        # on the fixture items a code change reaches); a test that turns out to need it
        # anyway imports it in its own process
        if LAZY_PANDAS and any(_uses_pandas(cid, p, by_num) for cid, p in jobs):
            runner.real_pandas()
        log("warm cpu %.2f, analysis cpu %.2f" % (time.process_time() - t_w, t_w))
        jobs.sort(key=lambda j: -j[1]["est"])

        def uses_tables(p):
            # 1: a TestExecutor test (the reduced TPC-DS probe brings its own tables);
            # 2: test_execute_tpcds itself, the one reader of the TPC-DS tables
            if runner.needs_tpcds(p["order"]):
                return 2
            return int(any(by_num[n]["file"] == "tests/test_executor.py" and
                           not (isinstance(tid, str) and tid.startswith("PROBE:tpcds2:")) for n, tid in p["order"]))

        # the jobs that do not need the executor tables run first, forked from a process
        # that has not loaded them yet (20 MB of pickle; every fork carries what is loaded)
        results = {}
        for want in (0, 1, 2):
            part = [(cid, p) for cid, p in jobs if uses_tables(p) == want]
            if not part:
                continue
            if want:
                warm.load_exec()
            if want == 2:
                warm.load_tpcds()
            results.update(runner.run_jobs(
                warm, [(cid, {"py": p["py"], "data": p["data"], "order": p["order"],
                              "limits": p["limits"], "stop_on_fail": True,
                              "dyn": {"ms": {n: ms[n] for n, _t in p["order"]},
                                      "rc": {n: p["rc"].get(n, 0.0) for n, _t in p["order"]},
                                      "kept": [ms[n] for n in p["blind"]]}})
                       for cid, p in part], parallel=PARALLEL, log=log, deadline=t_start + TIME_BUDGET))
    WARM["warm"] = warm
    if os.environ.get("BR_DEBUG"):
        with open(os.environ["BR_DEBUG"], "a") as dbg:
            for cid, p in jobs:
                for n, tid in p["order"]:
                    name = tid if isinstance(tid, str) else "FILTER:" + tid[1]
                    dbg.write("%s\t%d\t%s\t%.4f\n" % (cid, n, name, run_cost.get(n, 0.0) if not isinstance(tid, tuple) else -1))
    if os.environ.get("BR_DEBUG"):
        with open(os.environ["BR_DEBUG"] + ".res", "a") as dbg:
            for cid, p in jobs:
                res = results.get(cid, {})
                for n, tid in p["order"]:
                    kind = tid[0] if isinstance(tid, tuple) else ("PROBE" if tid.startswith("PROBE:") else "FULL")
                    dbg.write("%s\t%d\t%s\t%s\n" % (cid, n, kind, res.get(n, "-")))
                for n in p["blind"]:
                    dbg.write("%s\t%d\tBLIND\tB\n" % (cid, n))
    for cid, p in jobs:
        res = results.get(cid, {})
        keep = set(p["blind"])
        for num, _tid in p["order"]:
            st = res.get(num)
            if st != "P":          # failed, timed out, or never run
                keep.add(num)
        answers[cid] = sorted(keep)
        FAILED[cid] = [num for num, _tid in p["order"] if res.get(num) in ("F", "T")]
        UNRUN[cid] = [num for num, _tid in p["order"] if res.get(num) not in ("F", "T", "P")]
        BLIND[cid] = list(p["blind"])
        nfail = sum(1 for v in res.values() if v in ("F", "T"))
        log("%s verified %d/%d fail=%d keep=%d est=%.2f" % (cid, len(res), len(p["order"]), nfail, len(keep), p["est"]))

    all_bins = {}
    for cid in commits:
        keep = answers.get(cid, [])
        _cost, bins = best_split({n: ms[n] for n in keep})
        all_bins[cid] = bins
    try:
        _diagnose(all_bins, ms, commits, plans)
    except Exception as e:  # noqa: BLE001
        log("diagnose failed: %r" % (e,))
    out = [json.dumps({"commit": cid, "workers": all_bins[cid]}, separators=(",", ":")) for cid in commits]
    sys.stdout.write("".join(line + "\n" for line in out))
    sys.stdout.flush()
    log("done in %.2fs" % (time.time() - t_start))
    if os.environ.get("BR_PROF"):
        import resource
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        log("prof: end parent %.2f children %.2f" % (time.process_time(), ru.ru_utime + ru.ru_stime))
    try:
        _calibrate()
    except BaseException as e:  # noqa: BLE001  (the output is already written)
        log("calibration failed: %r" % (e,))


def _diagnose(all_bins, ms, commits, plans):
    """Diagnostics only (with BR_DIAG set): add tests that cannot change a
    miss but shift the simulated seconds by known amounts, so the board's number
    carries this run's own CPU split: +2 s per extra worker (a 0 ms test alone)
    counts the CPU of re-importing commits in quarter seconds, +1 ms per 1 ms test added to a
    busiest worker counts the CPU of commits with a probe in fiftieths."""
    if not os.environ.get("BR_DIAG"):
        return
    import resource
    me = resource.getrusage(resource.RUSAGE_SELF)
    ch = resource.getrusage(resource.RUSAGE_CHILDREN)
    if os.environ.get("BR_DIAG") == "12":
        # kernel time (page faults, forks): the children's in tenths, this process's in 1/200
        k = min(140, int(round(ch.ru_stime / 0.1)))
        m = min(1999, int(round(me.ru_stime / 0.005)))
        log("diag12: sys self %.2f children %.2f; minflt self %d children %d" %
            (me.ru_stime, ch.ru_stime, me.ru_minflt, ch.ru_minflt))
        _encode(all_bins, ms, commits, k, m)
        return
    if os.environ.get("BR_DIAG") == "13":
        # minor page faults: the children's in 20 thousands, this process's in hundreds
        k = min(140, int(round(ch.ru_minflt / 20000.0)))
        m = min(1999, int(round(me.ru_minflt / 100.0)))
        log("diag13: minflt self %d children %d" % (me.ru_minflt, ch.ru_minflt))
        _encode(all_bins, ms, commits, k, m)
        return
    if os.environ.get("BR_DIAG") == "11":
        # how many jobs run tests that use pandas (it is a quarter of the warm-up)
        tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
        by_num = {t["number"]: t for t in tests}
        users = {"tests/test_optimizer.py::TestOptimizer::" + x for x in (
            "test_canonicalize_internal_names", "test_eliminate_joins", "test_merge_subqueries", "test_optimize",
            "test_optimize_joins", "test_qualify_columns", "test_simplify", "test_unnest_subqueries",
            "test_tpch", "test_tpcds")}
        k1 = k1s = k2 = k3 = k4 = kp = 0
        for cid, p in plans.items():
            if not p.get("verify"):
                continue
            r = REACH.get(cid)
            code = bool(r is not None and (r.lines or r.classes or any(c is None for _p, _t, c in r.py)))
            f1 = f1s = f2 = f3 = f4 = fp = False
            for n, tid in p["order"]:
                t = by_num[n]
                if isinstance(tid, str) and tid.startswith("PROBE:tpcds2:"):
                    fp = True
                elif t["file"] == "tests/test_executor.py":
                    if isinstance(tid, tuple) and tid[0] == "SETUP":
                        f1s = True
                    else:
                        f1 = True
                elif t["id"] in users:
                    if isinstance(tid, tuple):
                        if code:
                            f3 = True
                        else:
                            f4 = True
                    else:
                        f2 = True
            k1 += f1; k1s += f1s; k2 += f2; k3 += f3; k4 += f4; kp += fp
        log("diag11: executor %d setup-only %d full %d filtered(code) %d filtered(fixture) %d probe %d" %
            (k1, k1s, k2, k3, k4, kp))
        _encode(all_bins, ms, commits, min(13, k1) * 10 + min(9, k2),
                min(9, k3) * 100 + min(9, k4) * 10 + min(9, kp) + (1000 if k1s else 0))
        return
    if os.environ.get("BR_DIAG") == "10":
        # does running tests pay: per commit, the CPU spent against the simulated seconds
        # it saved over scheduling every candidate unverified
        import runner

        def cost(keep):
            if not keep:
                return 0
            _c, b = best_split({n: ms[n] for n in keep})
            return max(sum(ms[n] for n in x) for x in b) + 2000 * len(b)
        bad_cpu = bad_save = good_cpu = good_save = 0.0
        n_bad = 0
        rows = []
        for cid, cpu in runner.CHILD_CPU.items():
            p = plans.get(cid)
            if p is None:
                continue
            cand = {n for n, _t in p.get("order", ())} | set(p.get("blind", ()))
            keep = {n for b in all_bins[cid] for n in b}
            save = (cost(cand | keep) - cost(keep)) / 1000.0
            rows.append((cpu - save, cid, cpu, save, len(cand), len(keep)))
            if save < cpu:
                bad_cpu += cpu
                bad_save += save
                n_bad += 1
            else:
                good_cpu += cpu
                good_save += save
        rows.sort(reverse=True)
        for d, cid, cpu, save, nc, nk in rows[:8]:
            log("diag10: %s cpu %.2f saved %.2f cand %d kept %d" % (cid, cpu, save, nc, nk))
        log("diag10: unprofitable %d commits cpu %.2f saved %.2f; profitable cpu %.2f saved %.2f" %
            (n_bad, bad_cpu, bad_save, good_cpu, good_save))
        arg = os.environ.get("BR_DIAG_ARG", "0")
        if arg == "0":
            k, m = min(140, int(round(bad_cpu / 0.1))), min(1999, int(round(bad_save / 0.05)))
        elif arg == "1":
            k, m = min(140, n_bad), min(1999, int(round(good_cpu / 0.02)))
        else:
            # the worst commit: CPU in tenths, and where its change is
            k, m = 0, 0
            rank = int(arg) - 2
            if rank < len(rows):
                d, cid, cpu, save, nc, nk = rows[rank]
                r = REACH.get(cid)
                files = _source_files()
                fi = 199
                if r is not None and r.py:
                    path = sorted(x for x, _t, _c in r.py)[0]
                    fi = files.index(path) if path in files else 198
                elif r is not None and r.data:
                    fi = 197
                k = min(140, int(round(cpu / 0.1)))
                m = fi * 10 + min(9, int(save / 0.5))
        _encode(all_bins, ms, commits, k, m)
        return
    if os.environ.get("BR_DIAG") == "9":
        # where the verification CPU goes, by kind of test
        import runner
        tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
        tid = {t["number"]: t["id"] for t in tests}
        cat = {}
        for (cid, n), secs in runner.TEST_SECS.items():
            i = tid.get(n, "")
            if "test_execute_tpcds" in i:
                c = "tpcds"
            elif i.endswith("TestParser::test_max_nodes"):
                c = "maxnodes"
            elif "test_generator_memory" in i:
                c = "genmem"
            elif "::TestOptimizer::" in i:
                c = "optimizer"
            elif "::TestExecutor::" in i:
                c = "executor"
            else:
                c = "rest"
            cat[c] = cat.get(c, 0.0) + secs
        cat["overhead"] = sum(runner.CHILD_CPU.values()) - sum(cat.values())
        log("diag9: " + " ".join("%s %.2f" % kv for kv in sorted(cat.items())) + " jobs %d" % len(runner.CHILD_CPU))
        trio = (("tpcds", "maxnodes", "genmem"), ("overhead", "optimizer", "executor"),
                ("rest", "jobs", "none"))[int(os.environ.get("BR_DIAG_ARG", "0"))]
        cat["jobs"] = len(runner.CHILD_CPU) * 0.2
        k = min(140, int(round(max(0.0, cat.get(trio[0], 0.0)) / 0.2)))
        y = min(49, int(round(max(0.0, cat.get(trio[1], 0.0)) / 0.2)))
        z = min(39, int(round(max(0.0, cat.get(trio[2], 0.0)) / 0.25)))
        _encode(all_bins, ms, commits, k, y * 40 + z)
        return
    if os.environ.get("BR_DIAG") == "8":
        # the CPU split: this process (analysis, warm-up, data diffs' parent side) in tenths,
        # the verification children in fiftieths
        import runner
        kids = sum(runner.CHILD_CPU.values())
        mine = me.ru_utime + me.ru_stime
        k = min(140, int(round(mine / 0.1)))
        m = min(1999, int(round(kids / 0.02)))
        log("diag8: self %.2f children(verify) %.2f all children %.2f" % (mine, kids, ch.ru_utime + ch.ru_stime))
        _encode(all_bins, ms, commits, k, m)
        return
    if os.environ.get("BR_DIAG") == "7":
        # where the CPU goes: the rank-th costliest commit's CPU, file and kind
        import runner
        rows = sorted(((c, cid) for cid, c in runner.CHILD_CPU.items()), reverse=True)
        rank = int(os.environ.get("BR_DIAG_ARG", "0"))
        k, m = 0, 1999
        if rank < len(rows):
            c, cid = rows[rank]
            r = REACH.get(cid)
            p = plans.get(cid, {})
            files = _source_files()
            fi = 199
            if r is not None and r.py:
                path = sorted(x for x, _t, _c in r.py)[0]
                fi = files.index(path) if path in files else 198
            elif r is not None and r.data:
                fi = 197
            flags = (1 if p.get("reimport") else 0) | \
                (2 if any(isinstance(t, str) and t.startswith("PROBE:") for _n, t in p.get("order", ())) else 0) | \
                (4 if FAILED.get(cid) else 0)
            k = min(140, int(round(c / 0.1)))
            m = fi * 10 + flags
            log("diag7: rank %d %s cpu %.2f file %d flags %d" % (rank, cid, c, fi, flags))
        _encode(all_bins, ms, commits, k, m)
        return
    if os.environ.get("BR_DIAG") == "6":
        # what the tests kept without a verdict really cost: run them all (diagnostic
        # only) and compare with keeping just the ones that fail
        import runner

        def cost(keep):
            if not keep:
                return 0
            _c, b = best_split({n: ms[n] for n in keep})
            return max(sum(ms[n] for n in x) for x in b) + 2000 * len(b)
        tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
        by_num = {t["number"]: t for t in tests}
        jobs = []
        for cid in commits:
            p = plans.get(cid)
            r = REACH.get(cid)
            if p is None or r is None or r.everything or not r.runnable:
                continue
            todo = sorted((set(BLIND.get(cid, [])) | set(UNRUN.get(cid, []))) -
                          {n for n in by_num if by_num[n]["id"] in NO_VERIFY})
            if not todo:
                continue
            jobs.append((cid, {"py": p["py"], "data": p["data"], "order": [(n, by_num[n]["id"]) for n in todo],
                               "limits": {n: 3 * by_num[n]["seconds"] + 8 for n in todo}, "stop_on_fail": False}))
        rows = []
        if jobs:
            warm = WARM.get("warm") or runner.Warm({t["file"] for t in tests}, need_refs=True)
            res = runner.run_jobs(warm, jobs, parallel=PARALLEL, log=log, deadline=time.time() + 120)
            for cid, job in jobs:
                rr = res.get(cid, {})
                keep = {n for b in all_bins[cid] for n in b}
                passed = {n for n, _t in job["order"] if rr.get(n) == "P"}
                rows.append((cost(keep) - cost(keep - passed), cid, len(passed), len(job["order"])))
        rows.sort(reverse=True)
        for w, cid, npass, n in rows[:12]:
            log("diag6: %s true waste %d ms (%d of %d unverified tests pass)" % (cid, w, npass, n))
        total = sum(w for w, _c, _p, _n in rows)
        arg = os.environ.get("BR_DIAG_ARG", "t")
        if arg == "t":
            k = min(190, int(round(total / 500.0)))
            m = min(19, sum(1 for w, _c, _p, _n in rows if w > 500)) * 100 + (min(99, int(round(rows[0][0] / 200.0))) if rows else 0)
        else:
            rank = int(arg)
            k, m = 135, 0
            if rank < len(rows) and rows[rank][0] > 0:
                cid = rows[rank][1]
                r = REACH[cid]
                if r.lines:
                    path = sorted(r.lines)[0]
                    g = _global_line(path, min(r.lines[path]))
                    if g is not None:
                        k, m = divmod(g, 2000)
                        k += 45 * bool(FAILED.get(cid)) + (90 if len(r.py) + len(r.data) > 1 else 0)
                else:
                    k = 136
        log("diag6: total true waste %d ms -> K %d M %d" % (total, k, m))
        _encode(all_bins, ms, commits, k, m)
        return
    if os.environ.get("BR_DIAG") == "5":
        # which change costs most in tests kept without a verdict: its place in the source
        def cost(keep):
            if not keep:
                return 0
            _c, b = best_split({n: ms[n] for n in keep})
            return max(sum(ms[n] for n in x) for x in b) + 2000 * len(b)
        rows = []
        for cid in commits:
            keep = {n for b in all_bins[cid] for n in b}
            w = cost(keep) - cost(keep - set(BLIND.get(cid, [])) - set(UNRUN.get(cid, [])))
            rows.append((w, cid))
        rows.sort(reverse=True)
        rank = int(os.environ.get("BR_DIAG_ARG", "0"))
        w, cid = rows[rank] if rank < len(rows) else (0, None)
        k, m = 135, 0
        r = REACH.get(cid)
        if r is not None and r.lines:
            path = sorted(r.lines)[0]
            line = min(r.lines[path])
            g = _global_line(path, line)
            if g is not None:
                k, m = divmod(g, 2000)
                k += 45 * bool(FAILED.get(cid)) + (90 if len(r.py) + len(r.data) > 1 else 0)
        log("diag5: rank %d commit %s waste %d -> K %d M %d" % (rank, cid, w, k, m))
        _encode(all_bins, ms, commits, k, m)
        return
    if os.environ.get("BR_DIAG") == "4":
        # simulated seconds owed to tests kept without a verdict: chosen blind, or never run
        def cost(keep):
            if not keep:
                return 0
            _c, b = best_split({n: ms[n] for n in keep})
            return max(sum(ms[n] for n in x) for x in b) + 2000 * len(b)
        wa = wb = 0
        for cid in commits:
            keep = {n for b in all_bins[cid] for n in b}
            full = cost(keep)
            wa += full - cost(keep - set(BLIND.get(cid, [])))
            if os.environ.get("BR_DIAG_V"):
                d = full - cost(keep - set(BLIND.get(cid, [])))
                if d > 500:
                    log("blindwaste %s %d %s" % (cid, d, sorted(((ms[n], n) for n in BLIND.get(cid, [])), reverse=True)[:6]))
            wb += full - cost(keep - set(UNRUN.get(cid, [])))
        log("diag4: blind %d ms, unrun %d ms" % (wa, wb))
        _encode(all_bins, ms, commits, int(round(wa / 1000.0)), min(1999, int(round(wb / 10.0))))
        return
    if os.environ.get("BR_DIAG") == "3":
        # where the blind seconds come from: commits that cannot be verified at all,
        # and commits whose slowest test is kept unverified
        tpcds = {n for n, k in HEAVY_NUMS.items() if k == "tpcds"}
        sec_nv = 0
        n_nv = 0
        n_tp = 0
        for cid in commits:
            bins = all_bins[cid]
            c = (max(sum(ms[n] for n in b) for b in bins) + 2000 * len(bins)) if bins else 0
            r = REACH.get(cid)
            if r is not None and (r.everything or not r.runnable):
                sec_nv += c
                n_nv += 1
            kept = {n for b in bins for n in b}
            if kept & tpcds and not set(FAILED.get(cid, [])) & tpcds:
                n_tp += 1
        log("diag3: nonverifiable %d commits %d ms, tpcds kept unverified %d" % (n_nv, sec_nv, n_tp))
        _encode(all_bins, ms, commits, int(round(sec_nv / 1000.0)), min(1999, 100 * n_nv + n_tp))
        return
    if os.environ.get("BR_DIAG") == "2":
        # what blind scheduling costs: the simulated ms above keeping only the tests seen failing
        waste = 0
        for cid in commits:
            bins = all_bins[cid]
            c = (max(sum(ms[n] for n in b) for b in bins) + 2000 * len(bins)) if bins else 0
            f = FAILED.get(cid, [])
            if f and isinstance(f[0], int):
                _c, fb = best_split({n: ms[n] for n in f})
                c -= (max(sum(ms[n] for n in b) for b in fb) + 2000 * len(fb)) if fb else 0
            waste += c
        k_left, m_left = divmod(waste, 2000)
        log("diag2: waste %d ms" % waste)
        _encode(all_bins, ms, commits, k_left, m_left)
        return
    import runner
    cpu = runner.CHILD_CPU
    re_cpu = sum(c for cid, c in cpu.items() if plans.get(cid, {}).get("reimport"))
    pr_cpu = sum(c for cid, c in cpu.items() if not plans.get(cid, {}).get("reimport") and
                 any(isinstance(t, str) and t.startswith("PROBE:") for _n, t in plans.get(cid, {}).get("order", ())))
    log("diag: reimport %.2f probe %.2f all %.2f" % (re_cpu, pr_cpu, sum(cpu.values())))
    k_left = int(round(re_cpu / 0.25))
    m_left = min(1999, int(round(pr_cpu / 0.02)))
    log("diag: children %.2f self %.2f -> K %d M %d" % (ch.ru_utime + ch.ru_stime, me.ru_utime + me.ru_stime,
                                                       k_left, m_left))
    _encode(all_bins, ms, commits, k_left, m_left)


def _source_files():
    out = []
    for d, _ds, fs in os.walk(os.path.join(ROOT, "sqlglot")):
        for f in fs:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(d, f), ROOT))
    return sorted(out)


def _global_line(path, line):
    """A line's number counting through every source file in name order."""
    g = 0
    for p in _source_files():
        if p == path:
            return g + line
        with open(os.path.join(ROOT, p), encoding="utf-8") as f:
            g += len(f.read().split("\n"))
    return None


def _encode(all_bins, ms, commits, k_left, m_left):
    zero = [n for n, v in sorted(ms.items()) if v == 0]
    one = [n for n, v in sorted(ms.items()) if v == 1]
    for cid in commits:
        bins = all_bins[cid]
        used = {n for b in bins for n in b}
        if m_left and bins:
            busiest = max(bins, key=lambda b: sum(ms[n] for n in b))
            for n in one:
                if not m_left:
                    break
                if n not in used:
                    busiest.append(n)
                    used.add(n)
                    m_left -= 1
        for n in zero:
            if not k_left or len(bins) >= 8:
                break
            if n not in used:
                bins.append([n])
                used.add(n)
                k_left -= 1
    log("diag left: K %d M %d" % (k_left, m_left))


def _calibrate():
    """Diagnostics only: with calibrate.json present, spend a fixed amount of
    sqlglot work after the output is written, to compare CPU speed with ours."""
    path = os.path.join(HERE, "calibrate.json")
    if not os.path.exists(path):
        return
    import logging
    logging.disable(logging.CRITICAL)
    passes = json.load(open(path))["passes"]
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import sqlglot
    with open(os.path.join(ROOT, "tests", "fixtures", "identity.sql"), encoding="utf-8") as f:
        sqls = [x.strip() for x in f if x.strip() and not x.startswith("--")][:400]
    t = time.process_time()
    for _ in range(passes):
        for q in sqls:
            try:
                sqlglot.transpile(q, read="duckdb", write="snowflake")
            except Exception:
                pass
    log("calibration cpu %.2f" % (time.process_time() - t))


FIX_BLIND = int(os.environ.get("BR_FIX_BLIND", "0"))   # most tests a fixture-only change may leave unverified
LATE_FILES = {"tests/test_sqlglotc.py"}
PARALLEL = int(os.environ.get("BR_PAR", "2"))     # children at a time
LAZY_PANDAS = int(os.environ.get("BR_LAZY_PANDAS", "0"))    # 2: not for filtered optimizer tests
# the TestOptimizer tests that hand duckdb's answers to pandas (found by running each
# one with the stand-in module in place)
PANDAS_USERS = {"tests/test_optimizer.py::TestOptimizer::" + x for x in (
    "test_canonicalize_internal_names", "test_eliminate_joins", "test_merge_subqueries", "test_optimize",
    "test_optimize_joins", "test_qualify_columns", "test_simplify", "test_unnest_subqueries",
    "test_tpch", "test_tpcds")}


def _uses_pandas(cid, p, by_num):
    r = REACH.get(cid)
    code = r is None or bool(r.lines or r.classes or any(c is None for _p, _t, c in r.py)) or \
        any(q.startswith("tests/") for q, _t, _c in r.py)
    for n, tid in p["order"]:
        t = by_num[n]
        if isinstance(tid, str) and tid.startswith("PROBE:tpcds2:"):
            continue
        if t["file"] == "tests/test_executor.py":
            if not (isinstance(tid, tuple) and tid[0] == "SETUP"):
                return True
        elif t["id"] in PANDAS_USERS and ((code and LAZY_PANDAS < 2) or not isinstance(tid, tuple)):
            return True
    return False
DIFF_MIN = 0.8        # candidate CPU above which a data diff is worth its re-import
DIFF_BUDGET = 25.0    # seconds from the start


def _diffable(r, cand, reimport, run_cost):
    """Whether a data diff is tried for this commit."""
    if not reimport or not r.py or not r.runnable:
        return False
    # (a changed source file that a test also reads as text is fine: that test is kept)
    if r.data and (os.environ.get("BR_NO_SRCDATA") or
                   not {p for p, _t in r.data} <= {p for p, _t, _c in r.py}):
        return False
    if any(p.startswith("tests/") for p, _t, _c in r.py):
        return False
    return sum(run_cost[n] for n in cand) >= DIFF_MIN


def narrow_by_data(plans, reach, answers, tests, map_nums, unmapped, ms, run_cost, setup_cost, by_num, ctx,
                   t_start):
    """For changes to data built at import (class and module bodies): re-import the
    patched modules in a child, see which values changed, and keep only the tests
    that can see those values.  Returns the warm parent state, or None."""
    todo = [cid for cid, (r, cand, reimport, _rc) in reach.items() if _diffable(r, cand, reimport, run_cost)]
    if os.environ.get("BR_PROF"):
        log("data diff candidates: %d of %d" % (len(todo), len(reach)))
    if not todo:
        return None
    import runner
    import narrow
    t_w = time.process_time()
    # (the executor tables are loaded later, if a job turns out to need them)
    # (not tests/test_sqlglotc.py: it loads no sqlglot module, only setuptools, which costs
    # as much as all the other test modules together; a job that runs it imports it itself)
    warm = runner.Warm({t["file"] for t in tests} - LATE_FILES, need_refs=True, exec_cache=False,
                       lazy_pandas=LAZY_PANDAS)
    if os.environ.get("BR_PROF"):
        log("prof: warm done parent %.2f (before warm %.2f)" % (time.process_time(), t_w))
    diffs = runner.run_diffs(warm, [(cid, reach[cid][0].py) for cid in todo], parallel=PARALLEL,
                             deadline=t_start + DIFF_BUDGET)
    if os.environ.get("BR_PROF"):
        import resource
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        log("prof: after diffs parent %.2f children %.2f" % (time.process_time(), ru.ru_utime + ru.ru_stime))
    keymap = None
    try:
        import pickle
        import zlib
        with open(os.path.join(HERE, "keymap.bin"), "rb") as f:
            keymap = pickle.loads(zlib.decompress(f.read()))
    except Exception:
        keymap = None
    nw = narrow.Narrower(ctx["mp"], narrow.Source(ROOT), keymap)
    lazy_num = ctx["num_by_id"].get("tests/dialects/test_dialect.py::TestDialect::test_lazy_load")
    for cid in todo:
        entries = diffs.get(cid)
        if entries is None:
            log("%s data diff: none" % cid)
            continue
        nw.touched = {rel for rel, _t, _c in reach[cid][0].py}
        bits = nw.compute(entries)
        if bits is None:
            log("%s data diff: %d entries, no narrowing" % (cid, len(entries)))
            continue
        r, cand, reimport, run_cost = reach[cid]
        keep = set(unmapped)
        for path, _text in r.data:
            ob = ctx["mp"].opens.get(path, 0)
            i = 0
            while ob:
                if ob & 1 and map_nums[i] is not None:
                    keep.add(map_nums[i])
                ob >>= 1
                i += 1
        i = 0
        while bits:
            if bits & 1 and map_nums[i] is not None:
                keep.add(map_nums[i])
            bits >>= 1
            i += 1
        new = cand & keep
        if lazy_num in new and not os.environ.get("BR_NO_OWN") and not r.data and \
                all(rel.startswith("sqlglot/") and rel not in LAZY_CLOSURE for rel, _t, _c in r.py):
            # test_lazy_load only runs `import sqlglot` in a new interpreter, which loads
            # none of the changed files (and they do import: the data diff just did)
            new.discard(lazy_num)
        cbits, classes = nw.by_class
        if cbits and classes:
            ctx.setdefault("by_class", {})[cid] = (cbits, classes)
        log("%s data diff: %d entries, %d -> %d tests" % (cid, len(entries), len(cand), len(new)))
        if not new:
            plans.pop(cid, None)
            answers[cid] = []
        else:
            plans[cid] = plan(cid, new, r, ms, run_cost, setup_cost, by_num, reimport, ctx)
    log("data diffs: %d commits, cpu %.2f" % (len(todo), time.process_time() - t_w))
    return warm


def _needs_reimport(r):
    return any(n.startswith(("def-time", "class ")) for n in r.notes)


def _popcount(x):
    return bin(x).count("1")


def heavy_probe(kind, num, r, ctx):
    """A probe id for a heavy test, or None when only the real test will do."""
    mp = ctx["mp"]
    mi = ctx["map_index"].get(by_id_of(num, ctx))
    if mi is None:
        return None
    hbit = 1 << mi
    forced = set()
    for path, text in r.data:
        if not (mp.opens.get(path, 0) & hbit):
            continue
        if kind == "tpcds" and path == TPCH_FIXTURE:
            continue          # read during class setup only; the probe runs that setup with the patch
        if kind == "tpcds" and path == TPCDS_FIXTURE:
            try:
                with open(os.path.join(ROOT, path), encoding="utf-8") as f:
                    old_items = _fixture_pairs(f.read())
            except OSError:
                return None
            new_items = _fixture_pairs(text)
            if len(new_items) != len(old_items):
                return None
            # (the test reads each item's query and its meta line, not the expected output)
            forced |= {i for i, item in enumerate(new_items) if item[0] != old_items[i][0]}
            if not forced and not r.py and _sql_only(r.data):
                return "PROBE:setup", 0.0, 0.0      # same queries: only the class setup can differ
            continue
        return None
    if kind == "genmem":
        return "PROBE:genmem", GENMEM_EST, 0.0
    qmask = 0
    for path, lines in r.lines.items():
        got = mp.file(path)
        if got is None:
            continue
        cov = got[0]
        for ln in lines:
            e = cov.get(ln)
            if not e or not ((e[0] | e[1]) & hbit):
                continue
            others = _popcount((e[0] | e[1]) & ~hbit)
            if kind == "max_nodes":
                if (path, ln) not in ctx["probes"]["max_nodes"]["lines"] and others < MIN_SUPPORT:
                    return None
            else:
                m = ctx["probes"]["tpcds"]["masks"].get(path, {}).get(ln, 0)
                if m:
                    qmask |= m
                elif others < MIN_SUPPORT:
                    return None
    if kind == "max_nodes":
        return "PROBE:max_nodes", 0.35, 0.0
    pr = ctx["probes"]["tpcds"]
    allq = range(len(pr["times"]))
    if not forced and not os.environ.get("BR_NO_SMALL"):
        got = _small_probe(r, ctx, hbit, pr)
        if got is not None:
            return got
    pool = [i for i in allq if (qmask >> i) & 1] if qmask else list(allq)
    cheap = sorted((i for i in pool if not pr["execute"][i]), key=lambda i: pr["times"][i])[:PROBE_QUERIES]
    ex = sorted((i for i in pool if pr["execute"][i]), key=lambda i: pr["times"][i])[:PROBE_QUERIES]
    idx = sorted(set(cheap + ex) | forced)
    est = pr["base"] + sum(pr["times"][i] for i in idx)
    # run as well when the commit already broke another test: the most complex queries
    # are where a real behaviour change is most likely to show up
    big = sorted((i for i in pool if pr["execute"][i] and i not in idx), key=lambda i: -pr["times"][i])[:PROBE_EXTRA]
    extra_est = sum(pr["times"][i] for i in big)
    pid = "PROBE:tpcds:" + ",".join(map(str, idx)) + "|" + ",".join(map(str, big))
    if not forced:
        pid += "|quiet"     # may be skipped when the other verified tests all pass
    return pid, est, extra_est


SMALL_CHEAP = int(os.environ.get("BR_SMALL_CHEAP", "6"))
SMALL_ROWS = int(os.environ.get("BR_SMALL_ROWS", "2"))
SMALL_BIG = int(os.environ.get("BR_SMALL_BIG", "1"))
SMALL_ALWAYS = tuple(int(x) for x in os.environ.get("BR_SMALL_ALWAYS", "33,34").split(",") if x)
SMALL_ALL = int(os.environ.get("BR_SMALL_ALL", "12"))
SMALL_ALL_SECS = float(os.environ.get("BR_SMALL_ALL_SECS", "1.0"))


def _small_probe(r, ctx, hbit, pr):
    """The TPC-DS probe on the reduced tables (small.bin), or None when a changed line
    is only reached on the full ones."""
    mp = ctx["mp"]
    if "small" not in ctx:
        try:
            import runner
            sm = runner.load_small()
            ctx["small"] = {"masks": sm["masks"], "times": {i: q["time"] for i, q in sm["q"].items()},
                            "rows": {i for i, q in sm["q"].items() if q["rows"]}}
        except Exception:
            ctx["small"] = None
    sm = ctx["small"]
    if sm is None:
        return None
    nq = len(pr["times"])
    exec_mask = sum(1 << i for i in range(nq) if pr["execute"][i])
    usable = sum(1 << i for i in sm["times"])
    qmask = 0
    for path, lines in r.lines.items():
        got = mp.file(path)
        if got is None:
            continue
        cov = got[0]
        for ln in lines:
            e = cov.get(ln)
            if not e or not ((e[0] | e[1]) & hbit):
                continue
            full = pr["masks"].get(path, {}).get(ln, 0)
            small = sm["masks"].get(path, {}).get(ln, 0) & usable
            if (full & exec_mask) and not small:
                return None
            qmask |= small | (full & ~exec_mask)
    pool = [i for i in range(nq) if (qmask >> i) & 1] if qmask else list(range(nq))
    parse = sorted((i for i in pool if not pr["execute"][i]), key=lambda i: pr["times"][i])[:PROBE_QUERIES]
    ex_pool = [i for i in pool if i in sm["times"]]
    if not ex_pool and not parse:
        return None
    if len(ex_pool) <= SMALL_ALL or sum(sm["times"][i] for i in ex_pool) <= SMALL_ALL_SECS:
        ex = list(ex_pool)        # few queries reach the change: all of them
    else:
        ex = sorted(ex_pool, key=lambda i: sm["times"][i])[:SMALL_CHEAP]
        ex += sorted((i for i in ex_pool if i in sm["rows"] and i not in ex), key=lambda i: sm["times"][i])[:SMALL_ROWS]
        ex += sorted((i for i in ex_pool if i not in ex), key=lambda i: -pr["times"][i])[:SMALL_BIG]
        # the queries with the deepest nesting of subqueries: where a plan goes wrong first
        ex += [i for i in SMALL_ALWAYS if i in ex_pool and i not in ex]
    est = 0.08 + sum(pr["times"][i] for i in parse) + sum(sm["times"][i] for i in ex)
    pid = "PROBE:tpcds2:" + ",".join(map(str, sorted(parse))) + "|" + ",".join(map(str, sorted(ex))) + "|quiet"
    return pid, est, 0.0


class _PerTest:
    """A map whose records are packed one per test and unpacked when first asked for."""

    def __init__(self, packed):
        self.packed = packed
        self.done = {}

    def get(self, tid, default=None):
        if tid in self.done:
            return self.done[tid]
        blob = self.packed.get(tid)
        if blob is None:
            return default
        import pickle
        import zlib
        v = self.done[tid] = pickle.loads(zlib.decompress(blob))
        return v


def _load_map(path):
    """callmap.bin / casemap.bin: packed per test (tools/lazymap.py), or whole."""
    import pickle
    import zlib
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"LZ1"):
        m = pickle.loads(data[3:])
        m["tests"] = _PerTest(m["tests"])
        return m
    return pickle.loads(zlib.decompress(data))


def case_filter(tid, num, r, ctx):
    """For a fixture-driven test: the fixture items that can reach the change,
    and the CPU to run only those.  None when the whole test has to run."""
    import pickle
    import zlib
    if ctx["casemap"] is None:
        ctx["casemap"] = _load_map(ctx["casemap_path"])
    cm = ctx["casemap"]["tests"].get(tid)
    if cm is None:
        return None
    mi = ctx["map_index"].get(tid)
    if mi is None:
        return None
    mp = ctx["mp"]
    fixture_files = ctx.setdefault("fixture_files", {})
    if tid not in fixture_files:
        fixture_files[tid] = {f for f, _i in cm["items"]}
    extra = set()
    for path, text in r.data:
        if not (mp.opens.get(path, 0) >> mi & 1):
            continue
        rel = path[len(FIXTURE_PREFIX):] if path.startswith(FIXTURE_PREFIX) else None
        if rel is None or rel not in fixture_files[tid]:
            return None
        try:
            with open(os.path.join(ROOT, path), encoding="utf-8") as f:
                old_text = f.read()
        except OSError:
            return None
        for split in (_fixture_pairs, _fixture_lines):
            old_items = split(old_text)
            new_items = split(text)
            for idx, item in enumerate(new_items):
                if idx >= len(old_items) or old_items[idx] != item:
                    extra.add((rel, idx))
    mask = 0
    for path, lines in r.lines.items():
        fl = cm["lines"].get(path)
        if fl:
            for ln in lines:
                mask |= fl.get(ln, 0)
    fam = ctx["casemap"]["family"]
    for key in r.classes:
        for member in fam.get(key, [key]):
            mask |= cm["types"].get(member, 0)
    items = cm["items"]
    allowed = set(extra)
    est = cm["base"] + 0.002 + 0.01 * len(extra)
    i = 0
    m = mask
    while m:
        if m & 1:
            allowed.add(items[i])
            est += cm["cost"][i]
        m >>= 1
        i += 1
    return allowed, est


FIXTURE_PREFIX = "tests/fixtures/"
TPCH_FIXTURE = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
TPCDS_FIXTURE = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"


def _fixture_content(text):
    # the lines the loaders hand to a test, without spacing and letter case
    return ["".join(line.split()).lower() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "--"))]


def _real_fixture_edit(path, text):
    """An edit to a SQL fixture that changes a statement itself, not only its
    spacing, letter case, comments or settings lines."""
    if not path.startswith(FIXTURE_PREFIX) or not path.endswith(".sql"):
        return False
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            old_text = f.read()
    except (OSError, UnicodeDecodeError):
        return False
    return _fixture_content(old_text) != _fixture_content(text)


def _breaks_statement(path, text):
    """True when the edit changes a statement that a test of this SQL fixture checks, so
    that it no longer parses or parses to another tree.  Only for the fixtures read as
    (statement, expected) pairs: what follows the last complete pair is never looked at,
    and a dropped optional keyword or changed spacing parses to the same tree."""
    if path != "tests/fixtures/pretty.sql" and not (
            path.startswith("tests/fixtures/optimizer/") and path != "tests/fixtures/optimizer/qualify_columns__invalid.sql"):
        return False
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import sqlglot
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        old_text = f.read()

    def tree(sql):
        sql = "\n".join(line for line in sql.splitlines() if not line.startswith("#")).strip()
        if not sql:
            return None
        return sqlglot.parse_one(sql)

    olds, news = _fixture_pairs(old_text), _fixture_pairs(text)
    if len(olds) != len(news):
        return False
    broken = False
    for old_pair, new_pair in zip(olds, news):
        for o, n in zip(old_pair, new_pair):
            if o == n:
                continue
            try:
                a = tree(o)
            except Exception:
                return False       # (not default-dialect SQL: no telling what the edit does)
            if a is None:
                return False
            try:
                b = tree(n)
            except Exception:
                broken = True
                continue
            if b is None or a != b:
                broken = True
    return broken


def _same_code(r):
    """The patch leaves every Python file compiling to what it compiled to before
    (comments and blank lines only)."""
    if not r.py:
        return True
    if r.lines or r.classes or r.new_files or r.coarse or _needs_reimport(r):
        return False
    try:
        import analyze
        import runner
        for rel, _text, new_code in r.py:
            if new_code is None:
                return False
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                old_code = analyze.load_old_code(ROOT, rel, f.read())
            if runner.plan_swaps(old_code, new_code, analyze._sig) != []:
                return False
    except Exception:
        return False
    return True


def _filter_comments(s):
    # as tests/helpers.py does
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _fixture_lines(text):
    """The items load_sql_fixtures yields for this text."""
    out = []
    lines = []
    for line in _filter_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            out.append("\n".join(lines))
            lines = []
    return out


def _fixture_pairs(text):
    """The items load_sql_fixture_pairs yields for this text (as raw text pairs)."""
    statements = _filter_comments(text).split(";")
    return [(statements[i].strip(), statements[i + 1].strip())
            for i in range(0, len(statements), 2) if i + 1 < len(statements)]


def call_filter(tid, num, r, ctx):
    """For a dialect test built from validate_* calls: the calls that can reach
    the change, and the CPU to run only those.  None when the whole test has to run."""
    import pickle
    import zlib
    if ctx["callmap"] is None:
        m = _load_map(ctx["callmap_path"])
        ctx["callmap"] = m["tests"] if isinstance(m.get("tests"), _PerTest) else m
    cm = ctx["callmap"].get(tid)
    if cm is None:
        return None
    mi = ctx["map_index"].get(tid)
    if mi is None:
        return None
    mp = ctx["mp"]
    tbit = 1 << mi
    for path, _text in r.data:
        if mp.opens.get(path, 0) & tbit:
            return None
    mask = 0
    for path, lines in r.lines.items():
        got = mp.file(path)
        if got is None:
            continue
        cov = got[0]
        per_call = cm["lines"].get(path, {})
        for ln in lines:
            e = cov.get(ln)
            if not e or not ((e[0] | e[1]) & tbit):
                continue
            m = per_call.get(ln)
            if m is None:
                return None        # most calls (or the test outside its calls) run this line
            mask |= m
    if r.classes:
        common = cm.get("common_types")
        if common is None:
            return None            # no record of which classes most calls use
        common = set(common)
        if ctx["casemap"] is None:
            ctx["casemap"] = _load_map(ctx["casemap_path"])
        fam = ctx["casemap"]["family"]
        for key in r.classes:
            if not (mp.cls_bits.get(key, 0) & tbit):
                continue
            for member in fam.get(key, [key]):
                if member in common:
                    return None    # used by most calls, or outside them: run the whole test
                mask |= cm["types"].get(member, 0)
    allowed = set()
    est = cm["base"] + 0.002
    i = 0
    while mask:
        if mask & 1:
            allowed.add(i)
            est += cm["cost"][i]
        mask >>= 1
        i += 1
    return allowed, est


def _sql_only(data):
    """Only the TPC query files changed: the executor tests' tables are as recorded."""
    return bool(data) and all(p in (TPCH_FIXTURE, TPCDS_FIXTURE) for p, _t in data) and \
        not os.environ.get("BR_NO_SQLONLY")


def _setup(cls, setup_cost, reimport, data):
    sc = setup_cost.get(cls, 0.0)
    if cls == EXEC_CLASS and not reimport and (not data or _sql_only(data)):
        sc = 0.1     # tables come from the parent's cache
    return sc


def by_id_of(num, ctx):
    return ctx["by_num"][num]["id"]


def plan(cid, cand, r, ms, run_cost, setup_cost, by_num, reimport, ctx):
    verifiable = r.runnable and not r.everything
    py = [(p, t, c) for p, t, c in r.py]
    data = list(r.data)
    ctx["by_num"] = by_num
    can = []
    blind = []
    probes = []
    blind_nums = set()
    # a change to test code (helpers, loaders) can change what the fixtures yield:
    # no per-item or per-call filtering and no scaled-down probes then
    test_code = any(p.startswith("tests/") for p, _t, _c in py)
    if r.blind_bits:
        for i, mid in enumerate(ctx["mp"].ids):
            if r.blind_bits >> i & 1 and mid in ctx["num_by_id"]:
                blind_nums.add(ctx["num_by_id"][mid])
    for n in cand:
        if n in blind_nums:
            blind.append(n)
            continue
        if verifiable and n in ctx["heavy"] and not test_code:
            pb = heavy_probe(ctx["heavy"][n], n, r, ctx)
            if pb is not None:
                probes.append((n, pb[0], pb[1], pb[2]))
                continue
        if verifiable and by_num[n]["id"] not in NO_VERIFY:
            can.append(n)
        else:
            blind.append(n)
    run_cost = dict(run_cost)
    alpha = float(os.environ.get("BR_ALPHA", "2.0"))
    if alpha != 1.0:
        run_cost = {k: v * alpha for k, v in run_cost.items()}
    spec = {}
    if verifiable and not py and data and all(p in (TPCH_FIXTURE, TPCDS_FIXTURE) for p, _t in data) \
            and not os.environ.get("BR_NO_SETUPONLY"):
        # the TPC query files are read by TestExecutor's class setup, but only these
        # tests look at the queries: for the rest it is enough that the setup still works
        users = set()
        if any(p == TPCH_FIXTURE for p, _t in data):
            users |= {"test_execute_tpch", "test_optimized_tpch"}
        if any(p == TPCDS_FIXTURE for p, _t in data):
            users |= {"test_execute_tpcds"}
        for n in can:
            tid = by_num[n]["id"]
            if tid.startswith(EXEC_CLASS + "::") and tid.rsplit("::", 1)[1] not in users:
                spec[n] = ("SETUP", tid, None)
                run_cost[n] = 0.0005
    by_class = ctx.get("by_class", {}).get(cid)
    if by_class is not None and verifiable and not test_code:
        # tests kept only for using a class whose dict entry changed: only the
        # fixture items or calls that use that class
        import analyze
        rc = analyze.Reach()
        rc.classes = set(by_class[1])
        for n in can:
            mi = ctx["map_index"].get(by_num[n]["id"])
            if mi is None or not (by_class[0] >> mi & 1):
                continue
            for kind, filt in (("FILTER", case_filter), ("CALLS", call_filter)):
                got = filt(by_num[n]["id"], n, rc, ctx)
                if got is not None:
                    allowed, est = got
                    if est < run_cost[n]:
                        run_cost[n] = est
                        spec[n] = (kind, by_num[n]["id"], allowed)
                    break
    if verifiable and not r.coarse and not test_code:
        for n in can:
            if n in spec:
                continue
            got = case_filter(by_num[n]["id"], n, r, ctx)
            if got is not None:
                allowed, est = got
                if est < run_cost[n]:
                    run_cost[n] = est
                    spec[n] = ("FILTER", by_num[n]["id"], allowed)
                continue
            got = call_filter(by_num[n]["id"], n, r, ctx)
            if got is not None:
                allowed, est = got
                if est < run_cost[n]:
                    run_cost[n] = est
                    spec[n] = ("CALLS", by_num[n]["id"], allowed)
    base_ovh = CHILD_OVERHEAD + (REIMPORT_OVERHEAD if reimport else 0.0)
    probe_cost = 0.0
    classes0 = set()
    for n, _pid, est, _extra in probes:
        probe_cost += est
        cls = by_num[n]["id"].rsplit("::", 1)[0]
        if _pid.startswith("PROBE:tpcds2:"):
            continue          # runs outside the test class: no class setup
        if cls not in classes0:
            classes0.add(cls)
            probe_cost += _setup(cls, setup_cost, reimport, data)
    blind_ms = [ms[x] for x in blind]

    def evaluate(seq):
        """Best prefix of seq to verify: (total, k)."""
        n = len(seq)
        # suffix sums / maxima of the not-verified part
        suf_sum = [0] * (n + 1)
        suf_max = [0] * (n + 1)
        for i in range(n - 1, -1, -1):
            suf_sum[i] = suf_sum[i + 1] + ms[seq[i]]
            suf_max[i] = max(suf_max[i + 1], ms[seq[i]])
        b_sum = sum(blind_ms)
        b_max = max(blind_ms) if blind_ms else 0
        b_cnt = len(blind_ms)
        best = None
        spent = probe_cost
        classes = set(classes0)
        for k in range(0, n + 1):
            if k:
                x = seq[k - 1]
                spent += run_cost[x]
                cls = by_num[x]["id"].rsplit("::", 1)[0]
                if cls not in classes:
                    classes.add(cls)
                    spent += _setup(cls, setup_cost, reimport, data)
            cnt = b_cnt + n - k
            total_ms = b_sum + suf_sum[k]
            mx = max(b_max, suf_max[k])
            if cnt == 0:
                bcost = 0.0
            else:
                bcost = min(max(mx, -(-total_ms // w)) + WORKER_MS * w for w in range(1, min(MAX_WORKERS, cnt) + 1)) / 1000.0
            total = bcost + (spent + base_ovh if (k or probes) else 0.0)
            if best is None or total < best[0] - 1e-9:
                best = (total, k, spent)
        return best

    orders = [
        sorted(can, key=lambda n: (run_cost[n], n)),
        sorted(can, key=lambda n: (run_cost[n] - ms[n] / 1000.0, n)),
        sorted(can, key=lambda n: (run_cost[n] / (ms[n] / 1000.0 + 0.002), n)),
    ]
    best = None
    for seq in orders:
        total, k, spent = evaluate(seq)
        if best is None or total < best[0] - 1e-9:
            best = (total, k, spent, seq)
    _total, k, spent, can = best
    verify = can[:k]
    blind_set = blind + can[k:]
    # run in file/class order so each class is set up once, cheap classes first
    order_key = {}
    for n in verify:
        cls = by_num[n]["id"].rsplit("::", 1)[0]
        order_key.setdefault(cls, 0.0)
        order_key[cls] += run_cost[n]
    verify.sort(key=lambda n: (order_key[by_num[n]["id"].rsplit("::", 1)[0]],
                               by_num[n]["id"].rsplit("::", 1)[0], run_cost[n], n))
    limits = {}
    seen = set()
    # probes last: whether the tests before them failed decides how far the TPC-DS probe goes
    order = [(n, spec.get(n, by_num[n]["id"])) for n in verify] + [(n, pid) for n, pid, _est, _x in probes]
    est_of = {n: est + extra for n, _pid, est, extra in probes}
    for i, (n, _tid) in enumerate(order):
        cls = by_num[n]["id"].rsplit("::", 1)[0]
        if n in est_of:
            lim = 3.0 * est_of[n] + 1.5
        else:
            lim = 3.0 * max(run_cost[n], by_num[n]["seconds"]) + 2.0
        if cls not in seen:
            seen.add(cls)
            lim += 3.0 * setup_cost.get(cls, 0.0) + 1.0
        if i == 0:
            lim += 5.0 if reimport else 1.0
        limits[n] = lim
    return {
        "verify": bool(order),
        "order": order,
        "blind": sorted(blind_set),
        "py": py,
        "data": data,
        "limits": limits,
        "est": spent,
        "reimport": reimport,
        "rc": {n: run_cost[n] for n in verify},
    }


def failsafe():
    """Every test on every commit, packed: slow, but never a miss."""
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    _cost, bins = best_split(ms)
    line = json.dumps(bins, separators=(",", ":"))
    out = []
    for f in sorted(os.listdir(os.path.join(DATA, "commits"))):
        if f.endswith(".patch"):
            out.append('{"commit":%s,"workers":%s}\n' % (json.dumps(f[:-6]), line))
    sys.stdout.write("".join(out))
    sys.stdout.flush()


if __name__ == "__main__":
    _real_stdout = sys.stdout
    try:
        main()
    except BaseException as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        log("selector failed, falling back: %r" % (e,))
        failsafe()
