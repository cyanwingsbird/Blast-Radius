"""Generate single-line mutants of sqlglot source as unified diffs.  usage: mkmut.py N SEED OUTDIR [focus]"""
import ast, os, random, sys, difflib, json, pickle, zlib
sys.path.insert(0, '/home/claude/work/repo')
import analyze
N, SEED, OUT = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
focus = sys.argv[4] if len(sys.argv) > 4 else None
R = random.Random(SEED)
ROOT = '/home/claude/work/sqlglot'
mp = analyze.Map('/home/claude/work/repo/map.bin')
ids = mp.ids
fnum = {i: n for n, i in enumerate(ids)}
heavy = {fnum['tests/test_executor.py::TestExecutor::test_execute_tpcds'], fnum['tests/test_parser.py::TestParser::test_max_nodes']}
files = [f for f in mp.files if f.startswith('sqlglot/') and f.endswith('.py')]
CMP = {ast.Eq: '!=', ast.NotEq: '==', ast.Lt: '<=', ast.LtE: '<', ast.Gt: '>=', ast.GtE: '>', ast.Is: '==', ast.IsNot: '!=', ast.In: 'not in', ast.NotIn: 'in'}
os.makedirs(os.path.join(OUT, 'commits'), exist_ok=True)
made = 0; tries = 0
while made < N and tries < N * 200:
    tries += 1
    f = R.choice(files)
    cov, anyb = mp.file(f)
    lines = [l for l, (d, r) in cov.items() if r]
    if focus == 'heavy':
        lines = [l for l in lines if any((cov[l][1] >> h) & 1 for h in heavy)]
    elif focus == 'def':
        lines = [l for l, (d, r) in cov.items() if d and not r]
    if not lines: continue
    src = open(os.path.join(ROOT, f)).read(); L = src.split('\n')
    ln = R.choice(lines); line = L[ln - 1]
    try: tree = ast.parse(src)
    except Exception: continue
    cands = []
    for node in ast.walk(tree):
        if getattr(node, 'lineno', None) != ln or getattr(node, 'end_lineno', None) != ln: continue
        seg = ast.get_source_segment(src, node)
        if not seg or line.count(seg) != 1: continue
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in CMP:
            l = ast.get_source_segment(src, node.left); r = ast.get_source_segment(src, node.comparators[0])
            if l and r: cands.append((seg, '%s %s %s' % (l, CMP[type(node.ops[0])], r)))
        elif isinstance(node, ast.BoolOp) and len(node.values) == 2:
            a, b = (ast.get_source_segment(src, v) for v in node.values)
            if a and b and '\n' not in a + b:
                op = 'or' if isinstance(node.op, ast.And) else 'and'
                cands.append((seg, '%s %s %s' % (a, op, b)))
        elif isinstance(node, ast.Constant) and type(node.value) is bool:
            cands.append((seg, str(not node.value)))
        elif isinstance(node, ast.Constant) and type(node.value) is int and seg.isdigit():
            cands.append((seg, str(node.value + 1)))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = ast.get_source_segment(src, node.operand)
            if inner: cands.append((seg, inner))
        elif isinstance(node, ast.Constant) and type(node.value) is str and len(node.value) > 1 and seg[0] in '"\'' and seg.count(seg[0]) == 2:
            cands.append((seg, seg[0] + node.value[:-1] + seg[0]))
    if not cands: continue
    old, new = R.choice(cands)
    L2 = list(L); L2[ln - 1] = line.replace(old, new, 1)
    if L2[ln - 1] == line: continue
    diff = ''.join(difflib.unified_diff([x + '\n' for x in L], [x + '\n' for x in L2], 'a/' + f, 'b/' + f, n=3))
    cid = 'm%d_%d' % (SEED, made)
    open(os.path.join(OUT, 'commits', cid + '.patch'), 'w').write('diff --git a/%s b/%s\n' % (f, f) + diff)
    made += 1
print(made, tries)
