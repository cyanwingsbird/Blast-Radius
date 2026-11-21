"""Per-query line coverage and timing for TestExecutor.test_execute_tpcds."""
import sys, os, json, time, pickle
ROOT = '/home/claude/work/sqlglot'
sys.path.insert(0, ROOT); os.chdir(ROOT); os.environ['SKIP_INTEGRATION'] = '0'
sys.path.insert(0, '/home/claude/work/repo')
import runner; runner.install_sync_pools()
import tests.test_executor as te
from sqlglot import parse_one, transpile
K = te.TestExecutor
K.setUpClass()
sqls = K.tpcds_sqls
te.initializer(*pickle.loads(pickle.dumps((te.TPCDS_SCHEMA, K.tpcds_tables))))
# timing pass, no instrumentation
times = []
for i, (meta, sql, _) in enumerate(sqls):
    t = time.process_time()
    e = parse_one(sql); a = pickle.loads(pickle.dumps(e)); r = te.mp_execute(a, meta)
    if r is not None:
        transpile(sql, write='duckdb')
    times.append(time.process_time() - t)
print('total', sum(times), 'executed', sum(1 for m, _, _ in sqls if m.get('execute')), file=sys.stderr)
pre = ROOT + '/'
mon = sys.monitoring; mon.use_tool_id(5, 'q')
cur = set()
def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(pre): cur.add((fn[len(pre):], line))
def on_start(code, off):
    fn = code.co_filename
    if fn.startswith(pre): cur.add((fn[len(pre):], code.co_firstlineno))
mon.register_callback(5, mon.events.LINE, on_line)
mon.register_callback(5, mon.events.PY_START, on_start)
mon.set_events(5, mon.events.LINE | mon.events.PY_START)
per = []
for i, (meta, sql, _) in enumerate(sqls):
    cur = set()
    e = parse_one(sql); a = pickle.loads(pickle.dumps(e)); r = te.mp_execute(a, meta)
    if r is not None:
        transpile(sql, write='duckdb')
    per.append(sorted(cur))
    print(i, len(cur), file=sys.stderr) if i % 20 == 0 else None
mon.set_events(5, 0)
json.dump({'times': times, 'execute': [bool(m.get('execute')) for m, _, _ in sqls], 'lines': per}, open('/home/claude/work/tpcds_q.json', 'w'))
