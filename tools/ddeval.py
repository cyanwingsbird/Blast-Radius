"""usage: ddeval.py MUTDIR DD.pickle"""
import sys, os, json, pickle
sys.path.insert(0, '/home/claude/work/repo')
ROOT = '/home/claude/work/sqlglot'
import analyze, narrow
MUT, DD = sys.argv[1], sys.argv[2]
T = json.load(open(os.path.join(MUT, 'tests.json')))['tests']
A = json.load(open(os.path.join(MUT, 'answers.json')))['commits']
mp = analyze.Map('/home/claude/work/repo/map.bin')
num_by_id = {t['id']: t['number'] for t in T}
sec = {t['number']: t['seconds'] for t in T}
src = narrow.Source(ROOT)
import zlib
KM = pickle.loads(zlib.decompress(open(sys.argv[3], 'rb').read())) if len(sys.argv) > 3 else None
nw = narrow.Narrower(mp, src, KM)
dd = pickle.load(open(DD, 'rb'))
tot_s = tot_f = 0
for cid, res in sorted(dd.items()):
    r = analyze.analyze(open(os.path.join(MUT, 'commits', cid + '.patch')).read(), ROOT, mp)
    cand = {num_by_id[mp.ids[i]] for i in range(len(mp.ids)) if (r.bits >> i) & 1 and mp.ids[i] in num_by_id}
    if r.everything:
        cand = set(sec)
    fail = set(A[cid]['failing'])
    if 'err' in res or res.get('lazy_missing'):
        bits = None
    else:
        bits = nw.compute(res['entries'])
    if bits is None:
        fin = cand
    else:
        keep = {num_by_id[mp.ids[i]] for i in range(len(mp.ids)) if (bits >> i) & 1 and mp.ids[i] in num_by_id}
        unmapped = {n for n in cand if T[n]['id'] not in mp.ids}
        fin = (cand & keep) | unmapped
    miss = fail - fin
    cs, cf = sum(sec[n] for n in cand), sum(sec[n] for n in fin)
    tot_s += cs; tot_f += cf
    print('%-8s cand %4d -> %4d  sec %7.2f -> %7.2f fail %d %s %s' % (
        cid, len(cand), len(fin), cs, cf, len(fail), 'MISS %s' % [T[n]['id'] for n in miss] if miss else '',
        'NONE' if bits is None else ''))
print('total sec %.1f -> %.1f' % (tot_s, tot_f))
