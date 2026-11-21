"""Collection-time only: record which keys tests look up in sqlglot's
class- and module-level dicts and sets.  Installed into builtins as
__br_rec__ (a key at a keyed site) and __br_wrap__ (a dict or set passed as
an argument: a proxy that records every lookup, and '*' for anything else).
The proxies keep no data of their own, so C code that reads a dict or set
directly sees it empty and the test fails: such tests are marked unrecorded."""
import builtins

REC = set()


def _r(k):
    try:
        return repr(k)[:100]
    except Exception:
        return "?"


def rec(name, key):
    REC.add((name, _r(key)))
    return key


def _all(name):
    REC.add((name, "*"))


def _mk(base, keyed):
    """A proxy class over `base` (dict, set or frozenset) that delegates to an inner object."""
    ns = {"__slots__": ("_br_n", "_br_v")}

    def keyed_method(mname):
        real = getattr(base, mname)

        def f(self, k, *a, **kw):
            REC.add((self._br_n, _r(k)))
            return real(self._br_v, k, *a, **kw)
        f.__name__ = mname
        return f

    def all_method(mname):
        real = getattr(base, mname)

        def f(self, *a, **kw):
            REC.add((self._br_n, "*"))
            a = tuple(x._br_v if isinstance(x, _PROXIES) else x for x in a)
            return real(self._br_v, *a, **kw)
        f.__name__ = mname
        return f

    for m in keyed:
        if hasattr(base, m):
            ns[m] = keyed_method(m)

    def __len__(self):
        REC.add((self._br_n, "#len"))     # size (and truthiness) only
        return len(self._br_v)
    ns["__len__"] = __len__
    for m in dir(base):
        if m in ns or m in keyed or m in ("__len__", "__new__", "__init__", "__getattribute__", "__setattr__", "__delattr__",
                                           "__class__", "__init_subclass__", "__subclasshook__", "__class_getitem__",
                                           "fromkeys", "__doc__", "__slots__", "__dir__", "__format__",
                                           "__getstate__", "__reduce__", "__reduce_ex__", "__sizeof__"):
            continue
        if callable(getattr(base, m, None)):
            ns[m] = all_method(m)

    def __reduce_ex__(self, proto):
        REC.add((self._br_n, "*"))
        return (base, (base(self._br_v),))
    ns["__reduce_ex__"] = __reduce_ex__
    ns["__reduce__"] = lambda self: __reduce_ex__(self, 2)
    if base is frozenset:
        ns["__hash__"] = lambda self: (REC.add((self._br_n, "*")), hash(self._br_v))[1]
    else:
        ns["__hash__"] = None
    return type("Rec" + base.__name__.capitalize(), (base,), ns)


RecDict = _mk(dict, ("__getitem__", "get", "__contains__", "pop", "setdefault", "__delitem__"))
RecSet = _mk(set, ("__contains__", "discard", "remove"))
RecFrozenset = _mk(frozenset, ("__contains__",))
_PROXIES = (RecDict, RecSet, RecFrozenset)


def wrap(name, v):
    t = type(v)
    if t is dict:
        p = dict.__new__(RecDict)
    elif t is set:
        p = set.__new__(RecSet)
    elif t is frozenset:
        p = frozenset.__new__(RecFrozenset)
    else:
        return v
    object.__setattr__(p, "_br_n", name)
    object.__setattr__(p, "_br_v", v)
    return p


builtins.__br_rec__ = rec
builtins.__br_wrap__ = wrap
