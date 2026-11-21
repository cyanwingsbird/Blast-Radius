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
CHILD_OVERHEAD = 0.03   # fork + patch + exit, swap mode
REIMPORT_OVERHEAD = 0.6  # extra when module- or class-level code changed
WARM_COST = 0.7
EXEC_CLASS = "tests/test_executor.py::TestExecutor"
HEAVY = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds",
    "tests/test_parser.py::TestParser::test_max_nodes": "max_nodes",
}
MIN_SUPPORT = 20       # other tests that must also run a changed line for a probe to stand in
PROBE_QUERIES = int(os.environ.get("BR_PROBE_QUERIES", "5"))
PROBE_EXTRA = 2


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


def main():
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
    ctx = {"mp": mp, "probes": probes, "heavy": heavy, "map_index": {i: n for n, i in enumerate(mp.ids)},
           "casemap": None, "casemap_path": os.path.join(HERE, "casemap.bin"),
           "callmap": None, "callmap_path": os.path.join(HERE, "callmap.bin"), "num_by_id": num_by_id}
    map_nums = [num_by_id.get(i) for i in mp.ids]
    unmapped = [t["number"] for t in tests if t["id"] not in set(mp.ids)]
    by_num = {t["number"]: t for t in tests}

    answers = {}
    plans = {}
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
        reimport = any(c is None for _p, _t, c in r.py) or _needs_reimport(r)
        plans[cid] = plan(cid, cand, r, ms, run_cost, setup_cost, by_num, reimport, ctx)

    jobs = []
    for cid, p in plans.items():
        if p["verify"]:
            jobs.append((cid, p))
        else:
            answers[cid] = p["blind"]
    results = {}
    if jobs:
        import runner
        t_w = time.process_time()
        need_refs = any(p.get("reimport") for _c, p in jobs)
        warm = runner.Warm({by_num[n]["file"] for _c, p in jobs for n, _ in p["order"]}, need_refs=need_refs)
        log("warm cpu %.2f, analysis cpu %.2f" % (time.process_time() - t_w, t_w))
        jobs.sort(key=lambda j: -j[1]["est"])
        results = runner.run_jobs(warm, [(cid, {"py": p["py"], "data": p["data"], "order": p["order"],
                                                "limits": p["limits"], "stop_on_fail": True,
                                                "dyn": {"ms": {n: ms[n] for n, _t in p["order"]},
                                                        "rc": {n: p["rc"].get(n, 0.0) for n, _t in p["order"]},
                                                        "kept": [ms[n] for n in p["blind"]]}})
                                         for cid, p in jobs], parallel=2, log=log,
                                  deadline=t_start + TIME_BUDGET)
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
    for cid, p in jobs:
        res = results.get(cid, {})
        keep = set(p["blind"])
        for num, _tid in p["order"]:
            st = res.get(num)
            if st != "P":          # failed, timed out, or never run
                keep.add(num)
        answers[cid] = sorted(keep)
        nfail = sum(1 for v in res.values() if v in ("F", "T"))
        log("%s verified %d/%d fail=%d keep=%d est=%.2f" % (cid, len(res), len(p["order"]), nfail, len(keep), p["est"]))

    out = []
    for cid in commits:
        keep = answers.get(cid, [])
        _cost, bins = best_split({n: ms[n] for n in keep})
        out.append(json.dumps({"commit": cid, "workers": bins}, separators=(",", ":")))
    sys.stdout.write("".join(line + "\n" for line in out))
    sys.stdout.flush()
    log("done in %.2fs" % (time.time() - t_start))


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
            forced |= {i for i, item in enumerate(new_items) if item != old_items[i]}
            continue
        return None
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


def case_filter(tid, num, r, ctx):
    """For a fixture-driven test: the fixture items that can reach the change,
    and the CPU to run only those.  None when the whole test has to run."""
    import pickle
    import zlib
    if ctx["casemap"] is None:
        with open(ctx["casemap_path"], "rb") as f:
            ctx["casemap"] = pickle.loads(zlib.decompress(f.read()))
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
        with open(ctx["callmap_path"], "rb") as f:
            ctx["callmap"] = pickle.loads(zlib.decompress(f.read()))
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
            with open(ctx["casemap_path"], "rb") as f:
                ctx["casemap"] = pickle.loads(zlib.decompress(f.read()))
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


def _setup(cls, setup_cost, reimport, data):
    sc = setup_cost.get(cls, 0.0)
    if cls == EXEC_CLASS and not reimport and not data:
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
    if r.blind_bits:
        for i, mid in enumerate(ctx["mp"].ids):
            if r.blind_bits >> i & 1 and mid in ctx["num_by_id"]:
                blind_nums.add(ctx["num_by_id"][mid])
    for n in cand:
        if n in blind_nums:
            blind.append(n)
            continue
        if verifiable and n in ctx["heavy"]:
            pb = heavy_probe(ctx["heavy"][n], n, r, ctx)
            if pb is not None:
                probes.append((n, pb[0], pb[1], pb[2]))
                continue
        if verifiable and by_num[n]["id"] not in NO_VERIFY:
            can.append(n)
        else:
            blind.append(n)
    run_cost = dict(run_cost)
    alpha = float(os.environ.get("BR_ALPHA", "1.0"))
    if alpha != 1.0:
        run_cost = {k: v * alpha for k, v in run_cost.items()}
    spec = {}
    if verifiable and not r.coarse:
        for n in can:
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
