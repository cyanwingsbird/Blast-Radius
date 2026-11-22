"""Record, per test, the kinds of operand pairs each comparison in sqlglot sees.

  cmprec.py instrument SRC DST            copy SRC to DST with recording sites
  cmprec.py collect DST OUT.pickle [i n]  run every test (forked) and record
  cmprec.py build OUT1,OUT2 DST CMPMAP    write the comparison map for the selector

Every one-operator comparison (is, is not, ==, !=, <, <=, >, >=) inside a function
becomes __br_cmp__(site, left, op, right), which answers as the operator does."""
import ast, os, sys, shutil, json, pickle, zlib, time
sys.path.insert(0, '/home/claude/work/repo')

OPS = [ast.Is, ast.IsNot, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE]


class Tr(ast.NodeTransformer):
    def __init__(self, path, sites):
        self.path = path
        self.sites = sites
        self.depth = 0

    def _func(self, node):
        # decorators, defaults and annotations run in the enclosing scope
        self.depth += 1
        node.body = [self.visit(s) for s in node.body] if isinstance(node.body, list) else self.visit(node.body)
        self.depth -= 1
        return node

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func
    visit_Lambda = _func

    def visit_Compare(self, node):
        self.generic_visit(node)
        if self.depth and len(node.ops) == 1 and type(node.ops[0]) in OPS:
            op = OPS.index(type(node.ops[0]))
            sid = len(self.sites)
            self.sites.append((self.path, node.lineno, node.col_offset, op))
            call = ast.Call(func=ast.Name(id='__br_cmp__', ctx=ast.Load()),
                            args=[ast.Constant(sid), node.left, ast.Constant(op), node.comparators[0]], keywords=[])
            return ast.copy_location(call, node)
        return node


def instrument(src, dst):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '.git'))
    sites = []
    for d, _ds, fs in os.walk(os.path.join(src, 'sqlglot')):
        for f in sorted(fs):
            if not f.endswith('.py'):
                continue
            p = os.path.relpath(os.path.join(d, f), src)
            text = open(os.path.join(src, p), encoding='utf-8').read()
            tree = ast.parse(text)
            n0 = len(sites)
            tree = Tr(p, sites).visit(tree)
            if len(sites) == n0:
                continue
            ast.fix_missing_locations(tree)
            new = ast.unparse(tree)
            compile(new, p, 'exec')
            with open(os.path.join(dst, p), 'w', encoding='utf-8') as fo:
                fo.write(new)
    json.dump(sites, open(os.path.join(dst, 'br_cmp_sites.json'), 'w'))
    print('instrumented', len(sites), 'sites', file=sys.stderr)


def collect(dst, out, shard=0, nsh=1):
    os.environ['SQLGLOT_ROOT'] = dst
    os.environ['SKIP_INTEGRATION'] = '0'
    sys.path.insert(0, '/home/claude/work/tools')
    import cmprt
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
    res = {'__global__': sorted(cmprt.REC)}
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
            skipped = False
            try:
                cmprt.REC.clear()
                cls, meth = runner.load_case(t['id'])
                tr = unittest.TestResult()
                unittest.TestSuite([cls(meth)]).run(tr)
                ok = tr.wasSuccessful() and not tr.skipped
                skipped = bool(tr.skipped)
            except BaseException:
                skipped = False
            data = pickle.dumps((ok, skipped, sorted(cmprt.REC)))
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


def build(outs, dst, path):
    import analyze
    mp = analyze.Map('/home/claude/work/repo/map.bin')
    idx = {t: i for i, t in enumerate(mp.ids)}
    sites = json.load(open(os.path.join(dst, 'br_cmp_sites.json')))
    res = {}
    glob = set()
    for o in outs:
        d = pickle.load(open(o, 'rb'))
        glob |= set(map(tuple, d.pop('__global__')))
        res.update(d)
    per = {}          # sid -> {cls: bits}
    unrec = 0
    nfail = 0
    for tid, (ok, skipped, recs) in res.items():
        i = idx.get(tid)
        if i is None:
            continue
        if not ok and not skipped:
            unrec |= 1 << i
            nfail += 1
            print('unrecorded', tid, file=sys.stderr)
            continue
        b = 1 << i
        for sid, c in recs:
            d = per.setdefault(sid, {})
            d[c] = d.get(c, 0) | b
    for tid, i in idx.items():
        if tid not in res:
            unrec |= 1 << i
    gl = {}
    for sid, c in glob:
        gl.setdefault(sid, set()).add(c)
    out = {}
    for sid, (p, ln, col, op) in enumerate(sites):
        out.setdefault(p, {})[(ln, col)] = (op, per.get(sid, {}), sorted(gl.get(sid, ())))
    data = {"sites": out, "unrecorded": unrec}
    blob = zlib.compress(pickle.dumps(data, protocol=5), 9)
    open(path, 'wb').write(blob)
    print('tests failing under recording:', nfail, 'sites', len(sites), 'seen', len(per), 'bytes', len(blob), file=sys.stderr)


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'instrument':
        instrument(sys.argv[2], sys.argv[3])
    elif cmd == 'collect':
        sh = (int(sys.argv[4]), int(sys.argv[5])) if len(sys.argv) > 5 else (0, 1)
        collect(sys.argv[2], sys.argv[3], *sh)
    elif cmd == 'build':
        build(sys.argv[2].split(','), sys.argv[3], sys.argv[4])
