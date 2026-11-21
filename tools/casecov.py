"""Per fixture-item coverage, class usage and CPU for tests that iterate SQL fixtures.
usage: casecov.py OUT.pickle"""
import sys, os, json, time, io, pickle, contextlib, unittest
ROOT = '/home/claude/work/sqlglot'
sys.path.insert(0, ROOT); os.chdir(ROOT); os.environ['SKIP_INTEGRATION'] = '0'
sys.path.insert(0, '/home/claude/work/repo')
import runner
OUT = sys.argv[1]
T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
MODS = ('tests/test_optimizer.py', 'tests/test_transpile.py', 'tests/test_serde.py')
LOADERS = ('load_sql_fixture_pairs', 'load_sql_fixtures')
BASE = ('<base>',)

def install(state, mod):
    """Wrap the fixture loaders in mod so the current item is known."""
    for name in LOADERS:
        orig = getattr(mod, name, None)
        if orig is None: continue
        def make(orig, name):
            def gen(filename, *a, **k):
                for idx, item in enumerate(orig(filename, *a, **k)):
                    key = (filename, idx)
                    if name == 'load_sql_fixture_pairs':
                        meta, sql, _exp = item
                        title = meta.get('title') or f'{idx + 1}, {sql}'
                        state['titles'][title] = key
                        state['titles'][f'(execute) {title}'] = key
                    state['switch'](key)
                    yield item
                state['switch'](BASE)
            return gen
        setattr(mod, name, make(orig, name))
    orig_sub = unittest.TestCase.subTest
    @contextlib.contextmanager
    def sub(self, *args, **kw):
        msg = args[0] if args else None
        key = state['titles'].get(msg) if isinstance(msg, str) else None
        if key is not None:
            prev = state['cur']; state['switch'](key)
        try:
            with orig_sub(self, *args, **kw):
                yield
        finally:
            if key is not None:
                state['switch'](prev)
    unittest.TestCase.subTest = sub

def run(tid, instrument):
    import importlib
    f, cls, meth = tid.split('::')
    mod = importlib.import_module(f[:-3].replace('/', '.'))
    runner.install_sync_pools()
    state = {'cur': BASE, 'titles': {}, 'cost': {}, 't': time.process_time(), 'lines': {}, 'types': {}}
    def switch(key):
        now = time.process_time()
        state['cost'][state['cur']] = state['cost'].get(state['cur'], 0.0) + now - state['t']
        state['t'] = now; state['cur'] = key
    state['switch'] = switch
    install(state, mod)
    klass = getattr(mod, cls)
    if instrument:
        pre = ROOT + '/'
        mon = sys.monitoring; mon.use_tool_id(5, 'case')
        gf = sys._getframe
        def on_line(code, line):
            fn = code.co_filename
            if fn.startswith(pre):
                state['lines'].setdefault(state['cur'], set()).add((fn[len(pre):], line))
        def on_start(code, off):
            fn = code.co_filename
            if not fn.startswith(pre):
                return mon.DISABLE
            s = state['lines'].setdefault(state['cur'], set()); s.add((fn[len(pre):], code.co_firstlineno))
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
        state['t'] = time.process_time(); state['cur'] = BASE
        if instrument: mon.set_events(5, mon.events.LINE | mon.events.PY_START)
        r = unittest.TestResult(); klass(meth).run(r)
        if instrument: mon.set_events(5, 0)
        switch(BASE)
    finally:
        sys.stdout, sys.stderr = so, se
    def tkey(t):
        m = getattr(t, '__module__', '') or ''
        return '%s:%s' % (m, t.__qualname__) if m.startswith(('sqlglot', 'tests')) else None
    return {'ok': r.wasSuccessful(), 'cost': state['cost'], 'lines': state['lines'],
            'types': {k: {x for x in map(tkey, v) if x} for k, v in state['types'].items()}}

def forked(tid, instrument):
    rd, wr = os.pipe(); pid = os.fork()
    if pid == 0:
        os.close(rd)
        try: res = run(tid, instrument)
        except BaseException as e:
            import traceback; res = {'error': traceback.format_exc()}
        data = pickle.dumps(res)
        with os.fdopen(wr, 'wb') as w: w.write(data)
        os._exit(0)
    os.close(wr)
    with os.fdopen(rd, 'rb') as r: data = r.read()
    os.waitpid(pid, 0)
    return pickle.loads(data)

out = {}
for t in T:
    if t['file'] not in MODS: continue
    a = forked(t['id'], False)
    if 'error' in a: print('ERR', t['id'], a['error'][-300:], file=sys.stderr); continue
    items = [k for k in a['cost'] if k != BASE]
    if len(items) < 2: continue
    b = forked(t['id'], True)
    if 'error' in b or not b['ok']:
        print('ERR/FAIL', t['id'], b.get('error', '')[-300:], file=sys.stderr); continue
    out[t['id']] = {'cost': a['cost'], 'lines': b['lines'], 'types': b['types']}
    print(t['id'], len(items), 'base %.3f items %.3f' % (a['cost'].get(BASE, 0), sum(v for k, v in a['cost'].items() if k != BASE)), file=sys.stderr)
pickle.dump(out, open(OUT, 'wb'))
