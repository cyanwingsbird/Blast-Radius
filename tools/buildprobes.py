"""probes.bin: per-query line masks and costs for test_execute_tpcds, and the lines the max_nodes probe runs."""
import json, pickle, zlib
q = json.load(open('/home/claude/work/tpcds_q.json'))
cost = json.load(open('/home/claude/work/tpcds_qcost.json'))
BASE = 0.11
masks = {}
for i, lines in enumerate(q['lines']):
    for f, l in lines:
        masks.setdefault(f, {}).setdefault(l, 0)
        masks[f][l] |= 1 << i
probes = {
    'tpcds': {'times': [round(max(0.01, c - BASE), 4) for c in cost], 'base': 0.15, 'execute': q['execute'], 'masks': masks},
    'max_nodes': {'lines': [tuple(x) for x in json.load(open('/home/claude/work/maxnodes_probe_lines.json'))]},
}
open('/home/claude/work/repo/probes.bin', 'wb').write(zlib.compress(pickle.dumps(probes, protocol=5), 9))
print('ok')
