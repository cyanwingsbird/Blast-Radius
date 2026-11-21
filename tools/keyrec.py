"""Record, per test, the keys looked up in sqlglot's class- and module-level
dicts and sets.

  keyrec.py instrument SRC DST        copy SRC to DST with recording sites
  keyrec.py collect DST OUT.pickle    run every test (forked) and record
  keyrec.py build OUT.pickle KEYMAP   write the key map for the selector

Sites are the ones narrow.scan finds at call time: keyed reads (d[k], d.get(k),
k in d, also through a local alias) get __br_rec__(NAME, k); a dict or set
passed as an argument gets __br_wrap__(NAME, value).  Lines do not move."""
import ast, os, sys, shutil, json, pickle, zlib, time
sys.path.insert(0, '/home/claude/work/repo')


def instrument(src, dst):
    import narrow
    narrow.INDEX_FILE = '/nonexistent'
    S = narrow.Source(src)
    names = set()
    for p in S.paths:
        for node in ast.walk(S.tree(p)):
            n = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if n and n.upper() == n and any(c.isalpha() for c in n):
                names.add(n)
    for a in narrow.ALIASES.values():
        names.update(a)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '.git'))
    nsites = 0
    for p in S.paths:
        text = S.text(p)
        tree = S.tree(p)
        ins = []        # (line, col_bytes, order, text)
        spans = []
        for node, name, kind, alias, is_attr, key in narrow.scan(tree, names):
            if narrow._enclosing_func_node(node) is None:
                continue
            if kind == 'keyed' and key is not None:
                tgt = key
                pre = '__br_rec__(%r, ' % name
            elif kind == 'callarg':
                tgt = node
                pre = '__br_wrap__(%r, ' % name
            else:
                continue
            a = (tgt.lineno, tgt.col_offset)
            b = (tgt.end_lineno, tgt.end_col_offset)
            spans.append((a, b, pre))
        # nested or touching spans: apply outermost last at equal starts
        spans.sort(key=lambda s: (s[0], (-s[1][0], -s[1][1])))
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                (a1, b1, _), (a2, b2, _) = spans[i], spans[j]
                if a2 < b1 and not (b2 <= b1):
                    raise SystemExit('overlapping sites in %s at %s/%s' % (p, a1, a2))
        lines = [l.encode('utf-8') for l in text.split('\n')]
        # apply from the end of the file backwards; for equal positions, closings first
        ops = []
        for k, (a, b, pre) in enumerate(spans):
            ops.append((b[0], b[1], 0, k, b')'))
            ops.append((a[0], a[1], 1, k, pre.encode()))
        ops.sort(reverse=True)
        for ln, col, _o, _k, txt in ops:
            l = lines[ln - 1]
            lines[ln - 1] = l[:col] + txt + l[col:]
        new = b'\n'.join(lines).decode('utf-8')
        compile(new, p, 'exec')
        assert new.count('\n') == text.count('\n')
        with open(os.path.join(dst, p), 'w', encoding='utf-8') as f:
            f.write(new)
        nsites += len(spans)
    json.dump(sorted(names), open(os.path.join(dst, 'br_names.json'), 'w'))
    print('instrumented', nsites, 'sites', file=sys.stderr)


def collect(dst, out, shard=0, nsh=1):
    os.environ['SQLGLOT_ROOT'] = dst
    os.environ['SKIP_INTEGRATION'] = '0'
    sys.path.insert(0, '/home/claude/work/tools')
    import brrec
    sys.path.insert(0, dst)
    os.chdir(dst)
    import runner, importlib, io, unittest
    T = json.load(open('/home/claude/sample/sample/tests.json'))['tests']
    for d, _ds, fs in os.walk(os.path.join(dst, 'sqlglot')):
        for f in sorted(fs):
            if f.endswith('.py') and f != '__main__.py':
                try:
                    importlib.import_module(runner.modname(os.path.relpath(os.path.join(d, f), dst)))
                except Exception as e:
                    print('import', f, e, file=sys.stderr)
    for f in sorted({t['file'] for t in T}):
        try:
            importlib.import_module(f[:-3].replace('/', '.'))
        except Exception as e:
            print('import', f, e, file=sys.stderr)
    runner.install_sync_pools()
    res = {}
    todo = [t for t in T][shard::nsh]
    t0 = time.time()
    for t in todo:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1); os.dup2(devnull, 2)
            sys.stdout = io.StringIO(); sys.stderr = io.StringIO()
            ok = False
            try:
                brrec.REC.clear()
                cls, meth = runner.load_case(t['id'])
                tr = unittest.TestResult()
                unittest.TestSuite([cls(meth)]).run(tr)
                ok = tr.wasSuccessful() and not tr.skipped
                skipped = bool(tr.skipped)
            except BaseException:
                skipped = False
            data = pickle.dumps((ok, skipped, sorted(brrec.REC)))
            v = memoryview(data)
            while v:
                n = os.write(w, v)
                v = v[n:]
            os._exit(0)
        os.close(w)
        buf = []
        while True:
            c = os.read(r, 1 << 20)
            if not c:
                break
            buf.append(c)
        os.close(r)
        os.waitpid(pid, 0)
        try:
            res[t['id']] = pickle.loads(b''.join(buf))
        except Exception:
            res[t['id']] = (False, False, [])
    pickle.dump(res, open(out, 'wb'))
    print('collected', len(res), 'in %.0fs' % (time.time() - t0), file=sys.stderr)


def build(outs, names_file, keymap_path):
    sys.path.insert(0, '/home/claude/work/repo')
    import analyze
    mp = analyze.Map('/home/claude/work/repo/map.bin')
    idx = {t: i for i, t in enumerate(mp.ids)}
    res = {}
    for o in outs:
        res.update(pickle.load(open(o, 'rb')))
    keys = {}
    unrec = 0
    nfail = 0
    for tid, (ok, skipped, recs) in res.items():
        i = idx.get(tid)
        if i is None:
            continue
        if not ok and not skipped:
            unrec |= 1 << i
            nfail += 1
            continue
        b = 1 << i
        for name, k in recs:
            d = keys.setdefault(name, {})
            d[k] = d.get(k, 0) | b
    for tid, i in idx.items():
        if tid not in res:
            unrec |= 1 << i
    names = set(json.load(open(names_file)))
    data = {"names": names, "keys": keys, "unrecorded": unrec}
    blob = zlib.compress(pickle.dumps(data, protocol=5), 9)
    open(keymap_path, 'wb').write(blob)
    print('tests failing under recording:', nfail, 'names', len(keys),
          'entries', sum(len(v) for v in keys.values()), 'bytes', len(blob), file=sys.stderr)


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'instrument':
        instrument(sys.argv[2], sys.argv[3])
    elif cmd == 'collect':
        sh = (int(sys.argv[4]), int(sys.argv[5])) if len(sys.argv) > 5 else (0, 1)
        collect(sys.argv[2], sys.argv[3], *sh)
    elif cmd == 'build':
        build(sys.argv[2].split(','), sys.argv[3], sys.argv[4])
