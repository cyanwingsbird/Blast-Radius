"""Ground truth for mutant patches by running the tests with the in-process runner.
usage: groundtruth.py MUTDIR OUT.json   (runs all non-heavy tests; heavy ones only if statically reachable)"""
import sys, os, json, time
sys.path.insert(0, '/home/claude/work/repo')
os.environ['SQLGLOT_ROOT'] = ROOT = '/home/claude/work/sqlglot'; os.environ['SKIP_INTEGRATION'] = '0'
import analyze, runner
MUT, OUTF = sys.argv[1], sys.argv[2]
T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
mp = analyze.Map('/home/claude/work/repo/map.bin')
HEAVY = {'tests/test_executor.py::TestExecutor::test_execute_tpcds', 'tests/test_parser.py::TestParser::test_max_nodes'}
res = json.load(open(OUTF)) if os.path.exists(OUTF) else {}
warm = runner.Warm({t['file'] for t in T})
jobs = []
for f in sorted(os.listdir(os.path.join(MUT, 'commits'))):
    cid = f[:-6]
    if cid in res: continue
    r = analyze.analyze(open(os.path.join(MUT, 'commits', f)).read(), ROOT, mp)
    cand = set(range(len(T))) if r.everything else {i for i in range(len(T)) if r.bits >> i & 1}
    order = [(t['number'], t['id']) for t in T if t['id'] not in HEAVY or t['number'] in cand]
    limits = {t['number']: 3 * t['seconds'] + 8 for t in T}
    jobs.append((cid, {'py': r.py, 'data': r.data, 'order': order, 'limits': limits, 'stop_on_fail': False, 'cand': sorted(cand), 'notes': r.notes}))
for i in range(0, len(jobs), 4):
    chunk = jobs[i:i + 4]
    out = runner.run_jobs(warm, chunk, parallel=2, log=lambda *a: print(*a, file=sys.stderr))
    for cid, job in chunk:
        rr = out.get(cid, {})
        res[cid] = {'fail': sorted(n for n, s in rr.items() if s != 'P'), 'ran': len(rr), 'n': len(job['order']),
                    'cand': job['cand'], 'notes': job['notes'][:5]}
    json.dump(res, open(OUTF, 'w'))
