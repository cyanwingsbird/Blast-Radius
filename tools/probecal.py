import sys, os, json, time
sys.path.insert(0, '/home/claude/work/repo')
os.environ['SQLGLOT_ROOT'] = ROOT = '/home/claude/work/sqlglot'; os.environ['SKIP_INTEGRATION'] = '0'
import runner, unittest
warm = runner.Warm({'tests/test_executor.py'})
runner._use_exec_cache(warm.exec_cache)
import tests.test_executor as te
K = te.TestExecutor
t = time.process_time(); K.setUpClass(); print('setup', time.process_time() - t, file=sys.stderr)
n = len(K.tpcds_sqls); cost = []
for i in range(n):
    k, m = runner._probe_case('PROBE:tpcds:%d' % i)
    r = unittest.TestResult(); t = time.process_time(); k(m).run(r)
    cost.append(round(time.process_time() - t, 4))
    assert r.wasSuccessful(), i
print('total', sum(cost), file=sys.stderr)
json.dump(cost, open('/home/claude/work/tpcds_qcost.json', 'w'))
