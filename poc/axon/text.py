"""Compact textual syntax for the IR (subphase 2.1).

This module renders ``define`` payloads (the inline IR node dicts) to a small S-expression
text and parses that text back to *exactly* the same dicts. It never changes the IR, the hashes
or the frozen identity encoding (ADR-0010): ``Service.define`` yields the same node hash for the
original and for the reconstructed node.

The canonical writing form omits ids; passing ``ids`` (a ``name -> id`` map) appends a short
``#<8 hex>`` after every referenced or defined name so a follow-up ``patch`` can address it.
``from_text`` ignores those suffixes, so the round trip is lossless in both modes.

Grammar (see ``docs/adr/0012-sintaxis-textual.md`` for the full reference)::

    program    := form*
    form       := "(" "fn" NAME fn_tail ")"        # named definition
                | NAME "=" node                    # named definition of any node
                | node                             # a single bare expression
    fn_tail    := "(" type* ")" type effects? node
    effects    := "!" "{" ... "}"                  # derived; parsed and dropped
    node       := INT | STRING | "$" INT | "@" INT | "true" | "false" | "unit"
                | "(" KIND arg* ")"
    type       := "i64" | "bool" | "string" | "atom" | "error" | "unit"
                | "option" "<" type ">" | "list" "<" type ">"
                | "result" "<" type ("," "error")? ">"
                | NAME                             # nominal record, by tag

Field order is positional and follows ``spec/ir-v1.json`` exactly, so no ``key=`` is ever
needed (every IR v1 field is mandatory and unambiguous).
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any

from .ir import is_hash

__all__ = ["TextSyntaxError", "from_text", "to_text"]

_TYPE_HELP = (
    "expected a type: i64 bool string atom error unit option<T> list<T> result<T> "
    "(or a record name)"
)
_SHAPES = {
    "err": "write (err (error (atom E_X) (none string)) T), or the sugar (err (atom E_X) T)",
}
_PRIMS = ("i64", "bool", "string", "atom", "error", "unit")
_CTORS = ("option", "list", "result")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:#[0-9a-fA-F]+)?")
_HASH_RE = re.compile(r"b3:[0-9a-f]{64}")
_LIT_WORDS = ("true", "false", "unit")
_MATCH_ARM_FAMILIES = {
    "none": ("none", "some"),
    "some": ("none", "some"),
    "ok": ("ok", "err"),
    "err": ("ok", "err"),
}

# kind -> ((field, role), ...) in the exact field order of spec/ir-v1.json. A role tells the
# parser (and the renderer) how to read/write that field:
#   int | str | word | ns       scalar
#   type                        one JSON type
#   types                       a parenthesised list of JSON types
#   ref | decl | fnref          a name/hash reference (decl/fnref keep or wrap it)
#   child | children            one node / the remaining nodes
_SCHEMA: dict[str, tuple[tuple[str, str], ...]] = {
    "lit": (("v", "int"),),
    "param": (("i", "int"),),
    "bin": (("op", "word"), ("a", "child"), ("b", "child")),
    "if": (("c", "child"), ("t", "child"), ("e", "child")),
    "call": (("fn", "ref"), ("args", "children")),
    "selfcall": (("args", "children"),),
    "fn": (("params", "types"), ("ret", "type"), ("body", "child")),
    "let": (("v", "child"), ("body", "child")),
    "var": (("i", "int"),),
    "true": (),
    "false": (),
    "not": (("a", "child"),),
    "and": (("a", "child"), ("b", "child")),
    "or": (("a", "child"), ("b", "child")),
    "str": (("v", "str"),),
    "concat": (("a", "child"), ("b", "child")),
    "length": (("a", "child"),),
    "repeat": (("s", "child"), ("n", "child")),
    "streq": (("a", "child"), ("b", "child")),
    "none": (("t", "type"),),
    "some": (("v", "child"),),
    "ok": (("v", "child"),),
    "err": (("v", "child"), ("t", "type")),
    "match": (("s", "child"), ("arms", "children")),
    "atom": (("name", "word"),),
    "error": (("code", "child"), ("detail", "child")),
    "mk": (("decl", "decl"), ("fields", "children")),
    "get": (("r", "child"), ("i", "int")),
    "with": (("r", "child"), ("i", "int"), ("v", "child")),
    "rec": (("tag", "word"), ("fields", "types")),
    "unit": (),
    "list": (("t", "type"), ("items", "children")),
    "len": (("l", "child"),),
    "at": (("l", "child"), ("idx", "child")),
    "append": (("a", "child"), ("b", "child")),
    "fnref": (("fn", "ref"),),
    "map": (("l", "child"), ("f", "fnref"), ("ctx", "child")),
    "filter": (("l", "child"), ("f", "fnref"), ("ctx", "child")),
    "fold": (("l", "child"), ("init", "child"), ("f", "fnref"), ("ctx", "child")),
    "and_then": (("r", "child"), ("f", "fnref"), ("ctx", "child")),
    "sort_by": (("l", "child"), ("f", "fnref"), ("ctx", "child")),
    "reverse": (("l", "child"),),
    "kv_get": (("ns", "word"), ("key", "child")),
    "kv_put": (("ns", "word"), ("key", "child"), ("v", "child")),
    "kv_del": (("ns", "word"), ("key", "child")),
    "kv_list": (("ns", "word"), ("prefix", "child")),
    "kv_keys": (("ns", "word"), ("prefix", "child")),
    "now": (),
    "print": (("s", "child"),),
    "cap": (("provider", "word"), ("op", "word"), ("args", "cap_args")),
}


class TextSyntaxError(ValueError):
    """A malformed textual IR, with the 1-based line/column where it was detected."""

    def __init__(self, message: str, line: int, col: int):
        self.message = message
        self.line = line
        self.col = col
        super().__init__(f"{message} (line {line}, col {col})")


# --------------------------------------------------------------------------- rendering


def _plain(name: Any) -> str:
    if isinstance(name, str) and (_NAME_RE.fullmatch(name) or is_hash(name)):
        return name
    return json.dumps(name, ensure_ascii=False)


def _short_id(ref: str) -> str:
    return ref.rsplit(":", 1)[-1][:8]


def _name_token(name: Any, ids: dict[str, str], *, suffix: bool) -> str:
    out = _plain(name)
    if suffix and isinstance(name, str) and name in ids:
        out += "#" + _short_id(ids[name])
    return out


def _render_rec_name(name: str) -> str:
    # A nominal tag colliding with a primitive/constructor keyword is quoted so it parses back
    # as a record and not as a reserved word.
    if name in (*_PRIMS, *_CTORS, "rec"):
        return json.dumps(name)
    return _plain(name)


def _render_type(t: Any) -> str:
    if isinstance(t, str):
        return _plain(t)
    ((ctor, arg),) = t.items()
    if ctor == "option":
        return f"option<{_render_type(arg)}>"
    if ctor == "list":
        return f"list<{_render_type(arg)}>"
    if ctor == "result":
        return f"result<{_render_type(arg)},error>"
    if ctor == "rec":
        return _render_rec_name(arg)
    raise ValueError(f"unknown type constructor {ctor!r}")


def _render_field(role: str, value: Any, ids: dict[str, str]) -> str:
    if role == "int":
        return str(value)
    if role == "str":
        return json.dumps(value, ensure_ascii=False)
    if role in ("word", "ns"):
        return _plain(value)
    if role == "type":
        return _render_type(value)
    if role == "types":
        return "(" + " ".join(_render_type(x) for x in value) + ")"
    if role == "ref":
        return _name_token(value, ids, suffix=True)
    if role == "decl":
        return _name_token(value, ids, suffix=False)
    if role == "fnref":
        return _name_token(value["fn"], ids, suffix=True)
    if role == "child":
        return _render_node(value, ids)
    if role == "children":
        return " ".join(_render_node(x, ids) for x in value)
    if role == "cap_args":
        return " ".join(
            _plain(x["name"]) if x.get("k") == "atom" else _render_node(x, ids)
            for x in value
        )
    raise ValueError(f"unknown field role {role!r}")


def _render_node(node: dict, ids: dict[str, str]) -> str:
    kind = node["k"]
    if kind == "lit":
        return str(node["v"])
    if kind == "str":
        return json.dumps(node["v"], ensure_ascii=False)
    if kind in ("true", "false", "unit"):
        return kind
    if kind == "param":
        return f"${node['i']}"
    if kind == "var":
        return f"@{node['i']}"
    schema = _SCHEMA.get(kind)
    if schema is None:
        raise ValueError(f"unknown node kind {kind!r}")
    parts = [kind]
    parts += [piece for field, role in schema if (piece := _render_field(role, node[field], ids))]
    return "(" + " ".join(parts) + ")"


def _render_def(name: str, node: dict, ids: dict[str, str]) -> str:
    if node.get("k") == "fn":
        parts = ["fn", _name_token(name, ids, suffix=True)]
        parts += [
            piece
            for field, role in _SCHEMA["fn"]
            if (piece := _render_field(role, node[field], ids))
        ]
        return "(" + " ".join(parts) + ")"
    return _name_token(name, ids, suffix=True) + " = " + _render_node(node, ids)


def _named_items(node: Any) -> list[tuple[str, dict]]:
    if isinstance(node, dict):
        items = list(node.items())
    elif isinstance(node, (list, tuple)):
        items = []
        for item in node:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                items.append((item[0], item[1]))
            elif isinstance(item, dict) and "node" in item:
                items.append((item.get("name"), item["node"]))
            else:
                raise TypeError("program items must be (name, node) pairs or {name, node}")
    else:
        raise TypeError("to_text expects a node, a {name: node} mapping or a list of items")
    for name, child in items:
        if not isinstance(name, str) or not isinstance(child, dict) or "k" not in child:
            raise TypeError("program items must be (name, node) pairs or {name, node}")
    return items


def to_text(node: Any, *, ids: dict[str, str] | None = None) -> str:
    """Render an IR node (or a named program) as compact textual IR.

    ``node`` may be one IR node dict (the inline dict ``define`` takes), a ``{name: node}``
    mapping, or a list of ``(name, node)`` / ``{"name", "node"}`` items (as ``emit_program`` and
    ``define_many`` produce). With ``ids`` (a ``name -> id`` map) a short ``#<hex>`` is appended
    after referenced and defined names; ``from_text`` drops it again.
    """
    id_map = ids or {}
    if isinstance(node, dict) and "k" in node:
        return _render_node(node, id_map)
    return "\n".join(_render_def(name, child, id_map) for name, child in _named_items(node))


# --------------------------------------------------------------------------- lexer


class _Tok:
    __slots__ = ("kind", "value", "line", "col")

    def __init__(self, kind: str, value: str, line: int, col: int):
        self.kind = kind
        self.value = value
        self.line = line
        self.col = col


_SINGLE = {
    "(": "lparen",
    ")": "rparen",
    "<": "lt",
    ">": "gt",
    "!": "bang",
    "{": "lbrace",
    "}": "rbrace",
    "=": "eq",
}


def _tokenize(src: str) -> tuple[list[_Tok], tuple[int, int]]:
    toks: list[_Tok] = []
    i, n = 0, len(src)
    line = col = 1

    def advance(count: int) -> None:
        nonlocal i, line, col
        for _ in range(count):
            if i < n and src[i] == "\n":
                line, col = line + 1, 1
            else:
                col += 1
            i += 1

    while i < n:
        char = src[i]
        if char in " \t\r\n,":
            advance(1)
            continue
        start_line, start_col = line, col
        if char in _SINGLE:
            toks.append(_Tok(_SINGLE[char], char, start_line, start_col))
            advance(1)
            continue
        if char == '"':
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            if j >= n:
                raise TextSyntaxError("unterminated string literal", start_line, start_col)
            raw = src[i : j + 1]
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                raise TextSyntaxError("invalid string literal", start_line, start_col) from None
            toks.append(_Tok("string", value, start_line, start_col))
            advance(j + 1 - i)
            continue
        hash_match = _HASH_RE.match(src, i)
        if hash_match is not None:
            toks.append(_Tok("hash", hash_match.group(0), start_line, start_col))
            advance(len(hash_match.group(0)))
            continue
        if char in "$@":
            j = i + 1
            while j < n and src[j] in "0123456789":
                j += 1
            if j == i + 1:
                raise TextSyntaxError(f"expected digits after {char!r}", start_line, start_col)
            kind = "param" if char == "$" else "var"
            toks.append(_Tok(kind, src[i + 1 : j], start_line, start_col))
            advance(j - i)
            continue
        if char in "0123456789" or (char == "-" and i + 1 < n and src[i + 1] in "0123456789"):
            j = i + 1
            while j < n and src[j] in "0123456789":
                j += 1
            toks.append(_Tok("int", src[i:j], start_line, start_col))
            advance(j - i)
            continue
        word_match = _WORD_RE.match(src, i)
        if word_match is not None:
            toks.append(_Tok("word", word_match.group(0), start_line, start_col))
            advance(len(word_match.group(0)))
            continue
        raise TextSyntaxError(f"unexpected character {char!r}", start_line, start_col)
    return toks, (line, col)


# --------------------------------------------------------------------------- parser


class _Form:
    __slots__ = ("kind", "name", "node", "line", "col")

    def __init__(self, kind: str, name: str | None, node: dict, line: int, col: int):
        self.kind = kind
        self.name = name
        self.node = node
        self.line = line
        self.col = col


class _Parser:
    def __init__(self, toks: list[_Tok], eof: tuple[int, int]):
        self.toks = toks
        self.i = 0
        self.eof = eof

    def peek(self, ahead: int = 0) -> _Tok | None:
        j = self.i + ahead
        return self.toks[j] if j < len(self.toks) else None

    def here(self) -> tuple[int, int]:
        tok = self.peek()
        return (tok.line, tok.col) if tok is not None else self.eof

    def error(self, message: str):
        line, col = self.here()
        raise TextSyntaxError(message, line, col)

    def error_at(self, tok: _Tok, message: str):
        raise TextSyntaxError(message, tok.line, tok.col)

    def take(self, kind: str, what: str) -> _Tok:
        tok = self.peek()
        if tok is None or tok.kind != kind:
            self.error(f"expected {what}")
        self.i += 1
        return tok

    def at_word(self, ahead: int, value: str) -> bool:
        tok = self.peek(ahead)
        return tok is not None and tok.kind == "word" and tok.value == value

    def word_at(self, ahead: int) -> bool:
        tok = self.peek(ahead)
        return tok is not None and tok.kind == "word"

    # -- forms -----------------------------------------------------------------
    def parse_form(self) -> _Form:
        tok = self.peek()
        if tok is None:
            self.error("expected a definition or an expression")
        line, col = tok.line, tok.col
        if tok.kind == "lparen":
            if self.at_word(1, "fn") and self.word_at(2):
                self.take("lparen", "'('")
                self.take("word", "'fn'")
                name = _strip_id(self.take("word", "a function name").value)
                node = self.parse_fn_tail()
                self.take("rparen", "')'")
                return _Form("def", name, node, line, col)
            return _Form("node", None, self.parse_node(), line, col)
        if tok.kind == "word" and self.peek(1) is not None and self.peek(1).kind == "eq":
            name = _strip_id(self.take("word", "a name").value)
            self.take("eq", "'='")
            return _Form("def", name, self.parse_node(), line, col)
        return _Form("node", None, self.parse_node(), line, col)

    def parse_fn_tail(self) -> dict:
        params = self.parse_type_list()
        ret = self.parse_type()
        self.parse_effects()
        body = self.parse_node()
        return {"k": "fn", "params": params, "ret": ret, "body": body}

    def parse_effects(self) -> None:
        if self.peek() is None or self.peek().kind != "bang":
            return
        self.take("bang", "'!'")
        self.take("lbrace", "'{' after '!'")
        depth = 1
        while depth:
            tok = self.peek()
            if tok is None:
                self.error("expected '}' to close the effect list")
            if tok.kind == "lbrace":
                depth += 1
            elif tok.kind == "rbrace":
                depth -= 1
            self.i += 1

    # -- nodes -----------------------------------------------------------------
    def parse_node(self) -> dict:
        tok = self.peek()
        if tok is None:
            self.error("expected an expression")
        if tok.kind == "lparen":
            return self.parse_sexp()
        if tok.kind == "int":
            self.i += 1
            return {"k": "lit", "v": int(tok.value)}
        if tok.kind == "string":
            self.i += 1
            return {"k": "str", "v": tok.value}
        if tok.kind == "param":
            self.i += 1
            return {"k": "param", "i": int(tok.value)}
        if tok.kind == "var":
            self.i += 1
            return {"k": "var", "i": int(tok.value)}
        if tok.kind == "word" and tok.value in _LIT_WORDS:
            self.i += 1
            return {"k": tok.value}
        self.error("expected an expression")

    def parse_sexp(self) -> dict:
        self.take("lparen", "'('")
        kind_tok = self.peek()
        if kind_tok is None or kind_tok.kind != "word":
            self.error("expected a node kind after '('")
        kind = kind_tok.value
        self.i += 1
        if kind == "fn":
            node = self.parse_fn_tail()
            self.take("rparen", "')'")
            return node
        schema = _SCHEMA.get(kind)
        if schema is None:
            close = difflib.get_close_matches(kind, _SCHEMA, n=1, cutoff=0.75)
            if close:
                self.error_at(kind_tok, f"unknown node kind {kind!r}; did you mean {close[0]!r}?")
            # sugar: `(name arg...)` is a call to the function `name`, like `(call name arg...)`
            args = self.parse_children()
            self.take("rparen", "')'")
            return {"k": "call", "fn": kind, "args": args}
        out: dict[str, Any] = {"k": kind}
        if kind == "match":
            return self.parse_match(out)
        if kind in ("get", "with"):
            return self.parse_field_access(kind, out)
        if kind == "mk" and self.mk_by_name():
            return self.parse_mk_by_name(out)
        for field, role in schema:
            nxt = self.peek()
            if nxt is not None and nxt.kind == "rparen" and kind in _SHAPES:
                self.error(f"({kind} ...) is missing its '{field}' argument; {_SHAPES[kind]}")
            out[field] = self.parse_field(role)
        self.take("rparen", "')'")
        if kind == "err" and out["v"].get("k") == "atom":
            # sugar: `(err (atom E_X) T)` means `(err (error (atom E_X) (none string)) T)`
            out["v"] = {
                "k": "error",
                "code": out["v"],
                "detail": {"k": "none", "t": "string"},
            }
        return out

    # `(match s (none a) (some b))` and `(match s (ok a) (err b))` are common
    # agent-written spellings. They are reading sugar for `(match s a b)`. We only accept the
    # sugar when the same text is not also a valid canonical match whose arms happen to produce
    # option/result constructors; that case would otherwise silently change the IR.
    def parse_match(self, out: dict[str, Any]) -> dict:
        out["s"] = self.parse_node()
        arms_start = self.i

        canonical = canonical_error = None
        canonical_end = arms_start
        try:
            canonical = self.parse_children()
            canonical_end = self.i
        except TextSyntaxError as exc:
            canonical_error = exc

        self.i = arms_start
        tagged = tagged_error = None
        tagged_end = arms_start
        tag_tok = self._match_arm_tag()
        try:
            tagged = self.parse_tagged_match_arms()
            tagged_end = self.i
        except TextSyntaxError as exc:
            tagged_error = exc

        if canonical is not None and tagged is not None:
            suggestion = "(match " + " ".join(
                [_render_node(out["s"], {}), *(_render_node(arm, {}) for arm in tagged)]
            ) + ")"
            self.error_at(
                tag_tok,
                f"ambiguous match arms; did you mean {suggestion!r} without constructor labels?",
            )
        if tagged is not None:
            out["arms"] = tagged
            self.i = tagged_end
        elif canonical is not None:
            out["arms"] = canonical
            self.i = canonical_end
        elif tagged_error is not None:
            raise tagged_error
        else:
            raise canonical_error
        self.take("rparen", "')'")
        return out

    def _match_arm_tag(self) -> _Tok | None:
        tok = self.peek(1)
        if self.peek() is None or self.peek().kind != "lparen":
            return None
        if tok is None or tok.kind != "word" or tok.value not in _MATCH_ARM_FAMILIES:
            return None
        return tok

    def parse_tagged_match_arms(self) -> list[dict] | None:
        first = self._match_arm_tag()
        if first is None:
            return None
        expected = _MATCH_ARM_FAMILIES[first.value]
        hint = f"(match SCRUTINEE ({expected[0]} EXPR) ({expected[1]} EXPR))"
        arms = []
        for tag in expected:
            if not (self.peek() is not None and self.peek().kind == "lparen"):
                self.error_at(first, f"invalid tagged match arms; did you mean {hint!r}?")
            self.i += 1
            actual = self.peek()
            if actual is None or actual.kind != "word" or actual.value != tag:
                self.error_at(first, f"invalid tagged match arms; did you mean {hint!r}?")
            self.i += 1
            if self.peek() is None or self.peek().kind == "rparen":
                self.error_at(
                    actual,
                    f"tagged arm {tag!r} needs an expression; did you mean {hint!r}?",
                )
            arms.append(self.parse_node())
            if self.peek() is None or self.peek().kind != "rparen":
                self.error_at(first, f"invalid tagged match arms; did you mean {hint!r}?")
            self.i += 1
        if self.peek() is None or self.peek().kind != "rparen":
            self.error_at(first, f"invalid tagged match arms; did you mean {hint!r}?")
        return arms

    # -- writing sugar: fields by name instead of by index ---------------------------------
    # `(get r nota titulo)` and `(with r nota titulo v)` are bound by `Service.define` to the
    # canonical `i` index; `(mk nota titulo=a cuerpo=b)` becomes `values`. The canonical
    # positional forms (`(get r 0)`, `(mk nota a b)`) keep parsing exactly as before.
    def parse_field_access(self, kind: str, out: dict[str, Any]) -> dict:
        out["r"] = self.parse_node()
        tok = self.peek()
        if tok is not None and tok.kind == "word":
            out["of"] = self.take("word", "a record name").value
            out["field"] = self.take("word", "a field name").value
        else:
            out["i"] = int(self.take("int", "a field index or a record and field name").value)
        if kind == "with":
            out["v"] = self.parse_node()
        self.take("rparen", "')'")
        return out

    def mk_by_name(self) -> bool:
        # position is on the declaration: `nota titulo=...`
        return self.word_at(1) and self.peek(2) is not None and self.peek(2).kind == "eq"

    def parse_mk_by_name(self, out: dict[str, Any]) -> dict:
        out["decl"] = self.parse_name_like("a record declaration")
        values: dict[str, Any] = {}
        while True:
            tok = self.peek()
            if tok is None:
                self.error("expected ')'")
            if tok.kind == "rparen":
                self.i += 1
                out["values"] = values
                return out
            name = self.take("word", "a field name").value
            self.take("eq", "'=' after the field name")
            if name in values:
                self.error_at(tok, f"duplicate field {name!r}")
            values[name] = self.parse_node()

    def parse_field(self, role: str) -> Any:
        if role == "child":
            return self.parse_node()
        if role == "children":
            return self.parse_children()
        if role == "cap_args":
            out = []
            while self.peek() is not None and self.peek().kind != "rparen":
                if self.peek().kind == "word" and self.peek().value not in _LIT_WORDS:
                    out.append({"k": "atom", "name": self.take("word", "an atom").value})
                else:
                    out.append(self.parse_node())
            return out
        if role == "type":
            return self.parse_type()
        if role == "types":
            return self.parse_type_list()
        if role == "ref":
            return _strip_id(self.parse_name_like("a function reference"))
        if role == "decl":
            return self.parse_name_like("a record declaration")
        if role == "fnref":
            tok = self.peek()
            if tok is not None and tok.kind == "lparen":
                return self.parse_node()
            return {"k": "fnref", "fn": _strip_id(self.parse_name_like("a function reference"))}
        if role == "word":
            return self.take("word", "a name").value
        if role == "int":
            return int(self.take("int", "an integer").value)
        if role == "str":
            return self.take("string", "a string").value
        self.error(f"cannot parse field role {role!r}")

    def parse_children(self) -> list[dict]:
        out = []
        while True:
            tok = self.peek()
            if tok is None:
                self.error("expected ')'")
            if tok.kind == "rparen":
                return out
            out.append(self.parse_node())

    def parse_type_list(self) -> list[Any]:
        self.take("lparen", "'('")
        out = []
        while True:
            tok = self.peek()
            if tok is None:
                self.error("expected ')'")
            if tok.kind == "rparen":
                self.i += 1
                return out
            out.append(self.parse_type())

    def parse_name_like(self, what: str) -> str:
        tok = self.peek()
        if tok is None or tok.kind not in ("word", "hash", "string"):
            self.error(f"expected {what}")
        self.i += 1
        return tok.value

    def parse_type(self) -> Any:
        tok = self.peek()
        if tok is None:
            self.error(_TYPE_HELP)
        if tok.kind == "lparen" and self.word_at(1) and self.peek(1).value in _CTORS:
            # sugar: `(result unit)` / `(option i64)` / `(list T)` mean `result<unit>` ...
            self.i += 1
            name = self.take("word", "a type constructor").value
            inner = self.parse_type()
            nxt = self.peek()
            if name == "result" and nxt is not None and nxt.kind == "word":
                self.i += 1
                if nxt.value != "error":
                    self.error("the failure type of 'result' is always 'error'")
            self.take("rparen", f"')' closing ({name} ...)")
            return {name: inner}
        if tok.kind == "string":
            self.i += 1
            return {"rec": tok.value}
        if tok.kind == "hash":
            self.i += 1
            return {"rec": tok.value}
        if tok.kind != "word":
            self.error(_TYPE_HELP)
        name = tok.value
        if name in _PRIMS:
            self.i += 1
            return name
        if name in _CTORS:
            self.i += 1
            self.take("lt", f"'<' after {name!r}")
            inner = self.parse_type()
            if name == "result":
                nxt = self.peek()
                if nxt is not None and nxt.kind == "word":
                    self.i += 1
                    if nxt.value != "error":
                        self.error("the failure type of 'result' is always 'error'")
            self.take("gt", f"'>' closing {name!r}")
            return {name: inner}
        self.i += 1
        return {"rec": name}


def _strip_id(token: str) -> str:
    return token.split("#", 1)[0]


def from_text(src: str) -> dict:
    """Parse textual IR into an IR node, or into a ``{name: node}`` mapping of definitions.

    A single bare expression (not a named definition) yields its node dict; one or more
    ``(fn name ...)`` / ``name = ...`` definitions yield a mapping, in the order written. A
    trailing ``#<hex>`` on a name (the addressing suffix ``to_text`` adds) is ignored.
    """
    if not isinstance(src, str):
        raise TypeError("from_text expects a string")
    toks, eof = _tokenize(src)
    parser = _Parser(toks, eof)
    forms = []
    while parser.peek() is not None:
        forms.append(parser.parse_form())
    if not forms:
        raise TextSyntaxError("empty input", eof[0], eof[1])
    if all(form.kind == "node" for form in forms):
        if len(forms) > 1:
            first = forms[1]
            raise TextSyntaxError(
                "several bare expressions; wrap each one in a named definition",
                first.line,
                first.col,
            )
        return forms[0].node
    for form in forms:
        if form.kind == "node":
            raise TextSyntaxError(
                "a bare expression cannot be mixed with named definitions", form.line, form.col
            )
    out: dict[str, dict] = {}
    for form in forms:
        if form.name in out:
            raise TextSyntaxError(f"duplicate definition {form.name!r}", form.line, form.col)
        out[form.name] = form.node
    return out
