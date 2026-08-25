"""Lossless JSON for row images.

PostgreSQL `numeric` is arbitrary precision AND carries a display scale
(1.5000000000 is not 1.5). psycopg2 decodes jsonb with stock json.loads, which
maps every JSON number to a Python float — so a capture→artifact→apply round
trip silently rewrites 1.5000000000 to 1.5 and
2.7182818284590452353602874713527 to 2.718281828459045. Worse, the engine's
own verifiers decode through the same lossy path, so they cannot see it.

Fix: numbers parse to Decimal (exact, scale-preserving) and serialize back as
raw JSON number tokens. json.dumps cannot emit a Decimal unquoted — its float
formatter is hardwired to float.__repr__ — so Decimals are dumped as unique
nonce strings and unquoted in one regex pass.

Decimal equality is VALUE equality: Decimal('100.0') == Decimal('100.000').
Any comparison that must see scale corruption goes through jkey().
"""
import json
import re
import uuid
from decimal import Decimal

import psycopg2.extras


def loads(s):
    if isinstance(s, (bytes, bytearray)):
        s = s.decode()
    return json.loads(s, parse_float=Decimal)


# One random marker per process, so the substitution pattern compiles once
# (a per-call nonce misses re's cache and recompiles on every row).
_NONCE = uuid.uuid4().hex
_PAT = re.compile(f'"@@{_NONCE}:(\\d+)@@"')


def dumps(obj, **kw):
    """json.dumps, but Decimals survive as exact JSON numbers."""
    kw.setdefault("default", str)
    user_default, subs = kw["default"], []     # subs stays empty in the hot path

    def default(o):
        if isinstance(o, Decimal):
            subs.append(str(o))
            return f"@@{_NONCE}:{len(subs) - 1}@@"
        return user_default(o)

    kw["default"] = default
    s = json.dumps(obj, **kw)
    if not subs:
        return s
    return _PAT.sub(lambda m: subs[int(m.group(1))], s)


def jkey(obj):
    """Canonical text of a value — the only scale-sensitive comparison key."""
    return dumps(obj, sort_keys=True)


def same(a, b):
    """Value equality that also sees numeric SCALE. Cheap paths first: this
    runs once per column per journaled row."""
    if a != b:
        return False
    if type(a) is Decimal:
        return str(a) == str(b)
    if isinstance(a, (dict, list)):
        return jkey(a) == jkey(b)
    return True


def register():
    """Make every jsonb/json column in this process decode losslessly."""
    psycopg2.extras.register_default_jsonb(loads=loads, globally=True)
    psycopg2.extras.register_default_json(loads=loads, globally=True)
