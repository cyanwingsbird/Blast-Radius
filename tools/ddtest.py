"""Prototype: data diff per commit.  usage: ddtest.py MUTDIR OUT.pickle"""
import sys, os, json, pickle, time, importlib, resource
sys.path.insert(0, '/home/claude/work/repo')
os.environ['SQLGLOT_ROOT'] = ROOT = '/home/claude/work/sqlglot'; os.environ['SKIP_INTEGRATION'] = '0'
import analyze, runner, datadiff
MUT, OUTF = sys.argv[1], sys.argv[2]
T = json.load(open(os.path.join(MUT, 'tests.json')))['tests']
mp = analyze.Map('/home/claude/work/repo/map.bin')
jobs = []
for f in sorted(os.listdir(os.path.join(MUT, 'commits'))):
    cid = f[:-6]
    r = analyze.analyze(open(os.path.join(MUT, 'commits', f)).read(), ROOT, mp)
    if not r.py or r.data:
        continue
    if not any(n.startswith(("def-time", "class ")) for n in r.notes):
        continue
    jobs.append((cid, r))
warm = runner.Warm({t['file'] for t in T}, need_refs=True)
out = {}
for cid, r in jobs:
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        t0 = time.process_time()
        try:
            old = {n: m for n, m in sys.modules.items() if m is not None and runner._ours(n)}
            warm.apply(r.py, [], analyze._sig,
                       lambda rel: analyze.load_old_code(ROOT, rel, open(os.path.join(ROOT, rel)).read()))
            purged = [n for n in old if n not in sys.modules]
            changed = {runner.modname(rel) for rel, _t, _c in r.py}
            lazy_missing = [n for n in changed if n not in old]
            pairs = {}
            err = None
            for n in purged:
                if n.startswith('tests'):
                    continue
                try:
                    pairs[n] = (old[n], importlib.import_module(n))
                except Exception as e:
                    err = repr(e)[:200]
                    break
            t1 = time.process_time()
            if err:
                res = {'err': err}
            else:
                ent = datadiff.diff_modules(pairs, set(pairs))
                res = {'entries': datadiff.describe(ent, ROOT), 'npurged': len(purged),
                       'lazy_missing': lazy_missing}
            res['cpu_import'] = t1 - t0
            res['cpu'] = time.process_time() - t0
        except Exception as e:
            import traceback
            res = {'err': traceback.format_exc()[-800:]}
        os.write(wfd, pickle.dumps(res))
        os._exit(0)
    os.close(wfd)
    data = b''
    while True:
        c = os.read(rfd, 1 << 20)
        if not c:
            break
        data += c
    os.waitpid(pid, 0)
    res = pickle.loads(data) if data else {'err': 'no output'}
    out[cid] = res
    print(cid, res.get('err', '')[:300] if 'err' in res else '%d entries purged=%d cpu=%.2f/%.2f %s' % (
        len(res['entries']), res['npurged'], res['cpu_import'], res['cpu'], [(e["where"], e["attr"], (e.get("key") or ("",))[:2], bool(e.get("func_lines"))) for e in res["entries"][:5]]), file=sys.stderr)
pickle.dump(out, open(OUTF, 'wb'))
