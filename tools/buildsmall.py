"""small.bin: a reduced copy of the TPC-DS tables, with what the pinned executor
answers on it for each query the test executes, the duckdb SQL the test would send,
and per-query line masks.  usage: buildsmall.py NFACT [out]"""
import sys, os, json, time, pickle, zlib
ROOT = '/home/claude/work/sqlglot'
sys.path.insert(0, ROOT); os.chdir(ROOT); os.environ['SKIP_INTEGRATION'] = '0'
sys.path.insert(0, '/home/claude/work/repo')
import runner; runner.install_sync_pools()
import tests.test_executor as te
from sqlglot import parse_one, transpile, find_tables
from sqlglot.executor import execute
from sqlglot.executor.table import Table
NF = int(sys.argv[1])
full = pickle.load(open('/tmp/tpcds_full.pickle', 'rb'))
FACTS = ['catalog_sales', 'catalog_returns', 'inventory', 'store_sales', 'store_returns', 'web_sales', 'web_returns']
small = {}
S = set()
for n in FACTS:
    cols, rows = full[n]
    keep = rows[:NF]
    small[n] = (cols, keep)
    for r in keep:
        S.update(int(v) for v in r if type(v) is int or (type(v) is float and v == int(v)))
for n, (cols, rows) in full.items():
    if n not in FACTS:
        small[n] = (cols, rows[:max(int(os.environ.get('DIMF','1'))*NF, 400)])
print({n: len(r) for n, (c, r) in small.items()}, file=sys.stderr)
sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
tabs = {n: Table(columns=c, rows=list(r)) for n, (c, r) in small.items()}
out = {'tables': small, 'q': {}, 'masks': {}}
tot = 0
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
def run(meta, sql):
    e = parse_one(sql)
    e = pickle.loads(pickle.dumps(e))
    res = execute(e, schema=te.TPCDS_SCHEMA, tables={x.name: tabs[x.name] for x in find_tables(e)})
    duck = transpile(sql, write='duckdb')[0]
    return res, duck
for i, (meta, sql, _) in enumerate(sqls):
    if not meta.get('execute'):
        continue
    try:
        run(meta, sql)
        t = time.process_time()
        res, duck = run(meta, sql)
        dt = time.process_time() - t
        cur = set()
        mon.set_events(5, mon.events.LINE | mon.events.PY_START)
        res2, _d = run(meta, sql)
        mon.set_events(5, 0)
        assert tuple(res2.columns) == tuple(res.columns) and repr(list(res2.rows)) == repr(list(res.rows))
    except Exception as ex:
        mon.set_events(5, 0)
        print(i, 'ERR', repr(ex)[:100], file=sys.stderr)
        continue
    tot += dt
    out['q'][i] = {'columns': tuple(res.columns), 'rows': list(res.rows), 'duck': duck, 'time': round(dt, 4)}
    for f, l in cur:
        d = out['masks'].setdefault(f, {})
        d[l] = d.get(l, 0) | (1 << i)
print('queries', len(out['q']), 'total %.2f' % tot, 'nonempty', sum(1 for v in out['q'].values() if v['rows']),
      'times', sorted(v['time'] for v in out['q'].values())[::6], file=sys.stderr)
blob = zlib.compress(pickle.dumps(out, protocol=5), 9)
open(sys.argv[2] if len(sys.argv) > 2 else '/tmp/small_%d.bin' % NF, 'wb').write(blob)
print('bytes', len(blob), file=sys.stderr)
