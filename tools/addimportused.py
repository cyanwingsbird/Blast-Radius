"""Store the classes instantiated while modules import (with their ancestors) in map.bin."""
import pickle, zlib, json, sys
path = sys.argv[1]
packed = pickle.load(open(path, 'rb'))
meta = pickle.loads(zlib.decompress(packed['meta']))
meta['import_used'] = sorted(json.load(open(sys.argv[2])))
packed['meta'] = zlib.compress(pickle.dumps(meta, protocol=5), 9)
open(path, 'wb').write(pickle.dumps(packed, protocol=5))
print(len(meta['import_used']))
