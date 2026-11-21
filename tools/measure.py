# measure per-test CPU in the runner (unpatched), one child per test file, cheap-first irrelevant
import sys, os, json, time, resource
sys.path.insert(0, '/home/claude/work/repo')
os.environ['SQLGLOT_ROOT'] = '/home/claude/work/sqlglot'
import runner
T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
t0 = time.process_time(); w0 = time.time()
warm = runner.Warm({t['file'] for t in T})
print('warm cpu', time.process_time() - t0, 'wall', time.time() - w0, file=sys.stderr)
res = {}
byfile = {}
for t in T: byfile.setdefault(t['file'], []).append((t['number'], t['id']))
for f, order in byfile.items():
    r, w = os.pipe()
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    pid = os.fork()
    if pid == 0:
        os.close(r); runner.child_main(warm, {'py': [], 'data': [], 'order': order}, w)
    os.close(w)
    data = b''
    while True:
        c = os.read(r, 65536)
        if not c: break
        data += c
    os.waitpid(pid, 0)
    ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    tot = ru1.ru_utime + ru1.ru_stime - ru0.ru_utime - ru0.ru_stime
    for line in data.decode().splitlines():
        if line == 'DONE': continue
        n, st, s = line.split(); res[int(n)] = (st, float(s))
    print(f, len(order), 'child cpu %.2f' % tot, 'sum tests %.2f' % sum(res[n][1] for n, _ in order if n in res), file=sys.stderr)
json.dump(res, open('/home/claude/work/measure.json', 'w'))
bad = [T[n]['id'] for n, (st, _) in res.items() if st != 'P']
print('not passing', len(bad), bad[:20], 'missing', [t['id'] for t in T if t['number'] not in res][:10], file=sys.stderr)
