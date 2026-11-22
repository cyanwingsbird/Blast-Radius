"""Collection-time only: record, per comparison site, which kinds of operand
pairs the tests compare.  Installed into builtins as __br_cmp__.

A pair's class says how every comparison operator would answer for it:

    ident (8) + relation
    relation: 0 equal and ordered, 1 equal and unordered, 2 less, 3 greater,
              4 unequal with every ordering false, 5 unequal and unordered, 7 unknown

Only pairs whose comparison cannot run code with side effects are evaluated;
anything else is recorded as unknown."""
import builtins
import datetime
import decimal
import operator
import sys

REC = set()
EQO, EQU, LT, GT, NEO, NEU, ERR = 0, 1, 2, 3, 4, 5, 7
OPS = [operator.is_, operator.is_not, operator.eq, operator.ne, operator.lt, operator.le, operator.gt, operator.ge]
OPNAMES = ["Is", "IsNot", "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE"]

K_NONE, K_IDENT, K_SCALAR, K_CONT, K_EXPR, K_DIALECT, K_OTHER = range(7)
_SCALARS = (int, float, str, bytes, complex, datetime.datetime, datetime.date, datetime.time,
            datetime.timedelta, decimal.Decimal)
_CONTS = (tuple, list, set, frozenset, dict)
_SLOTS = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")
_KIND = {}


def truth(op, cls):
    """True/False for operator number `op` on a pair of class `cls`; None when it raises or is unknown."""
    ident, rel = cls >> 3, cls & 7
    if rel == ERR:
        return None
    if op == 0:
        return bool(ident)
    if op == 1:
        return not ident
    if op == 2:
        return rel in (EQO, EQU)
    if op == 3:
        return rel not in (EQO, EQU)
    if rel in (EQU, NEU):
        return None
    if op == 4:
        return rel == LT
    if op == 5:
        return rel in (LT, EQO)
    if op == 6:
        return rel == GT
    return rel in (GT, EQO)


def _same_slots(t, base):
    for s in _SLOTS:
        if getattr(t, s) is not getattr(base, s):
            return False
    return True


def _kind(x):
    if x is None:
        return K_NONE
    t = type(x)
    k = _KIND.get(t)
    if k is not None:
        return k
    k = K_OTHER
    core = sys.modules.get("sqlglot.expressions.core")
    dia = sys.modules.get("sqlglot.dialects.dialect")
    if dia is not None and (isinstance(x, dia.Dialect) or isinstance(x, dia._Dialect)):
        k = K_DIALECT
    elif t.__eq__ is object.__eq__ and t.__ne__ is object.__ne__:
        k = K_IDENT
    elif core is not None and isinstance(x, core.Expression) and t.__eq__ is core.Expression.__eq__ \
            and t.__ne__ is core.Expression.__ne__:
        k = K_EXPR
    else:
        for base in _SCALARS:
            if issubclass(t, base) and _same_slots(t, base):
                k = K_SCALAR
                break
        else:
            for base in _CONTS:
                if issubclass(t, base) and _same_slots(t, base):
                    k = K_CONT
                    break
    if core is not None and dia is not None:
        _KIND[t] = k
    return k


def _safe(x, budget):
    """Elements (recursively) whose comparison runs no sqlglot code; returns the budget left or -1."""
    k = _kind(x)
    if k in (K_NONE, K_SCALAR, K_IDENT):
        return budget - 1
    if k != K_CONT:
        return -1
    if len(x) > budget:
        return -1
    if isinstance(x, dict):
        for a, b in x.items():
            budget = _safe(a, budget)
            if budget < 0:
                return -1
            budget = _safe(b, budget)
            if budget < 0:
                return -1
        return budget
    for a in x:
        budget = _safe(a, budget)
        if budget < 0:
            return -1
    return budget


def _rel(a, b):
    try:
        e = a == b
    except Exception:
        return ERR
    if e is True:
        try:
            lt, gt, le, ge = a < b, a > b, a <= b, a >= b
        except TypeError:
            return EQU
        except Exception:
            return ERR
        if lt is False and gt is False and le is True and ge is True:
            return EQO
        return ERR
    if e is not False:
        return ERR
    try:
        lt, gt, le, ge = a < b, a > b, a <= b, a >= b
    except TypeError:
        return NEU
    except Exception:
        return ERR
    if lt is True and gt is False and le is True and ge is False:
        return LT
    if gt is True and lt is False and ge is True and le is False:
        return GT
    if lt is False and gt is False and le is False and ge is False:
        return NEO
    return ERR


def classify(a, b):
    if a is b:
        k = _kind(a)
        if k in (K_NONE, K_IDENT, K_EXPR, K_DIALECT):
            return 8 + EQU
        if k in (K_SCALAR, K_CONT) and _safe(a, 64) >= 0:
            return 8 + _rel(a, b)
        return 8 + ERR
    ka, kb = _kind(a), _kind(b)
    if ka == K_OTHER or kb == K_OTHER:
        return ERR
    if ka == K_NONE or kb == K_NONE:
        return NEU
    if ka == K_DIALECT or kb == K_DIALECT:
        return ERR
    if ka == K_IDENT or kb == K_IDENT:
        return NEU
    if ka == K_EXPR or kb == K_EXPR:
        if ka == kb:
            return NEU if type(a) is not type(b) else ERR
        return NEU
    if _safe(a, 64) < 0 or _safe(b, 64) < 0:
        return ERR
    return _rel(a, b)


def cmp(sid, a, op, b):
    try:
        r = OPS[op](a, b)
    except BaseException:
        REC.add((sid, ERR))
        raise
    try:
        c = classify(a, b)
        if truth(op, c) is not r:
            c = ERR
    except BaseException:
        c = ERR
    REC.add((sid, c))
    return r


builtins.__br_cmp__ = cmp
