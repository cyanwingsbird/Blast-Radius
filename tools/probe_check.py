# coverage of the max_nodes probe vs the real test; probe timing
import sys, os, json, time
ROOT='/home/claude/work/sqlglot'; sys.path.insert(0, ROOT); os.chdir(ROOT); os.environ['SKIP_INTEGRATION']='0'
sys.path.insert(0,'/home/claude/work/repo')
import runner, unittest
k, m = runner._probe_case('PROBE:max_nodes')
pre = ROOT + '/'; seen=set()
mon=sys.monitoring; mon.use_tool_id(5,'q')
def on_line(code, line):
    if code.co_filename.startswith(pre): seen.add((code.co_filename[len(pre):], line))
    return mon.DISABLE
def on_start(code, off):
    if code.co_filename.startswith(pre): seen.add((code.co_filename[len(pre):], code.co_firstlineno))
    return mon.DISABLE
mon.register_callback(5, mon.events.LINE, on_line)
mon.register_callback(5, mon.events.PY_START, on_start)
r = unittest.TestResult(); t=time.process_time()
mon.set_events(5, mon.events.LINE | mon.events.PY_START); k(m).run(r); mon.set_events(5,0)
print('ok', r.wasSuccessful(), r.failures, r.errors, time.process_time()-t)
r = unittest.TestResult(); t=time.process_time(); k(m).run(r); print('uninstrumented', time.process_time()-t)
json.dump(sorted(seen), open('/home/claude/work/maxnodes_probe_lines.json','w'))
