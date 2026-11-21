"""Classes from sqlglot used (as self, cls or argument) while sqlglot, its dialects and the test modules are imported."""
import sys, os, json
ROOT = '/home/claude/work/sqlglot'
sys.path.insert(0, ROOT); os.chdir(ROOT); os.environ['SKIP_INTEGRATION'] = '0'
seen = set()
mon = sys.monitoring; mon.use_tool_id(4, 'imp')
gf = sys._getframe
pre = ROOT + '/'
def on_start(code, off):
    if not code.co_filename.startswith(pre):
        return mon.DISABLE
    n = code.co_argcount + code.co_kwonlyargcount
    if n:
        loc = gf(1).f_locals
        for v in code.co_varnames[:n]:
            x = loc.get(v)
            if x is not None and not isinstance(x, type):
                seen.add(type(x))
mon.register_callback(4, mon.events.PY_START, on_start)
mon.set_events(4, mon.events.PY_START)
import importlib, pkgutil
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix='sqlglot.'):
    if info.name.endswith('__main__'): continue
    try: importlib.import_module(info.name)
    except Exception: pass
T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
for f in sorted({t['file'] for t in T}):
    try: importlib.import_module(f[:-3].replace('/', '.'))
    except Exception: pass
mon.set_events(4, 0)
keys = set()
for t in seen:
    for c in getattr(t, '__mro__', (t,)):
        if (getattr(c, '__module__', '') or '').startswith('sqlglot'):
            keys.add('%s:%s' % (c.__module__, c.__qualname__))
keys = sorted(keys)
json.dump(keys, open('/home/claude/work/import_used.json', 'w'))
print(len(keys), [k for k in keys if k.endswith(':Cast') or 'DataType' in k][:10])
