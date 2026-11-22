"""Compare the reduced-data TPC-DS probe with the real test on mutants.
usage: tpcheck.py MUTDIR OUT.json"""
import sys, os, json, time, pickle, zlib
sys.path.insert(0, '/home/claude/work/repo')
os.environ['SQLGLOT_ROOT'] = ROOT = '/home/claude/work/sqlglot'; os.environ['SKIP_INTEGRATION'] = '0'
import analyze, runner, selector
selector.ROOT = ROOT
MUT, OUTF = sys.argv[1], sys.argv[2]
T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
TID = 'tests/test_executor.py::TestExecutor::test_execute_tpcds'
NUM = [t['number'] for t in T if t['id'] == TID][0]
mp = analyze.Map('/home/claude/work/repo/map.bin')
probes = pickle.loads(zlib.decompress(open('/home/claude/work/repo/probes.bin', 'rb').read()))
ctx = {'mp': mp, 'map_index': {m: i for i, m in enumerate(mp.ids)}, 'probes': probes}
hbit = 1 << ctx['map_index'][TID]
res = json.load(open(OUTF)) if os.path.exists(OUTF) else {}
warm = runner.Warm({t['file'] for t in T}, need_refs=True)
jobs = []
for f in sorted(os.listdir(os.path.join(MUT, 'commits'))):
    cid = f[:-6]
    if cid in res: continue
    r = analyze.analyze(open(os.path.join(MUT, 'commits', f)).read(), ROOT, mp)
    if r.everything or not r.runnable or r.data or not (r.bits & hbit):
        continue
    got = selector._small_probe(r, ctx, hbit, probes['tpcds'])
    if got is None:
        res[cid] = {'probe': None}
        continue
    jobs.append((cid, r, got[0].replace('|quiet', '')))
print('jobs', len(jobs), file=sys.stderr)
lim = {NUM: 200}
for i in range(0, len(jobs), 2):
    chunk = jobs[i:i + 2]
    t0 = time.time()
    a = runner.run_jobs(warm, [(c, {'py': r.py, 'data': r.data, 'order': [(NUM, pid)], 'limits': lim, 'stop_on_fail': False}) for c, r, pid in chunk], parallel=2, log=lambda *a: None)
    cpu = dict(runner.CHILD_CPU)
    b = runner.run_jobs(warm, [(c, {'py': r.py, 'data': r.data, 'order': [(NUM, TID)], 'limits': lim, 'stop_on_fail': False}) for c, r, pid in chunk], parallel=2, log=lambda *a: None)
    for c, r, pid in chunk:
        res[c] = {'probe': a.get(c, {}).get(NUM), 'real': b.get(c, {}).get(NUM), 'pid': pid, 'cpu': round(cpu.get(c, 0), 3)}
        flag = '' if res[c]['probe'] == res[c]['real'] else ('  <<< MISS' if res[c]['probe'] == 'P' else '  (spurious)')
        print(c, res[c]['probe'], res[c]['real'], res[c]['cpu'], flag, flush=True)
    json.dump(res, open(OUTF, 'w'))
print('DONE', len(res))
