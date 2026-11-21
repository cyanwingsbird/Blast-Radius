"""Which tests can see a change in the data sqlglot builds while it imports.

The child that re-imported the patched modules reports (datadiff) which
module- or class-level values differ.  A test can only notice such a value

  * through source code that names it (an attribute or a name), run while
    the test runs (call-time coverage of the line), or
  * through code that looks attributes up by computed names (getattr with a
    computed name, vars(), __dict__, dir()), or
  * for an entry of a dict keyed by a class: by looking that class up, which
    needs an instance of it (the per-test class usage map), or through code
    that reads the dict other than key by key.
  * for a function: by running it (call-time coverage of its lines).

Anything else gives up (None): the whole static reach stays."""

import ast
import os
import re

KEYED_METHODS = {"get", "pop", "setdefault", "__getitem__", "__contains__", "__delitem__"}
# reads of a class-keyed dict through another name: what the copy is stored as
ALIASES = {"TRANSFORMS": ("_dispatch",), "EXPRESSION_METADATA": ("expression_metadata",)}
# functions that copy a dict into a dispatch table, and read attributes by computed names
# only for names with this suffix
SUFFIX_FUNCS = {"_build_dispatch": "_sql", "_build_gen_dispatch": "_sql"}
COPY_FUNCS = {"_build_dispatch"}
# getattr on a module object: reads module globals only
MODULE_GETATTR = {("sqlglot/dialects/__init__.py", "__getattr__"), ("sqlglot/optimizer/__init__.py", "__getattr__"),
                  ("sqlglot/serde.py", "_load")}


INDEX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "narrow.idx")


class Source:
    def __init__(self, root):
        self.root = root
        self.paths = []
        for top in ("sqlglot", "tests"):      # test code reads sqlglot's data too
            for d, _dirs, files in os.walk(os.path.join(root, top)):
                for f in files:
                    if f.endswith(".py"):
                        self.paths.append(os.path.relpath(os.path.join(d, f), root))
        self.paths.sort()
        self._text = {}
        self._tree = {}
        self._dyn = None
        self._occ = {}
        self._load_index()

    def _load_index(self):
        try:
            import pickle
            import zlib
            with open(INDEX_FILE, "rb") as f:
                idx = pickle.loads(zlib.decompress(f.read()))
            if idx.get("root") != os.path.realpath(self.root) or idx.get("paths") != self.paths:
                return
            self._dyn = idx["dyn"]
            self._occ = idx["occ"]
        except Exception:
            pass

    def save_index(self, path):
        import pickle
        import zlib
        names = set()
        for p in self.paths:
            for node in ast.walk(self.tree(p)):
                n = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
                if n and n.upper() == n and any(c.isalpha() for c in n):
                    names.add(n)
        for alias in ALIASES.values():
            names.update(alias)
        occ = {}
        for p in self.paths:
            for node, name, kind, alias, is_attr, _k in scan(self.tree(p), names):
                lines = (node.lineno, node.end_lineno or node.lineno)
                occ.setdefault(name, []).append((p, lines, kind, alias, is_attr))
        data = {"root": os.path.realpath(self.root), "paths": self.paths, "dyn": self.dyn(), "occ": occ}
        with open(path, "wb") as f:
            f.write(zlib.compress(pickle.dumps(data, protocol=5), 6))

    def text(self, path):
        t = self._text.get(path)
        if t is None:
            with open(os.path.join(self.root, path), encoding="utf-8") as f:
                t = f.read()
            self._text[path] = t
        return t

    def tree(self, path):
        t = self._tree.get(path)
        if t is None:
            t = ast.parse(self.text(path))
            for node in ast.walk(t):
                for child in ast.iter_child_nodes(node):
                    child._br_parent = node
            self._tree[path] = t
        return t

    def token_lines(self, tok):
        """{path: set(lines)} for lines where `tok` appears as a word."""
        rx = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(tok) + r"(?![A-Za-z0-9_])")
        out = {}
        for p in self.paths:
            t = self.text(p)
            if tok not in t:
                continue
            for i, line in enumerate(t.split("\n"), 1):
                if rx.search(line):
                    out.setdefault(p, set()).add(i)
        return out

    def dyn(self):
        """[(path, lines, pattern)]: code reading attributes by computed names.
        pattern: ('any',) or ('suffix', s) or ('prefix', s)."""
        if self._dyn is not None:
            return self._dyn
        out = []
        for p in self.paths:
            t = self.text(p)
            if not any(w in t for w in ("getattr", "hasattr", "vars(", "__dict__", "dir(", "attrgetter")):
                continue
            tree = self.tree(p)
            for node in ast.walk(tree):
                pat = None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    fn = node.func.id
                    if fn in ("getattr", "hasattr", "setattr", "delattr") and len(node.args) >= 2:
                        a = node.args[1]
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            continue        # a literal name: found by its token
                        pat = _name_pattern(a)
                    elif fn in ("vars", "dir"):
                        pat = ("any",)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                        node.func.attr == "attrgetter":
                    pat = ("any",)
                elif isinstance(node, ast.Attribute) and node.attr == "__dict__":
                    par = getattr(node, "_br_parent", None)
                    gp = getattr(par, "_br_parent", None)
                    if isinstance(par, ast.Attribute) and par.attr in ("get", "__contains__") and \
                            isinstance(gp, ast.Call) and gp.args and isinstance(gp.args[0], ast.Constant):
                        continue    # X.__dict__.get("literal"): found by its token
                    if isinstance(par, ast.Subscript) and isinstance(par.slice, ast.Constant):
                        continue
                    if isinstance(par, ast.Compare) and isinstance(par.left, ast.Constant):
                        continue    # "literal" in X.__dict__
                    pat = ("any",)
                if pat is None:
                    continue
                fname = _enclosing_func(node)
                if fname in SUFFIX_FUNCS:
                    pat = ("suffix", SUFFIX_FUNCS[fname])
                elif (p, fname) in MODULE_GETATTR:
                    pat = ("module",)
                out.append((p, set(range(node.lineno, (node.end_lineno or node.lineno) + 1)), pat))
        self._dyn = out
        return out

    def occurrences(self, names, attr_only=False):
        """[(path, lines, kind, alias)] for each read of one of `names`:
        kind 'keyed' (d[k], d.get(k), k in d), 'copy' (stored under an alias name),
        'store' (assigned), 'opaque' (anything else)."""
        if names and all(n in self._occ for n in names):
            return [(p, set(range(a, b + 1)), kind, alias)
                    for n in names for p, (a, b), kind, alias, is_attr in self._occ[n]
                    if is_attr or not attr_only]
        out = []
        for p in self.paths:
            t = self.text(p)
            if not any(n in t for n in names):
                continue
            for node, _name, kind, alias, _is_attr, _k in scan(self.tree(p), names, attr_only):
                lines = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
                out.append((p, lines, kind, alias))
        return out


def _name_pattern(a):
    if isinstance(a, ast.JoinedStr) and a.values:
        last, first = a.values[-1], a.values[0]
        if isinstance(last, ast.Constant) and isinstance(last.value, str) and last.value:
            return ("suffix", last.value)
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value:
            return ("prefix", first.value)
    if isinstance(a, ast.BinOp) and isinstance(a.op, ast.Add):
        if isinstance(a.right, ast.Constant) and isinstance(a.right.value, str) and a.right.value:
            return ("suffix", a.right.value)
        if isinstance(a.left, ast.Constant) and isinstance(a.left.value, str) and a.left.value:
            return ("prefix", a.left.value)
    return ("any",)


def _enclosing_func(node):
    n = getattr(node, "_br_parent", None)
    while n is not None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return n.name
        n = getattr(n, "_br_parent", None)
    return None


def _key_node(node):
    """For a keyed read of `node`: the key expression, or None."""
    parent = getattr(node, "_br_parent", None)
    if isinstance(parent, ast.Subscript) and parent.value is node:
        sl = parent.slice
        if isinstance(sl, ast.Slice) or (isinstance(sl, ast.Tuple) and any(isinstance(x, ast.Slice) for x in sl.elts)):
            return None      # a slice of a sequence, not a key
        return sl
    if isinstance(parent, ast.Attribute) and parent.value is node and parent.attr in KEYED_METHODS:
        gp = getattr(parent, "_br_parent", None)
        if isinstance(gp, ast.Call) and gp.func is parent and gp.args and \
                not isinstance(gp.args[0], ast.Starred):
            return gp.args[0]
        return None
    if isinstance(parent, ast.Compare) and all(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops):
        for i, c in enumerate(parent.comparators):
            if c is node:
                return parent.left if i == 0 else parent.comparators[i - 1]
    return None


def _classify(node, name):
    if isinstance(node.ctx, (ast.Store, ast.Del)):
        return ("store", None)
    if _key_node(node) is not None:
        return ("keyed", None)
    parent = getattr(node, "_br_parent", None)
    if isinstance(parent, ast.Compare) and len(parent.ops) == 1 and \
            isinstance(parent.ops[0], (ast.Is, ast.IsNot)) and \
            any(isinstance(x, ast.Constant) and x.value is None for x in [parent.left] + parent.comparators):
        return ("store", None)    # `x is None`: reads nothing of the value
    if _enclosing_func(node) in COPY_FUNCS:
        return ("copy", None)
    func = _enclosing_func_node(node)
    if func is None:
        return ("deftime", None)  # module or class body: what it computes, the data diff sees
    # x.ALIAS = <expr naming the dict>  (alone or in an `or`)
    parent = getattr(node, "_br_parent", None)
    p = parent
    while isinstance(p, ast.BoolOp):
        p = getattr(p, "_br_parent", None)
    if isinstance(p, ast.Assign) and len(p.targets) == 1 and node is not p.targets[0]:
        tgt = p.targets[0]
        tname = tgt.attr if isinstance(tgt, ast.Attribute) else getattr(tgt, "id", None)
        if tname in ALIASES.get(name, ()):
            return ("copy", tname)
        if isinstance(tgt, ast.Name):
            return ("local", tgt.id)
    if _is_callarg(node):
        return ("callarg", None)
    return ("opaque", None)


def _is_callarg(node):
    parent = getattr(node, "_br_parent", None)
    if isinstance(parent, ast.keyword):
        parent = getattr(parent, "_br_parent", None)
        return isinstance(parent, ast.Call)
    return isinstance(parent, ast.Call) and node in parent.args


def _enclosing_func_node(node):
    n = getattr(node, "_br_parent", None)
    while n is not None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return n
        n = getattr(n, "_br_parent", None)
    return None


def scan(tree, names, attr_only=False):
    """Reads of `names` in a parsed file, with reads through a local variable
    a function assigns one of them to: (node, name, kind, alias, is_attr, key_node)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in names:
            name, is_attr = node.attr, True
        elif not attr_only and isinstance(node, ast.Name) and node.id in names:
            name, is_attr = node.id, False
        else:
            continue
        kind, alias = _classify(node, name)
        if kind != "local":
            out.append((node, name, kind, alias, is_attr, _key_node(node) if kind == "keyed" else None))
            continue
        out.append((node, name, "copy", alias, is_attr, None))
        func = _enclosing_func_node(node)
        for use in ast.walk(func):
            if isinstance(use, ast.Name) and use.id == alias and isinstance(use.ctx, ast.Load):
                k2, _a2 = _classify(use, alias)
                if k2 == "keyed":
                    out.append((use, name, "keyed", None, is_attr, _key_node(use)))
                elif k2 == "callarg":
                    out.append((use, name, "callarg", None, is_attr, None))
                elif k2 != "store":
                    out.append((use, name, "opaque", None, is_attr, None))
    return out


class Narrower:
    def __init__(self, mp, src, keymap=None):
        self.mp = mp
        self.src = src
        u = 0
        for v in mp.cls_bits.values():
            u |= v
        n = len(mp.ids)
        self.no_types = ((1 << n) - 1) & ~u      # tests that use no class at all: always kept
        self.memo = {}
        self.keymap = keymap
        self.unrecorded = keymap["unrecorded"] if keymap is not None else 0

    def call_bits(self, path, lines):
        """Tests that run these lines while they run; for test code, also as the
        test module loads (what it computes then, its tests use)."""
        got = self.mp.file(path)
        if got is None:
            return 0
        cov = got[0]
        both = path.startswith("tests/")
        b = 0
        for ln in lines:
            e = cov.get(ln)
            if e:
                b |= (e[0] | e[1]) if both else e[1]
        return b

    def any_bits(self, path, lines):
        got = self.mp.file(path)
        if got is None:
            return 0
        cov = got[0]
        b = 0
        for ln in lines:
            e = cov.get(ln)
            if e:
                b |= e[0] | e[1]
        return b

    def dyn_bits(self, attr, module_level=True):
        k = ("dyn", attr, module_level)
        if k in self.memo:
            return self.memo[k]
        b = self._dyn_bits(attr, module_level)
        self.memo[k] = b
        return b

    def _dyn_bits(self, attr, module_level):
        b = 0
        for p, lines, pat in self.src.dyn():
            if pat[0] == "module" and not module_level:
                continue
            if pat[0] == "suffix" and not attr.endswith(pat[1]):
                continue
            if pat[0] == "prefix" and not attr.startswith(pat[1]):
                continue
            b |= self.call_bits(p, lines)
        return b

    def token_bits(self, tok):
        k = ("tok", tok)
        if k in self.memo:
            return self.memo[k]
        b = 0
        for p, lines in self.src.token_lines(tok).items():
            b |= self.call_bits(p, lines)
        self.memo[k] = b
        return b

    def recorded(self, attr):
        return self.keymap is not None and attr in self.keymap["names"]

    def opaque_bits(self, attr):
        """Tests that read `attr` other than key by key (or, when the key map was
        recorded for it, other than through a recording site)."""
        rec = self.recorded(attr)
        k = ("opq", attr, rec)
        if k in self.memo:
            return self.memo[k]
        names = (attr,) + ALIASES.get(attr, ())
        b = 0
        seen = {}
        for p, lines, kind, _alias in self.src.occurrences(set(names[:1])) + \
                self.src.occurrences(set(names[1:]), attr_only=True):
            seen.setdefault(p, set()).update(lines)
            if kind == "opaque" or (kind == "callarg" and not rec) or \
                    (kind == "deftime" and p.startswith("tests/")):
                b |= self.call_bits(p, lines)
        # the name written some other way (a string, getattr(x, "NAME")): no telling how it is read
        for name in names:
            for p, lines in self.src.token_lines(name).items():
                rest = lines - seen.get(p, set())
                if rest:
                    b |= self.call_bits(p, rest)
        self.memo[k] = b
        return b

    def key_bits(self, attr, key_repr):
        b = self.unrecorded
        for name in (attr,) + ALIASES.get(attr, ()):
            km = self.keymap["keys"].get(name, {})
            b |= km.get(key_repr, 0) | km.get("*", 0)
        return b

    def compute(self, entries):
        """entries: dicts from datadiff.describe.  Returns test bits or None."""
        bits = self.no_types
        done_attr = set()
        for e in entries:
            if e.get("opaque"):
                return None
            attr = e["attr"]
            if attr.startswith("__") and attr.endswith("__") and not e.get("func_lines"):
                return None
            key = e.get("key")
            keyed = key is not None and key[0] in ("class", "other")
            if e.get("func_lines") and not keyed:
                # (a function held in a dict or set is reached only through it)
                for p, lines in e["func_lines"]:
                    if p is None:
                        return None
                    bits |= self.call_bits(p, lines)
            module_level = ":" not in e["where"]
            if keyed and self.recorded(attr):
                bits |= self.key_bits(attr, key[-1] if key[0] == "other" else key[3])
                if key[0] == "class":
                    for c in [key[1]] + list(key[2]):
                        bits |= self.mp.cls_bits.get(c, 0)
                bits |= self.opaque_bits(attr)
                bits |= self.dyn_bits(attr, module_level)
                continue
            if keyed and key[0] == "class":
                _k, cname, subs = key[:3]
                for c in [cname] + list(subs):
                    bits |= self.mp.cls_bits.get(c, 0)
                    bits |= self.token_bits(c.rsplit(":", 1)[1].rsplit(".", 1)[-1])
                bits |= self.opaque_bits(attr)
                bits |= self.dyn_bits(attr, module_level)
                continue
            if ("any", attr) in done_attr:
                continue
            done_attr.add(("any", attr))
            bits |= self.token_bits(attr)
            bits |= self.dyn_bits(attr, module_level)
        return bits
