"""Per-test isolated coverage. usage: covone.py REPO OUTDIR < "num id" lines"""
import sys, os, json, time, io, tempfile, glob

def run_one(tid, num, REPO, OUT):
    sys.path.insert(0, REPO); os.chdir(REPO)
    cdir = tempfile.mkdtemp(prefix="covc")
    os.environ["COVCHILD_DIR"] = cdir; os.environ["COVCHILD_MAIN"] = str(os.getpid()); os.environ["COV_REPO"] = REPO
    mon = sys.monitoring; TOOL = 3
    mon.use_tool_id(TOOL, "cov")
    phase = {"cur": set()}
    pre = REPO + os.sep
    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(pre):
            phase["cur"].add((fn[len(pre):], line, code.co_flags & 1))
        return mon.DISABLE
    def on_start(code, off):
        fn = code.co_filename
        if fn.startswith(pre):
            phase["cur"].add((fn[len(pre):], code.co_firstlineno, code.co_flags & 1))
        return mon.DISABLE
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
    opens = set()
    def hook(ev, args):
        if ev == "open":
            p = args[0]
            if isinstance(p, str):
                ap = os.path.abspath(p)
                if ap.startswith(pre) and "__pycache__" not in ap: opens.add(ap[len(pre):])
    sys.addaudithook(hook)
    import unittest
    f, cls, meth = tid.split("::")
    modname = f[:-3].replace("/", ".")
    imp = phase["cur"]; err = None
    t0 = time.perf_counter()
    try:
        mod = __import__(modname, fromlist=["x"])
        case = getattr(mod, cls)(meth)
    except BaseException as e:
        err = "import:" + repr(e); case = None
    run = set(); phase["cur"] = run; mon.restart_events()
    t1 = time.perf_counter(); ok = False; skipped = False
    if case is not None:
        suite = unittest.TestSuite([case])
        res = unittest.TestResult()
        so, se = sys.stdout, sys.stderr
        sys.stdout = io.StringIO(); sys.stderr = io.StringIO()
        try: suite.run(res)
        finally: sys.stdout, sys.stderr = so, se
        ok = res.wasSuccessful(); skipped = bool(res.skipped)
        if not ok: err = (res.failures + res.errors)[0][1][-2000:]
    t2 = time.perf_counter()
    mon.set_events(TOOL, 0)
    import psutil
    me = psutil.Process()
    tend = time.time() + 5
    while time.time() < tend:
        kids = [c for c in me.children(recursive=True) if 'resource_tracker' not in ' '.join(c.cmdline())]
        if not kids: break
        time.sleep(0.02)
    child = set(); nchild = 0
    for cf in glob.glob(os.path.join(cdir, "*.json")):
        nchild += 1
        for fn, ln, k in json.load(open(cf)): child.add((fn, ln, k))
    def pack(s):
        d = {}
        for fn, ln, k in s: d.setdefault(fn, [[], []])[1 if k else 0].append(ln)
        return {k: [sorted(v[0]), sorted(v[1])] for k, v in d.items()}
    json.dump({"id": tid, "imp": pack(imp), "run": pack(run), "child": pack(child), "nchild": nchild, "opens": sorted(opens), "ok": ok,
               "skipped": skipped, "err": err, "t_imp": t1 - t0, "t_run": t2 - t1}, open(os.path.join(OUT, f"{num}.json"), "w"))

if __name__ == "__main__":
    REPO = os.path.realpath(sys.argv[1]); OUT = os.path.abspath(sys.argv[2])
    os.environ["SKIP_INTEGRATION"] = "0"
    for line in sys.stdin:
        num, tid = line.strip().split(" ", 1)
        if os.path.exists(os.path.join(OUT, f"{num}.json")): continue
        pid = os.fork()
        if pid == 0:
            try: run_one(tid, num, REPO, OUT)
            except BaseException as e:
                import traceback; traceback.print_exc()
            finally:
                sys.stdout.flush(); os._exit(0)
        os.waitpid(pid, 0)
