"""Rewrite callmap.bin and casemap.bin so that each test's record is packed on its own:
the selector then unpacks only the records of the tests a patch can reach.

usage: lazymap.py callmap.bin casemap.bin   (rewrites in place; old-format input)"""
import pickle
import sys
import zlib

MAGIC = b"LZ1"


def pack(obj):
    return zlib.compress(pickle.dumps(obj, protocol=5), 6)


def main(callmap, casemap):
    with open(callmap, "rb") as f:
        data = f.read()
    if not data.startswith(MAGIC):
        cm = pickle.loads(zlib.decompress(data))
        with open(callmap, "wb") as f:
            f.write(MAGIC + pickle.dumps({"tests": {tid: pack(v) for tid, v in cm.items()}}, protocol=5))
    with open(casemap, "rb") as f:
        data = f.read()
    if not data.startswith(MAGIC):
        cm = pickle.loads(zlib.decompress(data))
        out = {k: v for k, v in cm.items() if k != "tests"}
        out["tests"] = {tid: pack(v) for tid, v in cm["tests"].items()}
        with open(casemap, "wb") as f:
            f.write(MAGIC + pickle.dumps(out, protocol=5))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
