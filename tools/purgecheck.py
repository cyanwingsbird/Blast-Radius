"""For every sqlglot module: reload it (unchanged) through the partial purge and run tests that use it.
Nothing changed, so every test must pass; a failure means the purge leaves stale state."""
import sys, os, json
sys.path.insert(0, '/home/claude/work/repo')
os.environ['SQLGLOT_ROOT'] = ROOT = '/home/claude/work/sqlglot'; os.environ['SKIP_INTEGRATION'] = '0'
import analyze, runner
T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
cost = json.load(open('/home/claude/work/repo/costs.json'))['cost']
mp = analyze.Map('/home/claude/work/repo/map.bin')
num = {i: n for n, i in enumerate(mp.ids)}
warm = runner.Warm({t['file'] for t in T}, need_refs=True)
mods = sorted(p for p in mp._blobs if p.startswith('sqlglot/') and p.endswith('.py'))
cheap_per_file = {}
for t in sorted(T, key=lambda t: cost[t['id']]):
    cheap_per_file.setdefault(t['file'], t)
jobs = []
skip = []
for rel in mods:
    name = runner.modname(rel)
    if name == 'sqlglot' or name.startswith(('sqlglot.expressions', 'sqlglot.dialects.dialect')):
        skip.append(rel); continue
    cov, anyb = mp.file(rel)
    users = sorted((t for t in T if anyb >> num[t['id']] & 1 and 'test_lazy_load' not in t['id'] and cost[t['id']] < 2.0),
                   key=lambda t: cost[t['id']])[:30]
    chosen = {t['number']: t for t in users}
    for t in cheap_per_file.values():
        if 'test_lazy_load' not in t['id']:
            chosen.setdefault(t['number'], t)
    text = open(os.path.join(ROOT, rel)).read()
    order = [(n, t['id']) for n, t in sorted(chosen.items())]
    jobs.append((rel, {'py': [(rel, text, None)], 'data': [], 'order': order, 'limits': {n: 60 for n, _ in order}, 'stop_on_fail': False}))
print('modules', len(jobs), 'skipped (always full purge)', len(skip), file=sys.stderr)
out = runner.run_jobs(warm, jobs, parallel=2)
bad = {}
for rel, job in jobs:
    res = out.get(rel, {})
    f = [T[n]['id'] for n, s in res.items() if s != 'P']
    missing = len(job['order']) - len(res)
    if f or missing:
        bad[rel] = (f[:5], missing)
print('modules with failures', len(bad))
for k, v in sorted(bad.items()): print(k, v)
