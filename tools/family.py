"""For each sqlglot class: the keys of related classes (itself, subclasses, nested, linked by class-valued attrs)."""
import sys, os, json, importlib, pkgutil, inspect
sys.path.insert(0, '/home/claude/work/sqlglot'); os.chdir('/home/claude/work/sqlglot')
import sqlglot
mods = []
for info in pkgutil.walk_packages(sqlglot.__path__, prefix='sqlglot.'):
    if info.name.endswith('__main__'): continue
    try: mods.append(importlib.import_module(info.name))
    except Exception as e: print('skip', info.name, e, file=sys.stderr)
from sqlglot.dialects.dialect import Dialect, Dialects
for d in Dialects:
    if d.value:
        try: Dialect.get_or_raise(d.value)
        except Exception: pass
def key(c): return '%s:%s' % (c.__module__, c.__qualname__)
classes = {}
def visit(c):
    if not isinstance(c, type): return
    m = getattr(c, '__module__', '') or ''
    if not m.startswith('sqlglot'): return
    k = key(c)
    if k in classes: return
    classes[k] = c
    for v in list(vars(c).values()):
        if isinstance(v, type): visit(v)
    for s in type.__subclasses__(c): visit(s)
for m in list(sys.modules.values()):
    if m is None or not getattr(m, '__name__', '').startswith('sqlglot'): continue
    for v in list(vars(m).values()):
        visit(v)
print(len(classes), file=sys.stderr)
link = {k: set() for k in classes}
for k, c in classes.items():
    # nested classes (by qualname prefix), class-valued attributes (incl inherited), both directions
    for name in dir(c):
        try: v = getattr(c, name)
        except Exception: continue
        if isinstance(v, type) and key(v) in classes and name not in ('__class__', '__base__'):
            if v in c.__mro__: continue
            link[k].add(key(v)); link[key(v)].add(k)
fam = {}
def subs(c):
    out = set(); st = [c]
    while st:
        x = st.pop()
        for s in type.__subclasses__(x):
            if key(s) in classes and key(s) not in out:
                out.add(key(s)); st.append(s)
    return out
subcache = {k: subs(c) for k, c in classes.items()}
for k, c in classes.items():
    base = {k} | subcache[k]
    # nested classes of the class and of its subclasses
    pref = [b + '.' for b in base]
    nested = {k2 for k2 in classes if any(k2.startswith(p) for p in pref)}
    s = base | nested
    linked = set()
    for x in s: linked |= link[x]
    for x in list(linked): linked |= subcache.get(x, set())
    fam[k] = sorted(s | linked)
json.dump(fam, open('/home/claude/work/family.json', 'w'))
import statistics
print('median family', statistics.median(len(v) for v in fam.values()), max(len(v) for v in fam.values()), file=sys.stderr)
for k in ['sqlglot.dialects.snowflake:Snowflake.Tokenizer', 'sqlglot.dialects.tsql:TSQL', 'sqlglot.expressions.ddl:TriggerProperties', 'sqlglot.parsers.singlestore:SingleStoreParser']:
    print(k, len(fam.get(k, [])), fam.get(k, [])[:12], file=sys.stderr)
