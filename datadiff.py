"""What a patch changes in the data sqlglot builds while it imports.

In a forked child that has re-imported the patched modules, compare every
module-level and class-level value of the re-imported modules with the
pinned ones still held from before.  The result names the attributes (and,
for dicts, the keys) whose values differ.  Anything that cannot be compared
with certainty counts as changed."""

import enum
import math
import sys
import types

FunctionType = types.FunctionType
CodeType = types.CodeType
PRIMS = (int, float, complex, str, bytes, bool, type(None), range, slice)
MAX_DEPTH = 40


class _Opaque(Exception):
    pass


def _canon_type(t):
    return (getattr(t, "__module__", None), getattr(t, "__qualname__", None))


class Differ:
    def __init__(self, purged):
        self.purged = set(purged)      # modules whose objects were re-created
        self.memo = {}

    # -- keys of dicts and members of sets
    def canon(self, k):
        if isinstance(k, PRIMS) and not isinstance(k, float):
            return ("v", type(k).__name__, k)
        if isinstance(k, float):
            return ("f", repr(k))
        if isinstance(k, type):
            m = getattr(k, "__module__", None)
            if m in self.purged:
                return ("c", m, k.__qualname__)
            return ("i", id(k))
        if isinstance(k, enum.Enum):
            return ("e", _canon_type(type(k)), k.name)
        if isinstance(k, tuple):
            return ("t",) + tuple(self.canon(x) for x in k)
        if isinstance(k, frozenset):
            return ("fs", frozenset(self.canon(x) for x in k))
        if isinstance(k, (FunctionType, types.BuiltinFunctionType, types.ModuleType)):
            m = getattr(k, "__module__", None) or getattr(k, "__name__", None)
            if m in self.purged:
                return ("fn", m, getattr(k, "__qualname__", getattr(k, "__name__", None)))
            return ("i", id(k))
        if type(k).__hash__ is object.__hash__:
            m = getattr(type(k), "__module__", None)
            if m in self.purged:
                raise _Opaque("identity-hashed key of a re-created class")
            return ("i", id(k))
        raise _Opaque("key %r" % type(k))

    # -- deep equality, False when unsure
    def same(self, a, b, depth=0):
        if a is b:
            return True
        if depth > MAX_DEPTH:
            return False
        ta, tb = type(a), type(b)
        if ta is not tb:
            if _canon_type(ta) != _canon_type(tb) or _canon_type(ta)[0] not in self.purged:
                return False
        key = (id(a), id(b))
        if key in self.memo:
            return self.memo[key]
        self.memo[key] = True           # assume while comparing (cycles)
        try:
            r = self._same(a, b, depth)
        except _Opaque:
            r = False
        except RecursionError:
            r = False
        self.memo[key] = r
        return r

    def _same(self, a, b, depth):
        if isinstance(a, float):
            return a == b or (math.isnan(a) and math.isnan(b))
        if isinstance(a, PRIMS):
            return a == b
        if isinstance(a, type):
            ma, mb = getattr(a, "__module__", None), getattr(b, "__module__", None)
            if ma != mb or a.__qualname__ != b.__qualname__ or not isinstance(ma, str) or \
                    not ma.startswith(("sqlglot", "tests")):
                return False
            if ma in self.purged:
                return True     # the class itself is diffed where it is defined
            # made at run time (type(...)): compare what it is made of
            if len(a.__bases__) != len(b.__bases__) or not all(
                    self.same(x, y, depth + 1) for x, y in zip(a.__bases__, b.__bases__)):
                return False
            if not self.same(type(a), type(b), depth + 1):
                return False
            da = {k: v for k, v in a.__dict__.items() if k not in SKIP_CLASS_ATTRS}
            db = {k: v for k, v in b.__dict__.items() if k not in SKIP_CLASS_ATTRS}
            return self.same(da, db, depth + 1)
        if isinstance(a, types.ModuleType):
            return a.__name__ == b.__name__
        if isinstance(a, enum.Enum):
            return type(a).__qualname__ == type(b).__qualname__ and a.name == b.name and \
                self.same(a.value, b.value, depth + 1)
        if isinstance(a, FunctionType):
            return self.same_func(a, b, depth)
        if isinstance(a, (classmethod, staticmethod)):
            return self.same(a.__func__, b.__func__, depth + 1)
        if isinstance(a, property):
            return all(self.same(getattr(a, n), getattr(b, n), depth + 1) for n in ("fget", "fset", "fdel"))
        if isinstance(a, types.MethodType):
            return self.same(a.__func__, b.__func__, depth + 1) and self.same(a.__self__, b.__self__, depth + 1)
        if isinstance(a, (types.BuiltinFunctionType, types.BuiltinMethodType)):
            if a.__name__ != b.__name__:
                return False
            sa, sb = getattr(a, "__self__", None), getattr(b, "__self__", None)
            if isinstance(sa, types.ModuleType) or sa is None:
                return sa is sb or (isinstance(sa, types.ModuleType) and sa.__name__ == getattr(sb, "__name__", None))
            return self.same(sa, sb, depth + 1)
        if isinstance(a, (types.MemberDescriptorType, types.GetSetDescriptorType,
                          types.WrapperDescriptorType, types.MethodDescriptorType)):
            return a.__name__ == b.__name__ and self.same(a.__objclass__, b.__objclass__, depth + 1)
        if isinstance(a, CodeType):
            return self.same_code(a, b, depth)
        if isinstance(a, dict):
            if len(a) != len(b):
                return False
            ca = {self.canon(k): v for k, v in a.items()}
            cb = {self.canon(k): v for k, v in b.items()}
            if ca.keys() != cb.keys():
                return False
            return all(self.same(v, cb[k], depth + 1) for k, v in ca.items())
        if isinstance(a, (list, tuple)):
            if len(a) != len(b):
                return False
            if all(self.same(x, y, depth + 1) for x, y in zip(a, b)):
                return True
            if isinstance(a, tuple) and all(isinstance(x, type) for x in a) and \
                    all(isinstance(y, type) for y in b):
                # a tuple of classes made from a set: its order is the set's
                return sorted(map(repr, map(self.canon, a))) == sorted(map(repr, map(self.canon, b)))
            return False
        if isinstance(a, (type({}.keys()), type({}.values()), type({}.items()))):
            return self.same(list(a), list(b), depth + 1)
        if isinstance(a, (set, frozenset)):
            return len(a) == len(b) and {self.canon(x) for x in a} == {self.canon(x) for x in b}
        if isinstance(a, types.CellType):
            try:
                va = a.cell_contents
            except ValueError:
                try:
                    b.cell_contents
                except ValueError:
                    return True
                return False
            try:
                vb = b.cell_contents
            except ValueError:
                return False
            return self.same(va, vb, depth + 1)
        if type(a).__module__ in ("functools",) and type(a).__name__ == "partial":
            return self.same(a.func, b.func, depth + 1) and self.same(a.args, b.args, depth + 1) and \
                self.same(a.keywords, b.keywords, depth + 1)
        tm = type(a).__module__
        if tm in ("typing", "_typing", "types", "typing_extensions"):
            return repr(a) == repr(b)        # annotations: TypeVars, generic aliases
        if tm == "collections" and type(a).__name__ == "_tuplegetter":
            return a.__reduce__() == b.__reduce__()
        if tm == "_thread" or type(a).__name__ in ("RLock", "Lock", "_RLock"):
            return True                      # locks carry no data
        if not (isinstance(tm, str) and tm.startswith(("sqlglot", "tests"))) and \
                type(a).__eq__ is not object.__eq__ and getattr(a, "__dict__", None) is None:
            try:
                r = a == b
            except Exception:
                return False
            return r is True
        # plain instances: their state
        da = getattr(a, "__dict__", None)
        db = getattr(b, "__dict__", None)
        slots = []
        for k in type(a).__mro__:
            s = k.__dict__.get("__slots__", ())
            slots.extend([s] if isinstance(s, str) else s)
        if da is None and not slots:
            return False      # a C object we cannot look into
        if (da is None) != (db is None):
            return False
        if da is not None and not self.same(dict(da), dict(db), depth + 1):
            return False
        for s in slots:
            if s in ("__dict__", "__weakref__"):
                continue
            va = getattr(a, s, _MISSING)
            vb = getattr(b, s, _MISSING)
            if (va is _MISSING) != (vb is _MISSING):
                return False
            if va is not _MISSING and not self.same(va, vb, depth + 1):
                return False
        return True

    def same_func(self, a, b, depth):
        if a.__qualname__ != b.__qualname__:
            return False
        if not self.same_code(a.__code__, b.__code__, depth + 1):
            return False
        if not self.same(a.__defaults__, b.__defaults__, depth + 1):
            return False
        if not self.same(a.__kwdefaults__, b.__kwdefaults__, depth + 1):
            return False
        ca, cb = a.__closure__ or (), b.__closure__ or ()
        if len(ca) != len(cb):
            return False
        if not all(self.same(x, y, depth + 1) for x, y in zip(ca, cb)):
            return False
        # attributes set on the function object (decorators)
        da = {k: v for k, v in a.__dict__.items() if k != "__wrapped__"}
        db = {k: v for k, v in b.__dict__.items() if k != "__wrapped__"}
        if not self.same(da, db, depth + 1):
            return False
        if "__wrapped__" in a.__dict__ or "__wrapped__" in b.__dict__:
            if not self.same(a.__dict__.get("__wrapped__"), b.__dict__.get("__wrapped__"), depth + 1):
                return False
        return True

    def same_code(self, a, b, depth):
        for n in ("co_code", "co_names", "co_varnames", "co_freevars", "co_cellvars", "co_flags",
                  "co_argcount", "co_kwonlyargcount", "co_posonlyargcount", "co_name", "co_qualname"):
            if getattr(a, n) != getattr(b, n):
                return False
        ka, kb = a.co_consts, b.co_consts
        if len(ka) != len(kb):
            return False
        for x, y in zip(ka, kb):
            if isinstance(x, CodeType) and isinstance(y, CodeType):
                if not self.same_code(x, y, depth + 1):
                    return False
            elif type(x) is not type(y) or not self.same(x, y, depth + 1):
                return False
        return True


_MISSING = object()


def _from_tests(v):
    m = getattr(v, "__module__", None) if isinstance(v, type) else getattr(type(v), "__module__", None)
    return isinstance(m, str) and (m == "tests" or m.startswith("tests."))
SKIP_CLASS_ATTRS = {"__dict__", "__weakref__", "__module__", "__qualname__", "__firstlineno__",
                    "__static_attributes__", "__orig_bases__", "__parameters__", "__annotations__",
                    "__annotate__", "__annotations_cache__", "_abc_impl", "__abstractmethods__"}


def diff_modules(pairs, purged):
    """pairs: {name: (old_module, new_module)}.  Returns a list of entries
    (where, attr, key) where `where` is 'module:qualname', attr the name
    holding the value, key the canonical dict key (or None for the whole value),
    plus ('*', None, None) when something could not be compared."""
    d = Differ(purged)
    out = []

    def cmp_value(where, attr, va, vb):
        if va is _MISSING or vb is _MISSING:
            out.append((where, attr, None, va, vb))
            return
        if isinstance(va, dict) and isinstance(vb, dict) and type(va) is type(vb):
            try:
                ca = {d.canon(k): (k, v) for k, v in va.items()}
                cb = {d.canon(k): (k, v) for k, v in vb.items()}
            except _Opaque:
                out.append((where, attr, None, va, vb))
                return
            for ck in ca.keys() | cb.keys():
                a = ca.get(ck)
                b = cb.get(ck)
                if (a is None or b is None) and _from_tests((a or b)[1]):
                    continue     # registered by a test module as it was imported
                if a is None or b is None or not d.same(a[1], b[1]):
                    out.append((where, attr, (a or b)[0], a[1] if a else _MISSING, b[1] if b else _MISSING))
            return
        if isinstance(va, (set, frozenset)) and isinstance(vb, (set, frozenset)) and type(va) is type(vb):
            try:
                ca = {d.canon(x): x for x in va}
                cb = {d.canon(x): x for x in vb}
            except _Opaque:
                out.append((where, attr, None, va, vb))
                return
            for ck in ca.keys() ^ cb.keys():
                out.append((where, attr, ca.get(ck, cb.get(ck)), _MISSING, _MISSING))
            return
        if not d.same(va, vb):
            out.append((where, attr, None, va, vb))

    def cmp_class(ca, cb, seen):
        where = "%s:%s" % (ca.__module__, ca.__qualname__)
        if where in seen:
            return
        seen.add(where)
        if len(ca.__bases__) != len(cb.__bases__) or not all(
                d.same(x, y) for x, y in zip(ca.__bases__, cb.__bases__)):
            out.append((where, "__bases__", None, _MISSING, _MISSING))
        if not d.same(type(ca), type(cb)):
            out.append((where, "__class__", None, _MISSING, _MISSING))
        da, db = ca.__dict__, cb.__dict__
        for k in set(da) | set(db):
            if k in SKIP_CLASS_ATTRS:
                continue
            va, vb = da.get(k, _MISSING), db.get(k, _MISSING)
            if isinstance(va, type) and isinstance(vb, type) and va.__module__ == ca.__module__ and \
                    va.__qualname__.startswith(ca.__qualname__ + "."):
                cmp_class(va, vb, seen)
                continue
            cmp_value(where, k, va, vb)

    for name, (om, nm) in pairs.items():
        seen = set()
        va, vb = vars(om), vars(nm)
        lazy = "__getattr__" in va or "__getattr__" in vb
        for k in set(va) | set(vb):
            if lazy and (k not in va or k not in vb):
                continue         # filled in on demand by the module's __getattr__
            if k in ("__builtins__", "__loader__", "__spec__", "__cached__", "__file__", "__path__",
                     "__package__", "__name__"):
                continue
            a, b = va.get(k, _MISSING), vb.get(k, _MISSING)
            if (a is _MISSING and isinstance(b, types.ModuleType)) or \
                    (b is _MISSING and isinstance(a, types.ModuleType)):
                continue         # a package's submodule, set when it is first imported
            if isinstance(a, type) and isinstance(b, type) and a.__module__ == name and b.__module__ == name \
                    and a.__qualname__ == b.__qualname__:
                cmp_class(a, b, seen)
                continue
            cmp_value(name, k, a, b)
    return out


def _codes(v):
    if isinstance(v, (classmethod, staticmethod)):
        v = v.__func__
    if isinstance(v, types.MethodType):
        v = v.__func__
    if isinstance(v, property):
        return [c for f in (v.fget, v.fset, v.fdel) if f is not None for c in _codes(f)]
    if isinstance(v, FunctionType):
        return [v.__code__]
    return []


def _all_lines(code):
    lines = set()
    stack = [code]
    while stack:
        c = stack.pop()
        lines.update(ln for _s, _e, ln in c.co_lines() if ln is not None)
        stack.extend(k for k in c.co_consts if isinstance(k, CodeType))
    return lines


def _subclasses(k):
    out, stack = set(), [k]
    while stack:
        c = stack.pop()
        try:
            subs = type.__subclasses__(c)
        except TypeError:
            subs = []
        for sc in subs:
            name = "%s:%s" % (sc.__module__, sc.__qualname__)
            if name not in out:
                out.add(name)
                stack.append(sc)
    return out


def key_repr(k):
    """How a key is named in the recorded key map (tools/keyrec.py does the same)."""
    try:
        return repr(k)[:100]
    except Exception:
        return "?"


def describe(raw, root):
    """Plain, picklable entries for narrow.Narrower."""
    import os
    root = os.path.realpath(root) + os.sep
    out = []
    for where, attr, key, va, vb in raw:
        e = {"where": where, "attr": attr}
        if attr in ("__bases__", "__class__"):
            e["opaque"] = True
        if isinstance(key, type):
            e["key"] = ("class", "%s:%s" % (key.__module__, key.__qualname__), sorted(_subclasses(key)),
                        key_repr(key))
        elif key is not None:
            e["key"] = ("other", key_repr(key))
        fl = []
        for c in _codes(va) if va is not _MISSING else []:
            f = os.path.realpath(c.co_filename)
            fl.append((f[len(root):] if f.startswith(root) else None, sorted(_all_lines(c))))
        if fl:
            e["func_lines"] = fl
        if key is not None:
            # a value changed under a key both sides have: the dict keeps its size
            e["resize"] = va is _MISSING or vb is _MISSING
        out.append(e)
    return out
