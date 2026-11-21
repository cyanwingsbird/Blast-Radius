"""Build the coverage map from per-test json. usage: buildmap.py COVDIR TESTS_JSON OUTDIR"""
import sys, os, json, glob, zlib, pickle, statistics
cov, tj, out = sys.argv[1:4]
tests = json.load(open(tj))["tests"]
ids = [t["id"] for t in tests]
D = {}; R = {}; opens = {}; setup = {}
for t in tests:
    d = json.load(open(os.path.join(cov, f"{t['number']}.json")))
    # test_generator_memory measures allocations, which the coverage hooks add to;
    # its subtests all still run, so its coverage is complete
    assert d["id"] == t["id"] and (d["ok"] or t["id"].endswith("::test_generator_memory")), d["id"]
    n = t["number"]; bit = 1 << n
    for ph in ("imp", "run", "child"):
        for fn, (dl, rl) in d[ph].items():
            fd = D.setdefault(fn, {}); fr = R.setdefault(fn, {})
            for l in dl: fd[l] = fd.get(l, 0) | bit
            for l in rl: fr[l] = fr.get(l, 0) | bit
    for o in d["opens"]: opens[o] = opens.get(o, 0) | bit
    cls = t["id"].rsplit("::", 1)[0]
    setup.setdefault(cls, []).append((d["t_run"] - t["seconds"], d["t_imp"], d["nchild"]))
os.makedirs(out, exist_ok=True)
index = {}
for fn in sorted(set(D) | set(R)):
    uniq = {}; lines = {}
    def idx(v):
        if v not in uniq: uniq[v] = len(uniq)
        return uniq[v]
    for l in sorted(set(D.get(fn, {})) | set(R.get(fn, {}))):
        lines[l] = (idx(D.get(fn, {}).get(l, 0)), idx(R.get(fn, {}).get(l, 0)))
    anyd = 0
    for v in D.get(fn, {}).values(): anyd |= v
    for v in R.get(fn, {}).values(): anyd |= v
    blob = zlib.compress(pickle.dumps((list(uniq), lines, anyd), protocol=5), 9)
    name = fn.replace("/", "__") + ".bin"
    open(os.path.join(out, name), "wb").write(blob)
    index[fn] = name
cls_info = {}
for cls, v in setup.items():
    cls_info[cls] = (round(max(0.0, statistics.median(x[0] for x in v)), 3), round(statistics.median(x[1] for x in v), 3), max(x[2] for x in v))
meta = {"ids": ids, "files": index, "opens": opens, "cls": cls_info}
open(os.path.join(out, "meta.bin"), "wb").write(zlib.compress(pickle.dumps(meta, protocol=5), 9))
print(len(index), sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out)))
