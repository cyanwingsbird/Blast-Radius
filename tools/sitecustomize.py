import os, sys
_d = os.environ.get("COVCHILD_DIR")
if _d and os.environ.get("COVCHILD_MAIN") != str(os.getpid()):
    def _install():
        import json
        REPO = os.environ["COV_REPO"]; pre = REPO + os.sep
        mon = sys.monitoring; TOOL = 3
        try: mon.use_tool_id(TOOL, "covc")
        except Exception: return
        S = set()
        def on_line(code, line):
            fn = code.co_filename
            if fn.startswith(pre): S.add((fn[len(pre):], line, code.co_flags & 1))
            return mon.DISABLE
        def on_start(code, off):
            fn = code.co_filename
            if fn.startswith(pre): S.add((fn[len(pre):], code.co_firstlineno, code.co_flags & 1))
            return mon.DISABLE
        mon.register_callback(TOOL, mon.events.LINE, on_line)
        mon.register_callback(TOOL, mon.events.PY_START, on_start)
        mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
        done = []
        def dump():
            if done: return
            done.append(1)
            try:
                with open(os.path.join(_d, f"{os.getpid()}.json"), "w") as f: json.dump(sorted(S), f)
            except Exception: pass
        import atexit; atexit.register(dump)
        try:
            from multiprocessing import util
            util.Finalize(None, dump, exitpriority=100)
        except Exception: pass
    _install()
