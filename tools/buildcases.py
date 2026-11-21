"""casemap.bin: for fixture-driven tests, per item line/class masks and CPU, plus class families."""
import pickle, zlib, json, os, sys
src = pickle.load(open(sys.argv[1], 'rb')); out = sys.argv[2]
BASE = ('<base>',)
tests = {}
for tid, d in src.items():
    items = sorted(k for k in d['cost'] if k != BASE)
    pos = {k: i for i, k in enumerate(items)}
    lm = {}
    for k, lines in d['lines'].items():
        if k == BASE: continue
        b = 1 << pos[k]
        for f, l in lines:
            fl = lm.setdefault(f, {}); fl[l] = fl.get(l, 0) | b
    tm = {}
    for k, ts in d['types'].items():
        if k == BASE: continue
        b = 1 << pos[k]
        for t in ts: tm[t] = tm.get(t, 0) | b
    tests[tid] = {'items': items, 'cost': [round(d['cost'][k], 5) for k in items], 'base': round(d['cost'].get(BASE, 0.0), 4),
                  'lines': lm, 'types': tm}
fam = json.load(open('/home/claude/work/family.json'))
blob = zlib.compress(pickle.dumps({'tests': tests, 'family': fam}, protocol=5), 9)
open(out, 'wb').write(blob); print(len(blob))
