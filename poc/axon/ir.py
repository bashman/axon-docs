"""IR v1 (vertical slice): node shapes, canonical form, Merkle hashing, tree flattening.

Stored nodes only reference children by hash. `define` input may nest nodes inline;
`flatten` turns such a tree into a post-order list of (hash, stored node).
"""

import json
import re
import unicodedata

import blake3

I64_MIN, I64_MAX = -(2**63), 2**63 - 1
MAX_ARITY = 16
MAX_LET_DEPTH = 64
MAX_STRING_BYTES = 1 << 20
MAX_TYPE_DEPTH = 8
ATOM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
BIN_OPS = ("add", "sub", "mul", "lt", "eq", "div", "rem")
# Identity: id = [ENCODING_TAG][BLAKE3 digest of the canonical binary node]. The tag names the
# hash algorithm AND the identity encoding version; textual form is TEXT_PREFIX + digest hex.
# Rule: a tag (and a node kind tag) never changes meaning; changing the semantics of an
# existing node requires a new node tag or a new encoding tag. See docs/adr/0001-identidad.md.
ENCODING_TAG = 0x01
TEXT_PREFIX = "b3:"
HASH_RE = re.compile(r"^b3:[0-9a-f]{64}$")
# Kind tags: 1-31 expression nodes, 32-47 types/records, 48-63 actors/effects (ADR-0004),
# 64-127 second expression block (ADR-0007).
KIND_TAG = {
    "lit": 1, "param": 2, "bin": 3, "if": 4, "call": 5, "selfcall": 6, "fn": 7,
    "let": 8, "var": 9, "true": 10, "false": 11, "not": 12, "and": 13, "or": 14,
    "str": 15, "concat": 16, "length": 17, "repeat": 18, "streq": 19,
    "none": 20, "some": 21, "ok": 22, "err": 23, "match": 24, "atom": 25, "error": 26,
    "mk": 27, "get": 28, "with": 29, "unit": 30,
    "rec": 32,
    # effects (ADR-0008)
    "kv_get": 48, "kv_put": 49, "kv_del": 50, "kv_list": 51, "kv_keys": 52, "now": 53,
    "print": 54, "cap": 55,
    # second expression block (ADR-0007): collections and higher-order combinators
    "list": 64, "len": 65, "at": 66, "append": 67, "fnref": 68, "map": 69, "filter": 70,
    "fold": 71, "and_then": 72, "sort_by": 73, "reverse": 74,
}
# Type codes (ADR-0004/0005). 1-15 primitives (one byte); 16-31 constructors followed by their
# arguments (16 = function type, reserved for round 5; 17 = option<T>; 18 = result<T,E>);
# 32 = nominal type followed by ref(declaration), reserved for records. `error` is a builtin
# primitive for now and becomes the first nominal record later. JSON spells a primitive as the
# key of TYPE_CODE, option<T> as {"option": T} and result<T,error> as {"result": T}.
TYPE_CODE = {"i64": 1, "bool": 2, "string": 3, "atom": 4, "error": 5, "unit": 6}
CTOR_OPTION, CTOR_RESULT, CTOR_LIST, NOMINAL = 17, 18, 19, 32
OP_TAG = {"add": 1, "sub": 2, "mul": 3, "lt": 4, "eq": 5, "div": 6, "rem": 7}

# kind -> (child fields holding one child, child fields holding a list of children)
CHILD_FIELDS = {
    "lit": ((), ()),
    "param": ((), ()),
    "bin": (("a", "b"), ()),
    "if": (("c", "t", "e"), ()),
    "call": (("fn",), ("args",)),
    "selfcall": ((), ("args",)),
    "fn": (("body",), ()),
    "let": (("v", "body"), ()),
    "var": ((), ()),
    "true": ((), ()),
    "false": ((), ()),
    "not": (("a",), ()),
    "and": (("a", "b"), ()),
    "or": (("a", "b"), ()),
    "str": ((), ()),
    "concat": (("a", "b"), ()),
    "length": (("a",), ()),
    "repeat": (("s", "n"), ()),
    "streq": (("a", "b"), ()),
    "none": ((), ()),
    "some": (("v",), ()),
    "ok": (("v",), ()),
    "err": (("v",), ()),
    "match": (("s",), ("arms",)),
    "atom": ((), ()),
    "error": (("code", "detail"), ()),
    "mk": (("decl",), ("fields",)),
    "get": (("r",), ()),
    "with": (("r", "v"), ()),
    "rec": ((), ()),
    "unit": ((), ()),
    "list": ((), ("items",)),
    "len": (("l",), ()),
    "at": (("l", "idx"), ()),
    "append": (("a", "b"), ()),
    "fnref": (("fn",), ()),
    "map": (("l", "f", "ctx"), ()),
    "filter": (("l", "f", "ctx"), ()),
    "fold": (("l", "init", "f", "ctx"), ()),
    "and_then": (("r", "f", "ctx"), ()),
    "sort_by": (("l", "f", "ctx"), ()),
    "reverse": (("l",), ()),
    "kv_get": (("key",), ()),
    "kv_put": (("key", "v"), ()),
    "kv_del": (("key",), ()),
    "kv_list": (("prefix",), ()),
    "kv_keys": (("prefix",), ()),
    "now": ((), ()),
    "print": (("s",), ()),
    "cap": ((), ("args",)),
}
SCALAR_FIELDS = {
    "lit": {"v"},
    "param": {"i"},
    "bin": {"op"},
    "if": set(),
    "call": set(),
    "selfcall": set(),
    "fn": {"params", "ret"},
    "let": set(),
    "var": {"i"},
    "true": set(),
    "false": set(),
    "not": set(),
    "and": set(),
    "or": set(),
    "str": {"v"},
    "concat": set(),
    "length": set(),
    "repeat": set(),
    "streq": set(),
    "none": {"t"},
    "some": set(),
    "ok": set(),
    "err": {"t"},
    "match": set(),
    "atom": {"name"},
    "error": set(),
    "mk": set(),
    "get": {"i"},
    "with": {"i"},
    "rec": {"tag", "fields"},
    "unit": set(),
    "list": {"t"},
    "len": set(),
    "at": set(),
    "append": set(),
    "fnref": set(),
    "map": set(),
    "filter": set(),
    "fold": set(),
    "and_then": set(),
    "sort_by": set(),
    "reverse": set(),
    "kv_get": {"ns"},
    "kv_put": {"ns"},
    "kv_del": {"ns"},
    "kv_list": {"ns"},
    "kv_keys": {"ns"},
    "now": set(),
    "print": set(),
    "cap": {"provider", "op"},
}


class GraphError(Exception):
    """Structured error returned to the agent: node, code, detail."""

    def __init__(self, node, code, detail):
        self.info = {"node": node, "code": code, "detail": detail}
        super().__init__(self.info)


def is_hash(x) -> bool:
    return isinstance(x, str) and HASH_RE.match(x) is not None


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def check_type(t, where, label="type", depth=0) -> None:
    """Validate a JSON type: a TYPE_CODE key, {"option": T} or {"result": T}."""
    if depth > MAX_TYPE_DEPTH:
        raise GraphError(where, "E_BAD_NODE", f"{label}: type nesting exceeds {MAX_TYPE_DEPTH}")
    if isinstance(t, str) and t in TYPE_CODE:
        return
    if isinstance(t, dict) and len(t) == 1 and next(iter(t)) in ("option", "result", "list"):
        check_type(next(iter(t.values())), where, label, depth + 1)
        return
    if isinstance(t, dict) and len(t) == 1 and "rec" in t and is_hash(t["rec"]):
        return
    raise GraphError(
        where,
        "E_BAD_NODE",
        f"{label}: type must be one of {sorted(TYPE_CODE)}, {{\"option\": T}}, "
        f"{{\"result\": T}}, {{\"list\": T}} or {{\"rec\": <b3: id of a declaration>}}, got {t!r}",
    )


def type_refs(t) -> list[str]:
    """Declaration ids referenced from inside a JSON type (nominal types)."""
    if isinstance(t, dict):
        (ctor, arg), = t.items()
        return [arg] if ctor == "rec" else type_refs(arg)
    return []


def encode_type(t) -> bytes:
    """Identity encoding of a type (see ADR-0005); result always carries E = error."""
    if isinstance(t, str):
        return bytes([TYPE_CODE[t]])
    (ctor, arg), = t.items()
    if ctor == "rec":
        return bytes([NOMINAL]) + child_ref(arg)
    if ctor == "option":
        return bytes([CTOR_OPTION]) + encode_type(arg)
    if ctor == "list":
        return bytes([CTOR_LIST]) + encode_type(arg)
    return bytes([CTOR_RESULT]) + encode_type(arg) + bytes([TYPE_CODE["error"]])


def _check_scalars(node: dict, where) -> None:
    kind = node["k"]
    if kind == "lit":
        if not _is_int(node.get("v")) or not I64_MIN <= node["v"] <= I64_MAX:
            raise GraphError(where, "E_BAD_NODE", "lit.v must be an i64 integer")
    elif kind == "param":
        if not _is_int(node.get("i")) or not 0 <= node["i"] < MAX_ARITY:
            raise GraphError(
                where, "E_BAD_NODE", f"param.i must be an integer in 0..{MAX_ARITY - 1}"
            )
    elif kind == "var":
        if not _is_int(node.get("i")) or not 0 <= node["i"] < MAX_LET_DEPTH:
            raise GraphError(
                where, "E_BAD_NODE", f"var.i must be an integer in 0..{MAX_LET_DEPTH - 1}"
            )
    elif kind == "bin":
        if node.get("op") not in BIN_OPS:
            raise GraphError(where, "E_BAD_NODE", f"bin.op must be one of {list(BIN_OPS)}")
    elif kind == "fn":
        params = node.get("params")
        if not isinstance(params, list) or len(params) > MAX_ARITY:
            raise GraphError(where, "E_BAD_NODE", f"fn.params must be a list (max {MAX_ARITY})")
        for t in [*params, node.get("ret")]:
            check_type(t, where, "fn")
    elif kind == "str":
        v = node.get("v")
        if not isinstance(v, str):
            raise GraphError(where, "E_BAD_NODE", "str.v must be a string")
        try:
            size = len(v.encode("utf-8"))
        except UnicodeEncodeError:
            raise GraphError(where, "E_BAD_NODE", "str.v must be valid UTF-8") from None
        if size > MAX_STRING_BYTES:
            raise GraphError(
                where, "E_STR_LIMIT", f"string literal exceeds {MAX_STRING_BYTES} bytes"
            )
    elif kind in ("kv_get", "kv_put", "kv_del", "kv_list", "kv_keys"):
        if not isinstance(node.get("ns"), str) or not ATOM_RE.match(node["ns"]):
            raise GraphError(where, "E_BAD_NODE", f"{kind}.ns must match {ATOM_RE.pattern}")
    elif kind == "cap":
        for field in ("provider", "op"):
            if not isinstance(node.get(field), str) or not ATOM_RE.match(node[field]):
                raise GraphError(
                    where, "E_BAD_NODE", f"cap.{field} must match {ATOM_RE.pattern}"
                )
    elif kind == "atom":
        if not isinstance(node.get("name"), str) or not ATOM_RE.match(node["name"]):
            raise GraphError(where, "E_BAD_NODE", f"atom.name must match {ATOM_RE.pattern}")
    elif kind in ("none", "err", "list"):
        check_type(node.get("t"), where, f"{kind}.t")
    elif kind in ("get", "with"):
        if not _is_int(node.get("i")) or not 0 <= node["i"] < MAX_ARITY:
            raise GraphError(
                where, "E_BAD_NODE", f"{kind}.i must be an integer in 0..{MAX_ARITY - 1}"
            )
    elif kind == "rec":
        fields = node.get("fields")
        if not isinstance(node.get("tag"), str) or not ATOM_RE.match(node["tag"]):
            raise GraphError(where, "E_BAD_NODE", f"rec.tag must match {ATOM_RE.pattern}")
        if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_ARITY:
            raise GraphError(
                where, "E_BAD_NODE", f"rec.fields must be a list of 1..{MAX_ARITY} types"
            )
        for t in fields:
            check_type(t, where, "rec.fields")


def check_shape(node, where=None, *, inline: bool) -> None:
    """Validate keys and scalar fields. inline=True allows dict children (define input)."""
    if not isinstance(node, dict) or node.get("k") not in CHILD_FIELDS:
        raise GraphError(
            where, "E_BAD_NODE", f"node must be an object with k in {sorted(CHILD_FIELDS)}"
        )
    kind = node["k"]
    singles, lists = CHILD_FIELDS[kind]
    allowed = {"k"} | SCALAR_FIELDS[kind] | set(singles) | set(lists)
    extra = set(node) - allowed
    if extra:
        raise GraphError(where, "E_BAD_NODE", f"unexpected fields for {kind}: {sorted(extra)}")
    for field in singles:
        if field not in node:
            raise GraphError(where, "E_BAD_NODE", f"{kind}.{field} is required")
        _check_child(node[field], where, f"{kind}.{field}", inline)
    for field in lists:
        if not isinstance(node.get(field), list) or len(node[field]) > MAX_ARITY:
            raise GraphError(
                where, "E_BAD_NODE", f"{kind}.{field} must be a list (max {MAX_ARITY})"
            )
        for c in node[field]:
            _check_child(c, where, f"{kind}.{field}[]", inline)
    _check_scalars(node, where)


def _check_child(c, where, label, inline) -> None:
    if is_hash(c):
        return
    if inline and isinstance(c, dict):
        return
    raise GraphError(
        where, "E_BAD_NODE", f"{label} must be a b3:<64 hex> id" + (" or a node" if inline else "")
    )


def children(node: dict) -> list[str]:
    """Child hashes of a stored node, in field order."""
    singles, lists = CHILD_FIELDS[node["k"]]
    out = [node[f] for f in singles]
    for f in lists:
        out.extend(node[f])
    # nominal types embedded in signatures and annotations are references too
    for t in [*node.get("params", []), *node.get("fields", []), node.get("ret"), node.get("t")]:
        out.extend(type_refs(t))
    return out


def canonical(node: dict) -> bytes:
    """Stable JSON of a stored node. STORAGE payload only: it is not part of the identity."""
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _sleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        done = (n == 0 and not b & 0x40) or (n == -1 and b & 0x40)
        out.append(b if done else b | 0x80)
        if done:
            return bytes(out)


def child_ref(h: str) -> bytes:
    """Binary form of a child reference: encoding tag + raw digest."""
    return bytes([ENCODING_TAG]) + bytes.fromhex(h[len(TEXT_PREFIX) :])


def encode_node(node: dict) -> bytes:
    """Identity encoding v1 of a stored node: kind tag, then fields in fixed order.

    Child references are the full [tag][digest]. Integers are LEB128 (lit uses signed).
    """
    kind = node["k"]
    out = bytearray([KIND_TAG[kind]])
    if kind == "lit":
        out += _sleb(node["v"])
    elif kind == "param":
        out += _uleb(node["i"])
    elif kind == "var":
        out += _uleb(node["i"])
    elif kind == "let":
        out += child_ref(node["v"]) + child_ref(node["body"])
    elif kind == "bin":
        out += bytes([OP_TAG[node["op"]]]) + child_ref(node["a"]) + child_ref(node["b"])
    elif kind == "if":
        out += child_ref(node["c"]) + child_ref(node["t"]) + child_ref(node["e"])
    elif kind == "call":
        out += child_ref(node["fn"]) + _uleb(len(node["args"]))
        for a in node["args"]:
            out += child_ref(a)
    elif kind == "selfcall":
        out += _uleb(len(node["args"]))
        for a in node["args"]:
            out += child_ref(a)
    elif kind == "fn":
        out += _uleb(len(node["params"]))
        for t in [*node["params"], node["ret"]]:
            out += encode_type(t)
        out += child_ref(node["body"])
    elif kind in ("true", "false"):
        pass
    elif kind in ("and", "or", "concat", "streq"):
        out += child_ref(node["a"]) + child_ref(node["b"])
    elif kind in ("length", "not"):
        out += child_ref(node["a"])
    elif kind == "repeat":
        out += child_ref(node["s"]) + child_ref(node["n"])
    elif kind == "str":
        raw = node["v"].encode("utf-8")
        out += _uleb(len(raw)) + raw
    elif kind == "atom":
        raw = node["name"].encode("ascii")
        out += _uleb(len(raw)) + raw
    elif kind == "none":
        out += encode_type(node["t"])
    elif kind in ("some", "ok"):
        out += child_ref(node["v"])
    elif kind == "err":
        out += child_ref(node["v"]) + encode_type(node["t"])
    elif kind == "match":
        out += child_ref(node["s"]) + _uleb(len(node["arms"]))
        for a in node["arms"]:
            out += child_ref(a)
    elif kind == "error":
        out += child_ref(node["code"]) + child_ref(node["detail"])
    elif kind == "mk":
        out += child_ref(node["decl"]) + _uleb(len(node["fields"]))
        for f in node["fields"]:
            out += child_ref(f)
    elif kind == "get":
        out += child_ref(node["r"]) + _uleb(node["i"])
    elif kind == "with":
        out += child_ref(node["r"]) + _uleb(node["i"]) + child_ref(node["v"])
    elif kind == "list":
        out += encode_type(node["t"]) + _uleb(len(node["items"]))
        for x in node["items"]:
            out += child_ref(x)
    elif kind in ("len", "reverse"):
        out += child_ref(node["l"])
    elif kind == "at":
        out += child_ref(node["l"]) + child_ref(node["idx"])
    elif kind == "append":
        out += child_ref(node["a"]) + child_ref(node["b"])
    elif kind == "fnref":
        out += child_ref(node["fn"])
    elif kind in ("map", "filter", "sort_by"):
        out += child_ref(node["l"]) + child_ref(node["f"]) + child_ref(node["ctx"])
    elif kind == "fold":
        out += (
            child_ref(node["l"]) + child_ref(node["init"]) + child_ref(node["f"])
            + child_ref(node["ctx"])
        )
    elif kind == "and_then":
        out += child_ref(node["r"]) + child_ref(node["f"]) + child_ref(node["ctx"])
    elif kind in ("kv_get", "kv_del", "kv_list", "kv_keys"):
        raw = node["ns"].encode("ascii")
        out += _uleb(len(raw)) + raw
        out += child_ref(node["prefix" if kind in ("kv_list", "kv_keys") else "key"])
    elif kind == "kv_put":
        raw = node["ns"].encode("ascii")
        out += _uleb(len(raw)) + raw + child_ref(node["key"]) + child_ref(node["v"])
    elif kind == "print":
        out += child_ref(node["s"])
    elif kind == "cap":
        for field in ("provider", "op"):
            raw = node[field].encode("ascii")
            out += _uleb(len(raw)) + raw
        out += _uleb(len(node["args"]))
        for arg in node["args"]:
            out += child_ref(arg)
    elif kind == "rec":
        raw = node["tag"].encode("ascii")
        out += _uleb(len(raw)) + raw + _uleb(len(node["fields"]))
        for t in node["fields"]:
            out += encode_type(t)
    return bytes(out)


def node_hash(node: dict) -> str:
    """Merkle id of a stored node: children are ids, so the id covers the whole subtree."""
    return TEXT_PREFIX + blake3.blake3(encode_node(node)).hexdigest()


def digest_hex(h: str) -> str:
    """The digest part of a textual id (what short prefixes are matched against)."""
    return h[len(TEXT_PREFIX) :]


def fake_hash(seed: str = "") -> str:
    """A well-formed id that points to nothing (for tests and negative cases)."""
    return TEXT_PREFIX + blake3.blake3(b"fake:" + seed.encode()).hexdigest()


def flatten(tree: dict) -> tuple[str, list[tuple[str, dict]]]:
    """Turn a (possibly nested) node into (root hash, [(hash, stored node)] in post-order).

    Duplicate subtrees collapse to one entry (content addressing).
    """
    out: dict[str, dict] = {}

    def visit(n, path):
        check_shape(n, path, inline=True)
        singles, lists = CHILD_FIELDS[n["k"]]
        stored = dict(n)
        if n["k"] == "str":
            stored["v"] = unicodedata.normalize("NFC", n["v"])
        for f in singles:
            stored[f] = child(n[f], f"{path}.{f}")
        for f in lists:
            stored[f] = [child(c, f"{path}.{f}[{i}]") for i, c in enumerate(n[f])]
        h = node_hash(stored)
        out.setdefault(h, stored)
        return h

    def child(c, path):
        return c if is_hash(c) else visit(c, path)

    root = visit(tree, "$")
    return root, list(out.items())
