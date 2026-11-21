"""Per test: sqlglot/tests classes seen as an argument (self/cls/any param) or passed as a class object.
usage: typeuse.py REPO OUTDIR < 'num id' lines"""
import sys, os, json, io, time
REPO = os.path.realpath(sys.argv[1]); OUT = os.path.abspath(sys.argv[2])
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, '/home/claude/work/repo')

def run_one(tid, num):
    sys.path.insert(0, REPO); os.chdir(REPO)
    import runner
    runner.install_sync_pools()
    import unittest
    f, cls, meth = tid.split("::")
    mod = __import__(f[:-3].replace("/", "."), fromlist=["x"])
    runner.install_sync_pools()
    case = getattr(mod, cls)(meth)
    seen = set()
    pre = REPO + os.sep
    mon = sys.monitoring; TOOL = 4
    mon.use_tool_id(TOOL, "types")
    getframe = sys._getframe
    def ok(t):
        m = getattr(t, "__module__", None)
        return isinstance(m, str) and (m.startswith("sqlglot") or m.startswith("tests"))
    def on_start(code, off, depth):
        n = code.co_argcount + code.co_kwonlyargcount
        if not n:
            return
        fr = getframe(depth)
        loc = fr.f_locals
        for v in code.co_varnames[:n]:
            x = loc.get(v)
            if x is None:
                continue
            t = x if isinstance(x, type) else type(x)
            if t not in seen:
                seen.add(t)
            if isinstance(x, (list, tuple)) and x and len(x) < 50:
                for y in x:
                    if isinstance(y, type) and y not in seen:
                        seen.add(y)
    def on_code(code, off):
        if not code.co_filename.startswith(pre) or not (code.co_argcount + code.co_kwonlyargcount):
            return mon.DISABLE
        return on_start(code, off, 2)
    mon.register_callback(TOOL, mon.events.PY_START, on_code)
    mon.set_events(TOOL, mon.events.PY_START)
    so, se = sys.stdout, sys.stderr
    sys.stdout = io.StringIO(); sys.stderr = io.StringIO()
    res = unittest.TestResult()
    t0 = time.time()
    try: unittest.TestSuite([case]).run(res)
    finally:
        sys.stdout, sys.stderr = so, se
        mon.set_events(TOOL, 0)
    keys = sorted({"%s:%s" % (t.__module__, t.__qualname__) for t in seen if ok(t)})
    json.dump({"id": tid, "ok": res.wasSuccessful(), "types": keys, "t": time.time() - t0}, open(os.path.join(OUT, f"{num}.json"), "w"))

for line in sys.stdin:
    num, tid = line.strip().split(" ", 1)
    if os.path.exists(os.path.join(OUT, f"{num}.json")): continue
    pid = os.fork()
    if pid == 0:
        try: run_one(tid, num)
        except BaseException:
            import traceback; traceback.print_exc()
        finally: os._exit(0)
    os.waitpid(pid, 0)
