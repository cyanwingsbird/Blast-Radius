# Run every per-call-mapped test with all validate_* calls skipped, on unpatched code: which fail?
import sys, os, json, pickle, zlib
sys.path.insert(0, '/home/claude/work/repo')
os.environ['SQLGLOT_ROOT'] = ROOT = '/home/claude/work/sqlglot'; os.environ['SKIP_INTEGRATION'] = '0'
import runner
cm = pickle.loads(zlib.decompress(open('/home/claude/work/repo/callmap.bin', 'rb').read()))
T = {t['id']: t['number'] for t in json.load(open('/home/claude/sample/sample/tests.json'))['tests']}
warm = runner.Warm({i.split('::')[0] for i in cm})
order = [(T[i], ('CALLS', i, set())) for i in cm]
out = runner.run_jobs(warm, [('x', {'py': [], 'data': [], 'order': order, 'limits': {n: 60 for n, _ in order}, 'stop_on_fail': False})], parallel=1)['x']
bad = sorted(i for i in cm if out.get(T[i]) != 'P')
print('tests', len(cm), 'fail when all calls skipped', len(bad))
json.dump(bad, open('/home/claude/work/call_unfilterable.json', 'w'))
