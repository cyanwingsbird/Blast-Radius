"""Which tests can a patch reach?

Works from a map built at image-build time from the pinned commit: for every
source line, the tests that executed it (split into definition-time code -
module and class bodies - and call-time code - function bodies), and for
every data file, the tests that opened it.  Nothing here knows about any
particular patch.
"""

import ast
import importlib.util
import marshal
import os
import pickle
import re
import textwrap
import zlib

ALL = "ALL"


# ----------------------------------------------------------------- patches

class FilePatch:
    __slots__ = ("old", "new", "hunks", "binary", "rename")

    def __init__(self):
        self.old = None
        self.new = None
        self.hunks = []
        self.binary = False
        self.rename = False


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _path(s):
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    s = s.split("\t")[0]
    if s == "/dev/null":
        return None
    if s.startswith(("a/", "b/")):
        s = s[2:]
    return s


def parse_patch(text):
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$', line)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            i += 1
            continue
        if cur is None:
            if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
                cur = FilePatch()
                files.append(cur)
            else:
                i += 1
                continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur.hunks:
                cur = FilePatch()
                files.append(cur)
            cur.old = _path(line[4:])
            cur.new = _path(lines[i + 1][4:])
            i += 2
            continue
        if line.startswith("new file mode"):
            cur.old = None
        elif line.startswith("deleted file mode"):
            cur.new = None
        elif line.startswith(("rename from", "rename to", "copy from", "copy to")):
            cur.rename = True
        elif line.startswith(("old mode", "new mode")):
            pass          # a permission change alone does not change what runs
        elif line.startswith(("Binary files", "GIT binary patch")):
            cur.binary = True
        m = _HUNK.match(line)
        if m:
            ol = int(m.group(2)) if m.group(2) is not None else 1
            nl = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            seen_o = seen_n = 0
            while i < n and (seen_o < ol or seen_n < nl):
                h = lines[i]
                if h.startswith("\\"):
                    body.append(h)
                    i += 1
                    continue
                c = h[:1]
                if c == "+":
                    seen_n += 1
                elif c == "-":
                    seen_o += 1
                else:
                    seen_o += 1
                    seen_n += 1
                    if c != " ":
                        h = " " + h
                body.append(h)
                i += 1
            while i < n and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            cur.hunks.append((int(m.group(1)), ol, int(m.group(3)), nl, body))
            continue
        i += 1
    return files


def apply_hunks(old_text, hunks):
    """Return (new_text, changes); changes are (first_old, last_old, n_added)
    with first_old > last_old for a pure insertion before line first_old."""
    old = old_text.split("\n")
    out = []
    changes = []
    pos = 0
    for ostart, olen, _ns, _nl, body in hunks:
        start = ostart - 1 if olen else ostart
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old[pos:start])
        pos = start
        run = None
        for h in body:
            if h.startswith("\\"):
                continue
            c, txt = h[:1], h[1:]
            if c == " ":
                if pos >= len(old) or old[pos] != txt:
                    raise ValueError("context mismatch")
                out.append(txt)
                pos += 1
                if run:
                    changes.append(tuple(run))
                    run = None
            elif c == "-":
                if pos >= len(old) or old[pos] != txt:
                    raise ValueError("removal mismatch")
                if run is None:
                    run = [pos + 1, pos, 0]
                run[1] = pos + 1
                pos += 1
            else:
                if run is None:
                    run = [pos + 1, pos, 0]
                run[2] += 1
                out.append(txt)
        if run:
            changes.append(tuple(run))
    out.extend(old[pos:])
    return "\n".join(out), changes


# --------------------------------------------------------------------- map

class Map:
    def __init__(self, mapfile):
        with open(mapfile, "rb") as f:
            packed = pickle.load(f)
        self._blobs = packed["files"]
        meta = pickle.loads(zlib.decompress(packed["meta"]))
        self.ids = meta["ids"]
        self.files = meta["files"]
        self.opens = meta["opens"]
        self.cls = meta["cls"]
        self.cls_bits = meta.get("cls_bits", {})
        self._cache = {}

    def file(self, path):
        """(lines: {line: (def_bits, call_bits)}, any_bits) or None."""
        if path not in self._blobs:
            return None
        got = self._cache.get(path)
        if got is None:
            raw = self._blobs[path]
            uniq, lines, anyb = pickle.loads(zlib.decompress(raw))
            got = ({ln: (uniq[d], uniq[r]) for ln, (d, r) in lines.items()}, anyb)
            self._cache[path] = got
        return got


# ------------------------------------------------------------ code objects

CodeType = type(compile("0", "x", "eval"))


def _norm(c):
    if isinstance(c, CodeType):
        return ("<code>", c.co_name)
    if isinstance(c, tuple):
        return tuple(_norm(x) for x in c)
    if isinstance(c, frozenset):
        return frozenset(_norm(x) for x in c)
    return (type(c).__name__, c)


def _consts(c):
    out = []
    fl = "__firstlineno__" in c.co_names
    for x in c.co_consts:
        if fl and type(x) is int and x == c.co_firstlineno:
            out.append(("<firstlineno>",))
        else:
            out.append(_norm(x))
    return tuple(out)


def _sig(c):
    return (c.co_code, c.co_names, c.co_varnames, c.co_freevars, c.co_cellvars, c.co_argcount,
            c.co_posonlyargcount, c.co_kwonlyargcount, c.co_flags, c.co_name, c.co_qualname,
            c.co_exceptiontable, _consts(c))


def _kids(c):
    return [x for x in c.co_consts if isinstance(x, CodeType)]


def changed_codes(old, new, out):
    """Old code objects whose own instructions or constants differ."""
    ko, kn = _kids(old), _kids(new)
    if _sig(old) != _sig(new) or len(ko) != len(kn):
        out.append(old)
        if len(ko) != len(kn):
            return
    for a, b in zip(ko, kn):
        changed_codes(a, b, out)


def own_lines(c):
    return {ln for _s, _e, ln in c.co_lines() if ln is not None}


def all_lines(c):
    s = own_lines(c)
    for k in _kids(c):
        s |= all_lines(k)
    return s


def load_old_code(root, path, src):
    full = os.path.join(root, path)
    try:
        pyc = importlib.util.cache_from_source(full)
        with open(pyc, "rb") as f:
            data = f.read()
        return marshal.loads(data[16:])
    except Exception:
        return compile(src, full, "exec", dont_inherit=True)


# ---------------------------------------------------------- statement span

_COMPOUND = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.FunctionDef, ast.ClassDef,
             ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith, ast.Match) + (
                 (ast.TryStar,) if hasattr(ast, "TryStar") else ())


def _stmt_chain(tree, line):
    """Statements containing `line`, outermost first."""
    chain = []
    node = tree
    while True:
        nxt = None
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                if child.lineno <= line <= (child.end_lineno or child.lineno):
                    nxt = child
                    break
                decos = getattr(child, "decorator_list", None)
                if decos and decos[0].lineno <= line <= (child.end_lineno or child.lineno):
                    nxt = child
                    break
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                # look through to the statements inside
                for sub in ast.walk(child):
                    if isinstance(sub, ast.stmt) and sub.lineno <= line <= (sub.end_lineno or sub.lineno):
                        nxt = child
                        break
                if nxt is not None:
                    break
        if nxt is None:
            return chain
        if isinstance(nxt, ast.stmt):
            chain.append(nxt)
        node = nxt


def _span(stmt, line):
    """(lines, is_docstring) of the part of `stmt` that `line` belongs to."""
    end = stmt.end_lineno or stmt.lineno
    if isinstance(stmt, _COMPOUND):
        body = getattr(stmt, "body", None)
        if body:
            first_body = body[0].lineno
            decos = getattr(stmt, "decorator_list", None)
            start = decos[0].lineno if decos else stmt.lineno
            if start <= line < first_body or (line == stmt.lineno == first_body):
                return range(start, max(first_body, start + 1)), False
        return range(stmt.lineno, end + 1), False
    doc = isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)
    return range(stmt.lineno, end + 1), doc


# ---------------------------------------------------------------- analysis

class Reach:
    """Result for one patch: a bitset over map test numbers, or everything."""

    def __init__(self):
        self.bits = 0
        self.everything = False
        self.notes = []
        self.py = []       # (path, new_source, new_code or None) to overlay
        self.data = []     # (path, new_text or None) to overlay
        self.runnable = True
        self.lines = {}    # path -> set of old line numbers of changed statements
        self.classes = set()   # "module:qualname" of class bodies that changed
        self.coarse = False    # a change only the whole-module view covers
        self.new_files = []
        self.blind_bits = 0    # tests that must be scheduled without verification

    def add(self, bits, note):
        self.bits |= bits
        self.notes.append(note)

    def all(self, note):
        self.everything = True
        self.notes.append("ALL:" + note)


def _nearest(lines_sorted, a, b):
    """Lines in [a, b] plus the nearest one at or before a and at or after b."""
    out = [l for l in lines_sorted if a <= l <= b]
    before = [l for l in lines_sorted if l < a]
    after = [l for l in lines_sorted if l > b]
    if before:
        out.append(before[-1])
    if after:
        out.append(after[0])
    return out


def _func_candidates(code, src_lines, changes, cov, out_lines):
    """Tests that execute the changed statements of one function."""
    lines_all = all_lines(code)
    first = code.co_firstlineno
    last = max(lines_all) if lines_all else first
    mine = [c for c in changes if not (c[1] < first - 1 or c[0] > last + 1)]
    if not mine:
        mine = None
    tree = None
    try:
        seg = textwrap.dedent("\n".join(src_lines[first - 1:last]))
        tree = ast.parse(seg)
    except Exception:
        tree = None
    off = first - 1

    def bits_of(lines):
        b = 0
        for ln in lines:
            e = cov.get(ln)
            if e:
                b |= e[1]
        return b

    whole = bits_of(range(first, last + 1))
    if tree is None or mine is None:
        out_lines.update(range(first, last + 1))
        return whole, "func-whole"
    total = 0
    for a, b, _added in mine:
        if a > b:
            probe = [a - 1, a]          # insertion between a-1 and a
        else:
            probe = list(range(a, b + 1))
        for ln in probe:
            rel = ln - off
            chain = _stmt_chain(tree, rel)
            # drop the function definition itself when the line is in its body
            got = None
            while chain:
                stmt = chain[-1]
                if len(chain) == 1:
                    break                # the function itself: use all of it
                span, doc = _span(stmt, rel)
                if doc and a <= b:
                    got = 0
                    break
                lines = [s + off for s in span]
                bts = bits_of(lines)
                if bts or (set(lines) & lines_all):
                    got = bts
                    out_lines.update(lines)
                    break
                chain.pop()
            if got is None:
                out_lines.update(range(first, last + 1))
                return whole, "func-whole"
            total |= got
    return total, "stmt"


def analyze(patch_text, root, mp):
    """Return a Reach for the patch against the checkout at `root`."""
    r = Reach()
    try:
        files = parse_patch(patch_text)
    except Exception as e:
        r.all("unparsed patch %r" % (e,))
        r.runnable = False
        return r
    if not files and patch_text.strip():
        r.all("no files in patch")
        r.runnable = False
        return r
    for fp in files:
        path = fp.old or fp.new
        try:
            _one(fp, path, root, mp, r)
        except Exception as e:  # anything unexpected: be safe
            r.all("error on %s: %r" % (path, e))
            r.runnable = False
    return r


_CONFIG = ("conftest.py", "pytest.ini", "tox.ini")


def _one(fp, path, root, mp, r):
    if fp.binary and not fp.rename and path and not path.endswith(".py") and not path.startswith("sqlglot/"):
        # a binary data file: only the tests that open it can see the change
        r.add(mp.opens.get(path, 0), "binary " + path)
        r.blind_bits |= mp.opens.get(path, 0)
        return
    if fp.binary or fp.rename or (fp.old and fp.new and fp.old != fp.new):
        r.all("binary/rename " + str(path))
        r.runnable = False
        return
    base = os.path.basename(path)
    is_py = path.endswith(".py")
    opens = mp.opens.get(path, 0)
    if fp.old is None:                                   # new file
        try:
            new_text, _ = apply_hunks("", fp.hunks)
        except Exception:
            new_text = None
        if base in _CONFIG or new_text is None or os.path.exists(os.path.join(root, path)):
            r.all("new file " + path)
            r.runnable = False
            return
        if is_py and path.startswith(("sqlglot/", "tests/")):
            # nothing imports a new module unless other changed code does, and that code
            # brings its own tests; the module is served to the verification run
            r.py.append((path, new_text, None))
            r.new_files.append(path)
            d = os.path.dirname(path) + "/"
            scanned = 0
            for p2, bits in mp.opens.items():
                if d.startswith("sqlglot/") and p2.startswith(d) and p2.endswith(".py") and "/" not in p2[len(d):]:
                    scanned |= bits      # tests that read this package's sources list its directory
            if scanned:
                r.add(scanned, "lists " + d)
                r.blind_bits |= scanned
        else:
            r.notes.append("new inert file " + path)
        return
    full = os.path.join(root, path)
    if not os.path.exists(full):
        r.all("patch to unknown file " + path)
        r.runnable = False
        return
    info = mp.file(path) if is_py else None
    if fp.new is None:                                   # deleted
        r.add(opens | (info[1] if info else 0), "deleted " + path)
        if base in _CONFIG:
            r.all("deleted config")
        r.runnable = False
        return
    raw = open(full, "rb").read()
    try:
        old_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        r.add(opens, "binary data " + path)
        r.runnable = False
        return
    new_text, changes = apply_hunks(old_text, fp.hunks)
    if new_text == old_text:
        return
    if is_py and opens:
        # some tests read this source file as text: they must see the patch
        r.data.append((path, new_text))
    if base in _CONFIG or (base in ("setup.cfg", "pyproject.toml") and "pytest" in new_text):
        if base in _CONFIG or _pytest_section_changed(old_text, new_text):
            r.all("pytest config " + path)
            r.runnable = False
            return
    if not is_py:
        r.data.append((path, new_text))
        r.add(opens, "data " + path)
        return
    if info is None:
        # no test ever executed a line of this file
        r.py.append((path, new_text, None))
        r.add(opens, "unexecuted " + path)
        return
    cov, anyb = info
    try:
        new_code = compile(new_text, full, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        r.py.append((path, new_text, None))
        r.coarse = True
        r.add(anyb | opens, "does not compile " + path)
        return
    r.py.append((path, new_text, new_code))
    old_code = load_old_code(root, path, old_text)
    changed = []
    changed_codes(old_code, new_code, changed)
    if not changed:
        r.add(opens, "same code " + path)
        return
    r.bits |= opens
    src_lines = old_text.split("\n")
    for c in changed:
        if c.co_flags & 1:                               # function body
            bits, how = _func_candidates(c, src_lines, changes, cov, r.lines.setdefault(path, set()))
            r.add(bits, "%s %s:%s" % (how, path, c.co_qualname))
        else:                                            # module or class body
            own = sorted(own_lines(c))
            lines = []
            for a, b, _added in changes:
                lo, hi = (a - 1, a) if a > b else (a, b)
                lines += _nearest(own, lo, hi)
            if not lines:
                lines = own
            r.lines.setdefault(path, set()).update(lines)
            bits = 0
            for ln in lines:
                e = cov.get(ln)
                if e:
                    bits |= e[0]
            key = "%s:%s" % (_modname(path), c.co_qualname)
            if c.co_name != "<module>" and key in mp.cls_bits:
                r.add(bits & mp.cls_bits[key], "class %s" % key)
                r.classes.add(key)
                continue
            r.coarse = True
            if not bits:
                # definition-time code no test reached line by line: fall back
                # to everything that loads the file
                for ln in own:
                    e = cov.get(ln)
                    if e:
                        bits |= e[0]
            r.add(bits, "def-time %s:%s" % (path, c.co_qualname))


def _modname(rel):
    m = rel[:-3].replace("/", ".")
    if m.endswith(".__init__"):
        m = m[: -len(".__init__")]
    return m


def _pytest_section_changed(old, new):
    def sect(t):
        keep = []
        on = False
        for line in t.split("\n"):
            s = line.strip()
            if s.startswith("["):
                on = "pytest" in s
            if on:
                keep.append(line)
        return keep
    return sect(old) != sect(new)
