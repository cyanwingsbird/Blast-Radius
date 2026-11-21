"""Per validate_* call coverage, class usage and CPU for Validator-based dialect tests.
usage: callcov.py OUT.pickle [shard nshards]"""
import sys, os, json, time, io, pickle, unittest
ROOT = '/home/claude/work/sqlglot'
sys.path.insert(0, ROOT); os.chdir(ROOT); os.environ['SKIP_INTEGRATION'] = '0'
sys.path.insert(0, '/home/claude/work/repo')
import runner
OUT = sys.argv[1]
shard, nsh = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (0, 1)
T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
METHODS = ('validate_identity', 'validate_all', 'validate_transpile')
BASE = ('<base>',)

def run(tid, instrument):
    import importlib
    f, cls, meth = tid.split('::')
    mod = importlib.import_module(f[:-3].replace('/', '.'))
    runner.install_sync_pools()
    V = importlib.import_module('tests.dialects.test_dialect').Validator
    klass = getattr(mod, cls)
    if not issubclass(klass, V):
        return None
    state = {'cur': BASE, 'n': 0, 'cost': {}, 't': time.process_time(), 'lines': {}, 'types': {}}
    def switch(key):
        now = time.process_time()
        state['cost'][state['cur']] = state['cost'].get(state['cur'], 0.0) + now - state['t']
        state['t'] = now; state['cur'] = key
    for name in METHODS:
        orig = getattr(V, name)
        def make(orig):
            def wrapper(self, *a, **k):
                if state['cur'] != BASE:          # nested call: part of the outer one
                    return orig(self, *a, **k)
                key = ('call', state['n']); state['n'] += 1
                switch(key)
                try:
                    return orig(self, *a, **k)
                finally:
                    switch(BASE)
            return wrapper
        setattr(V, name, make(orig))
    if instrument:
        pre = ROOT + '/'
        mon = sys.monitoring; mon.use_tool_id(5, 'call')
        gf = sys._getframe
        def on_line(code, line):
            fn = code.co_filename
            if fn.startswith(pre):
                state['lines'].setdefault(state['cur'], set()).add((fn[len(pre):], line))
        def on_start(code, off):
            fn = code.co_filename
            if not fn.startswith(pre):
                return mon.DISABLE
            state['lines'].setdefault(state['cur'], set()).add((fn[len(pre):], code.co_firstlineno))
            n = code.co_argcount + code.co_kwonlyargcount
            if n:
                loc = gf(1).f_locals; ts = state['types'].setdefault(state['cur'], set())
                for v in code.co_varnames[:n]:
                    x = loc.get(v)
                    if x is not None:
                        ts.add(x if isinstance(x, type) else type(x))
        mon.register_callback(5, mon.events.LINE, on_line)
        mon.register_callback(5, mon.events.PY_START, on_start)
    so, se = sys.stdout, sys.stderr; sys.stdout = io.StringIO(); sys.stderr = io.StringIO()
    try:
        klass.setUpClass()
        state['t'] = time.process_time()
        if instrument: mon.set_events(5, mon.events.LINE | mon.events.PY_START)
        r = unittest.TestResult(); klass(meth).run(r)
        if instrument: mon.set_events(5, 0)
        switch(BASE)
    finally:
        sys.stdout, sys.stderr = so, se
    def tkey(t):
        m = getattr(t, '__module__', '') or ''
        return '%s:%s' % (m, t.__qualname__) if m.startswith(('sqlglot', 'tests')) else None
    return {'ok': r.wasSuccessful(), 'n': state['n'], 'cost': state['cost'], 'lines': state['lines'],
            'types': {k: {x for x in map(tkey, v) if x} for k, v in state['types'].items()}}

def forked(tid, instrument):
    rd, wr = os.pipe(); pid = os.fork()
    if pid == 0:
        os.close(rd)
        try: res = run(tid, instrument)
        except BaseException:
            import traceback; res = {'error': traceback.format_exc()}
        data = pickle.dumps(res)
        with os.fdopen(wr, 'wb') as w: w.write(data)
        os._exit(0)
    os.close(wr)
    with os.fdopen(rd, 'rb') as r: data = r.read()
    os.waitpid(pid, 0)
    return pickle.loads(data)

def compact(d):
    """Keep only lines/types used by fewer than half of the calls, as bitmasks over calls."""
    n = d['n']
    lm = {}; tm = {}
    for k, lines in d['lines'].items():
        if k == BASE: continue
        b = 1 << k[1]
        for f, l in lines:
            lm.setdefault(f, {}); lm[f][l] = lm[f].get(l, 0) | b
    for k, ts in d['types'].items():
        if k == BASE: continue
        b = 1 << k[1]
        for t in ts: tm[t] = tm.get(t, 0) | b
    half = n / 2.0
    lm = {f: {l: m for l, m in ls.items() if bin(m).count('1') < half} for f, ls in lm.items()}
    lm = {f: ls for f, ls in lm.items() if ls}
    tm = {t: m for t, m in tm.items() if bin(m).count('1') < half}
    return lm, tm

out = {}
tests = [t for t in T if t['file'].startswith('tests/dialects/')]
tests = tests[shard::nsh]
for t in tests:
    a = forked(t['id'], False)
    if a is None or 'error' in a or a['n'] < 4: continue
    b = forked(t['id'], True)
    if b is None or 'error' in b or not b['ok'] or b['n'] != a['n']:
        print('SKIP', t['id'], (b or {}).get('error', '')[-200:] if b else '', file=sys.stderr); continue
    lm, tm = compact(b)
    costs = [round(a['cost'].get(('call', i), 0.0), 5) for i in range(a['n'])]
    out[t['id']] = {'n': a['n'], 'cost': costs, 'base': round(a['cost'].get(BASE, 0.0), 4), 'lines': lm, 'types': tm}
pickle.dump(out, open(OUT, 'wb'))
print('done', len(out), file=sys.stderr)
