"""Check the comparison-swap narrowing: make operator-swap mutants at recorded sites,
run every test the narrowing drops, and report any that fails.
usage: cmpcheck.py N SEED OUT.json [max_drop_cpu]"""
import ast, os, random, sys, difflib, json, pickle, zlib, re, time
sys.path.insert(0, '/home/claude/work/repo')
os.environ['SQLGLOT_ROOT'] = ROOT = '/home/claude/work/sqlglot'
os.environ['SKIP_INTEGRATION'] = '0'
import analyze, runner
N, SEED, OUTF = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
MAXCPU = float(sys.argv[4]) if len(sys.argv) > 4 else 1e9
R = random.Random(SEED)
T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
mp = analyze.Map('/home/claude/work/repo/map.bin')
num_by_id = {t['id']: t['number'] for t in T}
by_num = {t['number']: t for t in T}
map_nums = [num_by_id.get(i) for i in mp.ids]
costs = json.load(open('/home/claude/work/repo/costs.json'))['cost']
TOK = ['is', 'is not', '==', '!=', '<', '<=', '>', '>=']
ALT = {0: [2], 1: [3], 2: [0, 0, 5, 7], 3: [1, 1, 4, 6], 4: [5, 5, 5, 6, 3], 5: [4, 4, 4, 2], 6: [7, 7, 7, 4, 3], 7: [6, 6, 6, 2]}
PAT = re.compile(r'\bis\s+not\b|\bis\b|==|!=|<=|>=|<|>')
mp.cmp_sites('x')
sites = [(p, k, v) for p, ss in mp._cmp['sites'].items() for k, v in ss.items() if v[1]]
R.shuffle(sites)


def nums(bits):
    out = set(); i = 0
    while bits:
        if bits & 1 and map_nums[i] is not None:
            out.add(map_nums[i])
        bits >>= 1; i += 1
    return out


res = json.load(open(OUTF)) if os.path.exists(OUTF) else {}
warm = runner.Warm({t['file'] for t in T})
made = 0
bad = 0
t0 = time.time()
for p, (ln, col), (op, per, gl) in sites:
    if made >= N:
        break
    src = open(os.path.join(ROOT, p)).read()
    tree = ast.parse(src)
    node = next((n for n in ast.walk(tree) if isinstance(n, ast.Compare) and n.lineno == ln and n.col_offset == col), None)
    if node is None:
        continue
    new_op = R.choice(ALT[op])
    L = src.split('\n')
    a = (node.left.end_lineno, node.left.end_col_offset)
    b = (node.comparators[0].lineno, node.comparators[0].col_offset)
    if a[0] != b[0]:
        continue
    line = L[a[0] - 1].encode()
    gap = line[a[1]:b[1]].decode()
    m = PAT.search(gap)
    if not m or re.sub(r'\s+', ' ', m.group(0)) != TOK[op]:
        continue
    L2 = list(L)
    L2[a[0] - 1] = (line[:a[1]] + (gap[:m.start()] + TOK[new_op] + gap[m.end():]).encode() + line[b[1]:]).decode()
    diff = 'diff --git a/%s b/%s\n' % (p, p) + ''.join(difflib.unified_diff([x + '\n' for x in L], [x + '\n' for x in L2], 'a/' + p, 'b/' + p, n=3))
    cid = 'k%d_%s_%d_%d_%s' % (SEED, p.replace('/', '.'), ln, col, TOK[new_op].replace(' ', ''))
    if cid in res:
        continue
    os.environ['BR_NO_CMP'] = '1'
    r0 = analyze.analyze(diff, ROOT, mp)
    del os.environ['BR_NO_CMP']
    r1 = analyze.analyze(diff, ROOT, mp)
    if r0.everything or r1.everything or not any('+cmp' in n for n in r1.notes):
        continue
    full, narrow = nums(r0.bits), nums(r1.bits)
    assert narrow <= full
    drop = sorted(full - narrow)
    cpu = sum(costs.get(by_num[n]['id'], by_num[n]['seconds']) for n in drop)
    if not drop or cpu > MAXCPU:
        continue
    order = [(n, by_num[n]['id']) for n in drop]
    limits = {n: 3 * by_num[n]['seconds'] + 8 for n in drop}
    out = runner.run_jobs(warm, [(cid, {'py': r1.py, 'data': r1.data, 'order': order, 'limits': limits, 'stop_on_fail': False})],
                          parallel=1, log=lambda *a: None)
    rr = out.get(cid, {})
    fail = sorted(n for n in drop if rr.get(n) != 'P')
    res[cid] = {'full': len(full), 'narrow': len(narrow), 'drop': len(drop), 'fail': fail, 'op': TOK[op], 'new': TOK[new_op]}
    made += 1
    if fail:
        bad += 1
        print('UNSOUND', cid, TOK[op], '->', TOK[new_op], 'dropped', len(drop), 'failing', [(n, rr.get(n), by_num[n]['id']) for n in fail[:5]], flush=True)
    if made % 10 == 0:
        print('checked', made, 'bad', bad, 'elapsed %.0fs' % (time.time() - t0), flush=True)
        json.dump(res, open(OUTF, 'w'))
json.dump(res, open(OUTF, 'w'))
print('DONE checked', made, 'bad', bad)
