"""Reference interpreter used for provider-capability programs.

The frozen WASM backend intentionally does not lower ``cap`` yet.  Service
execution routes graphs that reach a capability through this interpreter while
all older graphs keep using the established WASM path.
"""

from __future__ import annotations

import unicodedata

from . import values
from .ir import I64_MAX, I64_MIN, MAX_STRING_BYTES, GraphError


def uses_cap(get, entry: str) -> bool:
    seen = set()

    def visit(ref):
        if ref in seen:
            return False
        seen.add(ref)
        node = get(ref)
        if node is None:
            return False
        if node["k"] == "cap":
            return True
        from .ir import children

        return any(visit(child) for child in children(node))

    return visit(entry)


class _Machine:
    def __init__(self, get, host, field_names, fuel):
        self.get = get
        self.host = host
        self.field_names = field_names
        self.fuel = fuel

    def node(self, ref):
        node = self.get(ref)
        if node is None:
            raise GraphError(ref, "E_UNKNOWN_REF", f"unknown node {ref}")
        return node

    def spend(self, ref):
        if self.fuel <= 0:
            raise GraphError(ref, "E_FUEL", "execution fuel exhausted")
        self.fuel -= 1

    def call(self, ref, args):
        fn = self.node(ref)
        if fn["k"] != "fn":
            raise GraphError(ref, "E_TYPE_MISMATCH", "call target must reference a fn node")
        if len(args) != len(fn["params"]):
            raise GraphError(
                ref, "E_ARITY", f"expected {len(fn['params'])} arguments, got {len(args)}"
            )
        return self.eval(fn["body"], args, (), ref)

    def eval(self, ref, params, env, current_fn):  # noqa: C901
        self.spend(ref)
        node = self.node(ref)
        kind = node["k"]

        def ev(child):
            return self.eval(child, params, env, current_fn)
        if kind == "lit":
            return node["v"]
        if kind == "str":
            return node["v"]
        if kind == "atom":
            return {"atom": node["name"]}
        if kind == "true":
            return True
        if kind == "false":
            return False
        if kind == "unit":
            return None
        if kind == "param":
            return params[node["i"]]
        if kind == "var":
            return env[-1 - node["i"]]
        if kind == "let":
            value = ev(node["v"])
            return self.eval(node["body"], params, (*env, value), current_fn)
        if kind == "if":
            return ev(node["t"] if ev(node["c"]) else node["e"])
        if kind == "not":
            return not ev(node["a"])
        if kind == "and":
            return ev(node["a"]) and ev(node["b"])
        if kind == "or":
            return ev(node["a"]) or ev(node["b"])
        if kind == "bin":
            return self.binary(ref, node["op"], ev(node["a"]), ev(node["b"]))
        if kind == "concat":
            value = unicodedata.normalize("NFC", ev(node["a"]) + ev(node["b"]))
            if len(value.encode()) > MAX_STRING_BYTES:
                raise GraphError(ref, "E_STR_LIMIT", "concatenated string exceeds limit")
            return value
        if kind == "length":
            return len(ev(node["a"]))
        if kind == "repeat":
            value = ev(node["s"]) * ev(node["n"])
            if len(value.encode()) > MAX_STRING_BYTES:
                raise GraphError(ref, "E_STR_LIMIT", "repeated string exceeds limit")
            return value
        if kind == "streq":
            return ev(node["a"]) == ev(node["b"])
        if kind == "none":
            return {"tag": "none"}
        if kind == "some":
            return {"tag": "some", "v": ev(node["v"])}
        if kind == "ok":
            return {"tag": "ok", "v": ev(node["v"])}
        if kind == "err":
            return {"tag": "err", "v": ev(node["v"])}
        if kind == "error":
            return {"code": ev(node["code"]), "detail": ev(node["detail"])}
        if kind == "match":
            subject = ev(node["s"])
            index = 0 if subject["tag"] in {"none", "ok"} else 1
            binds = subject["tag"] != "none"
            arm_env = (*env, subject.get("v")) if binds else env
            return self.eval(node["arms"][index], params, arm_env, current_fn)
        if kind == "call":
            return self.call(node["fn"], [ev(arg) for arg in node["args"]])
        if kind == "selfcall":
            return self.call(current_fn, [ev(arg) for arg in node["args"]])
        if kind == "mk":
            vals = [ev(field) for field in node["fields"]]
            names = self._names(node["decl"], len(vals))
            return dict(zip(names, vals, strict=True)) if names else vals
        if kind == "get":
            return self._field(ev(node["r"]), node["i"])
        if kind == "with":
            record, value = ev(node["r"]), ev(node["v"])
            if isinstance(record, dict):
                key = list(record)[node["i"]]
                return {**record, key: value}
            result = list(record)
            result[node["i"]] = value
            return result
        if kind == "list":
            return [ev(item) for item in node["items"]]
        if kind == "len":
            return len(ev(node["l"]))
        if kind == "at":
            items, index = ev(node["l"]), ev(node["idx"])
            if 0 <= index < len(items):
                return {"tag": "some", "v": items[index]}
            return {"tag": "none"}
        if kind == "append":
            result = ev(node["a"]) + ev(node["b"])
            if len(result) > values.MAX_LIST_LENGTH:
                raise GraphError(ref, "E_LIST_LIMIT", "list exceeds limit")
            return result
        if kind == "reverse":
            return list(reversed(ev(node["l"])))
        if kind in {"map", "filter", "fold", "and_then", "sort_by"}:
            return self.combinator(node, ev)
        if kind == "kv_get":
            return self.host.kv_get(node["ns"], ev(node["key"]))
        if kind == "kv_put":
            self.host.kv_put(node["ns"], ev(node["key"]), ev(node["v"]))
            return None
        if kind == "kv_del":
            self.host.kv_del(node["ns"], ev(node["key"]))
            return None
        if kind == "kv_list":
            return self.host.kv_list(node["ns"], ev(node["prefix"]))
        if kind == "kv_keys":
            return self.host.kv_keys(node["ns"], ev(node["prefix"]))
        if kind == "now":
            return self.host.clock()
        if kind == "print":
            self.host.print(ev(node["s"]))
            return None
        if kind == "cap":
            return self.host.cap(node["provider"], node["op"], [ev(arg) for arg in node["args"]])
        raise GraphError(ref, "E_UNSUPPORTED", f"interpreter does not support {kind}")

    def binary(self, ref, op, left, right):
        if op == "lt":
            return left < right
        if op == "eq":
            return left == right
        if op in {"div", "rem"} and right == 0:
            raise GraphError(ref, "E_DIV_ZERO", "i64 division by zero")
        if op == "add":
            result = left + right
        elif op == "sub":
            result = left - right
        elif op == "mul":
            result = left * right
        elif op == "div":
            result = abs(left) // abs(right) * (-1 if (left < 0) != (right < 0) else 1)
        else:
            quotient = abs(left) // abs(right) * (-1 if (left < 0) != (right < 0) else 1)
            result = left - quotient * right
        if not I64_MIN <= result <= I64_MAX:
            raise GraphError(ref, "E_OVERFLOW", f"i64 {op} overflow")
        return result

    def combinator(self, node, ev):
        target = self.node(node["f"])["fn"]
        ctx = ev(node["ctx"])
        if node["k"] == "and_then":
            result = ev(node["r"])
            return result if result["tag"] == "err" else self.call(target, [ctx, result["v"]])
        items = ev(node["l"])
        if node["k"] == "map":
            return [self.call(target, [ctx, item]) for item in items]
        if node["k"] == "filter":
            return [item for item in items if self.call(target, [ctx, item])]
        if node["k"] == "fold":
            acc = ev(node["init"])
            for item in items:
                acc = self.call(target, [ctx, acc, item])
            return acc
        return sorted(items, key=lambda item: self.call(target, [ctx, item]))

    def _names(self, decl, arity):
        if self.field_names is None:
            return None
        names = [self.field_names(decl, index) for index in range(arity)]
        return names if all(name is not None for name in names) else None

    @staticmethod
    def _field(record, index):
        return list(record.values())[index] if isinstance(record, dict) else record[index]


def run(get, entry, args, *, fuel=10_000_000, field_names=None, host=None):
    if host is None:
        raise GraphError(entry, "E_NO_HOST", "effectful execution requires a host")
    fn = get(entry)
    if fn is None or fn.get("k") != "fn":
        raise GraphError(entry, "E_TYPE_MISMATCH", "entry must reference a fn node")
    if len(args) != len(fn["params"]):
        raise GraphError(
            entry, "E_ARITY", f"expected {len(fn['params'])} arguments, got {len(args)}"
        )
    for index, (type_, value) in enumerate(zip(fn["params"], args, strict=True)):
        reason = values.is_encodable(get, type_, value)
        if reason:
            raise GraphError(entry, "E_TYPE_MISMATCH", f"args[{index}]: {reason}")
    normalized = [
        _normalize_value(get, type_, value, field_names)
        for type_, value in zip(fn["params"], args, strict=True)
    ]
    return _Machine(get, host, field_names, fuel).call(entry, normalized)


def _normalize_value(get, type_, value, field_names):
    """Put boundary records in declaration order before positional field access."""
    if not isinstance(type_, dict):
        return value
    constructor, payload = next(iter(type_.items()))
    if constructor == "rec":
        declaration = get(payload)
        field_types = declaration["fields"]
        if not isinstance(value, dict) or field_names is None:
            return value
        names = [field_names(payload, index) for index in range(len(field_types))]
        if not all(name is not None for name in names):
            return value
        return {
            name: _normalize_value(get, field_type, value[name], field_names)
            for name, field_type in zip(names, field_types, strict=True)
        }
    if constructor == "list":
        return [_normalize_value(get, payload, item, field_names) for item in value]
    if constructor in {"option", "result"} and "v" in value:
        nested_type = payload if value["tag"] != "err" else "error"
        return {**value, "v": _normalize_value(get, nested_type, value["v"], field_names)}
    return value
