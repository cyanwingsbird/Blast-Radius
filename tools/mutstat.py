import json, sys
T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
d = json.load(open(sys.argv[1]))
H = {t['number']: t['id'].split('::')[-1] for t in T if t['id'].split('::')[-1] in ('test_execute_tpcds', 'test_max_nodes')}
nf = 0; miss = 0
for cid, v in sorted(d.items()):
    f = set(v['fail']); c = set(v['cand'])
    m = f - c
    nf += bool(f)
    if m: miss += 1
    hc = [H[h] for h in H if h in c]
    hf = [H[h] for h in H if h in f]
    others = f - set(H)
    flag = 'MISS ' + str([T[n]['id'] for n in list(m)[:3]]) if m else ''
    print(cid, 'fail=%d cand=%d ran=%d/%d heavyC=%s heavyF=%s otherF=%d %s %s' % (len(f), len(c), v['ran'], v['n'], hc, hf, len(others), flag, v['notes'][:1]))
print('commits', len(d), 'with failures', nf, 'misses', miss)
