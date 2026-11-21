"""One-character edits to fixture files, in the style of the sample fixture commits.  usage: mkfixmut.py N SEED OUT"""
import os, random, sys, difflib
N, SEED, OUT = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
R = random.Random(SEED)
ROOT = '/home/claude/work/sqlglot'
files = []
for dp, dn, fn in os.walk(os.path.join(ROOT, 'tests/fixtures')):
    for f in fn:
        if f.endswith('.sql'): files.append(os.path.relpath(os.path.join(dp, f), ROOT))
os.makedirs(os.path.join(OUT, 'commits'), exist_ok=True)
made = 0
while made < N:
    f = R.choice(files); L = open(os.path.join(ROOT, f)).read().split('\n')
    cands = [i for i, l in enumerate(L) if l.strip() and not l.startswith('--')]
    if not cands: continue
    i = R.choice(cands); line = L[i]
    kind = R.random()
    j = R.randrange(len(line))
    if kind < 0.4: new = line[:j] + line[j + 1:]                           # drop a char
    elif kind < 0.7: new = line[:j] + R.choice('x1 ,(') + line[j:]          # insert a char
    elif kind < 0.85: new = line + ' '                                      # trailing space
    else: new = '-- ' + line if not line.startswith('#') else line + ' '   # comment it out
    if new == line: continue
    L2 = list(L); L2[i] = new
    diff = ''.join(difflib.unified_diff([x + '\n' for x in L], [x + '\n' for x in L2], 'a/' + f, 'b/' + f, n=3))
    open(os.path.join(OUT, 'commits', 'f%d_%d.patch' % (SEED, made)), 'w').write('diff --git a/%s b/%s\n' % (f, f) + diff)
    made += 1
