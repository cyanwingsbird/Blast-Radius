"""Pack map/meta.bin and map/*.bin into the single map.bin the selector reads."""
import os, pickle, zlib, sys
d = sys.argv[1]; out = sys.argv[2]
meta = open(os.path.join(d, 'meta.bin'), 'rb').read()
m = pickle.loads(zlib.decompress(meta))
blobs = {path: open(os.path.join(d, name), 'rb').read() for path, name in m['files'].items()}
open(out, 'wb').write(pickle.dumps({'meta': meta, 'files': blobs}, protocol=5))
print(os.path.getsize(out))
