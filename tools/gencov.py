"""Per-dialect coverage, class usage and CPU for TestGenerator.test_generator_memory (one dialect per run)."""
import sys, os, json, time, io, pickle, unittest
ROOT = '/home/claude/work/sqlglot'
sys.path.insert(0, ROOT); os.chdir(ROOT); os.environ['SKIP_INTEGRATION'] = '0'
TID = 'tests/test_generator.py::TestGenerator::test_generator_memory'

def run(value, instrument):
    import tests.test_generator as tg
    from sqlglot.dialects import Dialects
    tg.Dialects = [Dialects(value)]
    lines = set(); types_ = set()
    if instrument:
        pre = ROOT + '/'
        mon = sys.monitoring; mon.use_tool_id(5, 'gen')
        gf = sys._getframe
        def on_line(code, line):
            if code.co_filename.startswith(pre): lines.add((code.co_filename[len(pre):], line))
        def on_start(code, off):
            if not code.co_filename.startswith(pre): return mon.DISABLE
            lines.add((code.co_filename[len(pre):], code.co_firstlineno))
            n = code.co_argcount + code.co_kwonlyargcount
            if n:
                loc = gf(1).f_locals
                for v in code.co_varnames[:n]:
                    x = loc.get(v)
                    if x is not None: types_.add(x if isinstance(x, type) else type(x))
        mon.register_callback(5, mon.events.LINE, on_line)
        mon.register_callback(5, mon.events.PY_START, on_start)
        mon.set_events(5, mon.events.LINE | mon.events.PY_START)
    t = time.process_time(); r = unittest.TestResult(); tg.TestGenerator('test_generator_memory').run(r); dt = time.process_time() - t
    if instrument: sys.monitoring.set_events(5, 0)
    def tkey(t):
        m = getattr(t, '__module__', '') or ''
        return '%s:%s' % (m, t.__qualname__) if m.startswith(('sqlglot', 'tests')) else None
    return {'cost': dt, 'lines': lines, 'types': {x for x in map(tkey, types_) if x}, 'ok': True}

def forked(value, instrument):
    rd, wr = os.pipe(); pid = os.fork()
    if pid == 0:
        os.close(rd)
        try: res = run(value, instrument)
        except BaseException:
            import traceback; res = {'error': traceback.format_exc()}
        with os.fdopen(wr, 'wb') as w: w.write(pickle.dumps(res))
        os._exit(0)
    os.close(wr)
    with os.fdopen(rd, 'rb') as r: data = r.read()
    os.waitpid(pid, 0)
    return pickle.loads(data)

sys.path.insert(0, ROOT)
from sqlglot.dialects import Dialects
values = [d.value for d in Dialects]
items = []; cost = []; lm = {}; tm = {}
for i, v in enumerate(values):
    a = forked(v, False); b = forked(v, True)
    if 'error' in a or 'error' in b:
        print('ERR', v, (a.get('error') or b.get('error'))[-300:], file=sys.stderr); sys.exit(1)
    items.append(('__dialects__', v)); cost.append(round(a['cost'], 5))
    for f, l in b['lines']:
        lm.setdefault(f, {}); lm[f][l] = lm[f].get(l, 0) | (1 << i)
    for t in b['types']: tm[t] = tm.get(t, 0) | (1 << i)
print(len(items), 'total %.2f' % sum(cost), file=sys.stderr)
pickle.dump({TID: {'items': items, 'cost': cost, 'base': 0.0, 'lines': lm, 'types': tm}}, open(sys.argv[1], 'wb'))
