import json, os, pickle, zlib, sys, glob
mapdir = sys.argv[1]
TYPES = sys.argv[2] if len(sys.argv) > 2 else '/home/claude/work/types'
FAMILY = sys.argv[3] if len(sys.argv) > 3 else '/home/claude/work/family.json'
meta = pickle.loads(zlib.decompress(open(os.path.join(mapdir, 'meta.bin'), 'rb').read()))
ids = meta['ids']; num = {i: n for n, i in enumerate(ids)}
users = {}
bad = []
for f in glob.glob(os.path.join(TYPES, '*.json')):
    d = json.load(open(f))
    if not d['ok']: bad.append(d['id'])
    bit = 1 << num[d['id']]
    for k in d['types']: users[k] = users.get(k, 0) | bit
assert len(glob.glob(os.path.join(TYPES, '*.json'))) == len(ids), 'incomplete'
print('not ok', bad)
fam = json.load(open(FAMILY))
cls_bits = {}
for k, members in fam.items():
    b = 0
    for m in members: b |= users.get(m, 0)
    cls_bits[k] = b
meta['cls_bits'] = cls_bits
open(os.path.join(mapdir, 'meta.bin'), 'wb').write(zlib.compress(pickle.dumps(meta, protocol=5), 9))
print(len(cls_bits), os.path.getsize(os.path.join(mapdir, 'meta.bin')))
