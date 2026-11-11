"""show: render a stored IR subgraph as readable TypeScript-like pseudocode.

Example for a factorial graph:

    fn fact(p0: i64): i64 { return p0 < 2 ? 1 : p0 * self(p0 - 1); }

Conventions:
- functions referenced by `call` are printed once each, dependencies first,
  and the root `fn` (when the root IS an `fn`) goes last;
- names come from `name_of(h)` when available, otherwise the first 8 hex
  characters of the digest (a textual id is `b3:<64 hex>`; the `b3:` prefix is
  dropped for display);
- signatures come from the fn's declared `params`/`ret` (ADR-0004): each
  parameter prints as `p<i>: <type>` and the return type follows the parens,
  e.g. `fn f(p0: i64, p1: bool): bool { ... }`; a malformed fn (no types)
  falls back to bare names and omits the return type;
- `selfcall` renders as `self(...)`;
- boolean nodes render as TypeScript: literals `true`/`false`, `not{a}` as
  `!a`, `and{a,b}` as `a && b`, `or{a,b}` as `a || b`, with short-circuit
  semantics implied (as in TypeScript). `if` keeps the ternary form, its
  condition is always boolean;
- strings (ADR-0005) render as `"..."` with `\n`, `\"`, `\\` and `\\u{..}`
  (lowercase hex, no padding) for the remaining non-printable code points;
  a stored string is NFC by construction, so the projection never shows a
  combining character;
- `concat{a,b}` renders as the infix `"a" + "b"` ONLY when both sides are
  self-evidently strings (their projected text starts with a quote):
  anywhere else it would be ambiguous against arithmetic, so the
  functional `concat(a, b)` is used instead. Infix concat shares `+`'s
  precedence and associativity;
- `length{a}` renders `length(a)`, `repeat{s,n}` renders `repeat(s, n)`,
  `streq{a,b}` renders as the infix `a === b` (same precedence as `==`);
- atoms render as `:name`, and the option/result constructors render as
  calls: `none()`, `some(v)`, `ok(v)`, `err(v)` (the annotations of
  `none{t}`/`err{v,t}` are dropped: they live in the signature and the
  verifier, not in the projection) and `error{code, detail}` renders
  `error(code, detail)`;
- records (ADR-0006) render with field names when the optional
  `field_names(decl_hash)` callable provides them (a list of names per
  declaration, via `show`'s `field_names` parameter): `mk` renders
  `nota { titulo: ..., cuerpo: ... }` with the tag of its referenced
  declaration, `get{r, i}` renders `r.titulo`, and `with{r, i, v}` renders
  the copy as `{ ...r, titulo: v }`. Without names (callable absent, or it
  returns None / a shorter list) the projection falls back to POSITIONS
  (`nota { 0: ..., 1: ... }`, `r.#1`, `{ ...r, #1: v }`): the stored graph
  only carries positions, exactly like the identity encoding. An unknown
  declaration degrades to `rec#<8 hex>` text; nothing raises;
- lists (ADR-0007) render as TypeScript arrays: `list{items}` as `[a, b]`,
  and an EMPTY literal reveals its annotated element type: `[] as list<i64>`
  (the annotation is part of the node, but only the empty spelling cannot
  tell its element type from the items). `unit` renders `()`;
  `len{l}` renders the call `len(l)`, `at{l, idx}` renders the TypeScript
  index `l[3]` — whose result is an `option<T>` (an out-of-range read is
  `none`, never a trap; the projection does NOT annotate that type because
  it lives in the signature and the following `match`) —, and
  `append{a, b}` / `reverse{l}` stay functional: `append(a, b)`,
  `reverse(l)`;
- combinators (ADR-0007) render as builtin calls with the referenced fn BY
  NAME, `ctx` visible in its place: `map(l, validar, ctx)`,
  `fold(l, init, sumar, ctx)`, `sort_by(l, fecha, ctx)`,
  `and_then(r, seguir, ctx)`. The name comes from `name_of` applied to the
  `fn` that the `f` argument's `fnref` points to (the same callable that
  names `call` targets); without a stored name the fallback is
  `fn#<8 hex>`, never an exception. A `fnref` found STANDALONE (an escaped
  use: E_FNREF_ESCAPE at check time) renders `fnref name` so it cannot be
  read as a value, and the fn a combinator applies is printed — like
  `call` targets — before the root;
- effect nodes (ADR-0008) render as a typed namespace object with the
  node's `ns` scalar in the object position: `kv_get{ns, key}` as
  `kv.notas.get("primer")`, `kv_put{ns, key, v}` as `kv.notas.put("primer",
  v)`, `kv_del` as `kv.notas.delete("primer")`, `kv_list{ns, prefix}` as
  `kv.notas.list("n")`, `kv_keys` as `kv.notas.keys("n")`; `now{}` as
  `now()` and `print{s}` as `print(s)`;
- fn signatures carry the DERIVED effects suffix, right after the return
  type: `fn listar(p0: string): option<nota>!{kv.read@notas} { ... }`.
  Effects never enter the hash (they are derived, INV-05), so `show`
  derives them exactly as the checker does: the union of every literal
  effect reachable from the body (kv.read@ns / kv.write@ns / clock@host /
  print@stdout), following `call` and `fnref` into other fns and `selfcall`
  back into itself — sorted alphabetically, without repeats; a pure fn
  gets no suffix, a malformed ns renders `<?>` and nothing raises;
- `match{s,arms}` renders as a one-line block with the CANONICAL constructor
  names, e.g. `match (s) { none => 0, some(v0) => v0 }`. The constructor
  family comes from the scrutinee when it is literally a constructor node
  (none/some -> [none, some], ok/err -> [ok, err]), else from the arm roles
  (an arm using the `var{0}` payload binder must be a payload arm; the first
  canonical arm binds only for [ok, err]). A payload arm's binder displays
  as `v{d}` — the next free absolute level, like a `let` there — and the
  arm body is projected with that binder added;
- infix operators (`bin`, `and`, `or`, `not`, `streq`) take minimal
  parentheses by precedence `!` > `*` `/` `%` > `+` `-` > `<` > `==` /
  `===` > `&&` > `||` (lt binds tighter than eq, as the spec says; `div`
  and `rem` render as `/` and `%`), left associative, and a nested `if`
  (ternary) used as an operand gets parentheses; an operand that would
  parse differently unparenthesized (same precedence on the right of a
  left-associative operator) keeps parens;
- `let{v, body}` bindings are anonymous (De Bruijn), but the projection gives
  them display names by their ABSOLUTE level inside the enclosing `fn`: the
  outermost let of the function is `v0`, the next spine let is `v1`, and a
  `var{i}` under `d` enclosing lets renders as `v{d - 1 - i}` (var 0 reads the
  innermost let, which carries the highest index). When a let sits somewhere
  it cannot attach to an outer statement list — a branch of an `if`, an
  operand of a bin, the value of another let — it projects as an
  immediately-invoked arrow `(() => { const v0 = ...; return ...; })()`:
  - deterministic (same node -> same text, hence re-parseable and diffable);
  - keeps each branch's bindings in their own evaluable scope with no need to
    invent control-flow syntax (let-in-ternary would need it otherwise);
  - reuses the same `v{n}` naming (the counter keeps counting into the IIFE),
    so the display name of a binding is a pure function of how many lets
    enclose it, exactly mirroring the De Bruijn index of `var`.
- unknown children render as `<unknown:xxxxxxxx>`; a `var` whose index points
  beyond the enclosing lets (E_BAD_VAR at check time) renders as `<var:i>`.
  `show` is a projection for humans, so a missing reference must never raise.
"""

from contextvars import ContextVar

from .ir import CHILD_FIELDS, digest_hex, is_hash

# Parameter types of the fn being rendered: a `get`/`with` on a parameter then shows its field
# by name (the node itself only holds the position; the declaration lives in the signature).
_FN_PARAMS: ContextVar = ContextVar("show_fn_params", default=())

# Infix text and precedence (higher binds tighter); _ATOM = self-delimiting
# expressions (literals, params, calls, IIFEs, match blocks) that never need
# surrounding parens. The scale follows the spec's chain `!` > `*` `/` `%` >
# `+` `-` > `<` > `==` / `===` > `&&` > `||` (TypeScript-like; note lt above eq).
_INFIX = {
    "add": "+",
    "sub": "-",
    "mul": "*",
    "div": "/",
    "rem": "%",
    "lt": "<",
    "eq": "==",
    "streq": "===",
    "and": "&&",
    "or": "||",
}
_INFIX_PREC = {
    "or": 1,
    "and": 2,
    "eq": 3,
    "streq": 3,
    "lt": 4,
    "add": 5,
    "sub": 5,
    "mul": 6,
    "div": 6,
    "rem": 6,
}
_NOT = "!"
_NOT_PREC = 7
_ATOM = 8
_TERNARY = 0
# Canonical arm constructors per scrutinee family (position, not content):
# option<T> -> [none, some]; result<T, error> -> [ok, err] (ADR-0005).
_OPTION_ARMS = ("none", "some")
_RESULT_ARMS = ("ok", "err")


def _head(ref) -> str:
    """8-char display prefix of an id: the digest part when well formed."""
    if isinstance(ref, str):
        return digest_hex(ref)[:8] if is_hash(ref) else ref[:8]
    return "??"


def _unknown(ref) -> str:
    return f"<unknown:{_head(ref)}>"


def _label(h, name_of) -> str:
    """Human-readable name for an id: given name or 8-char digest prefix."""
    given = name_of(h) if name_of else None
    return given if given else _head(h)


def _load(get, child):
    """Resolve a child (hash or inline dict) to a node dict, or None."""
    if isinstance(child, dict):
        return child if child.get("k") in CHILD_FIELDS else None
    if is_hash(child):
        node = get(child)
        return node if isinstance(node, dict) else None
    return None


def _int(x) -> bool:
    """Shared scalar-shape test (booleans are not integers here)."""
    return isinstance(x, int) and not isinstance(x, bool)


def _quote(s) -> str:
    """Project a string literal value with TypeScript-like escapes.

    `\\n`, `\\\"` and `\\\\` get their classic escapes; any other
    non-printable code point becomes `\\u{..}` (lowercase hex, no padding).
    Stored strings are NFC, so combining marks never reach this path.
    """
    if not isinstance(s, str):
        return "<str:?>"
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch.isprintable():
            out.append(ch)
        else:
            out.append(f"\\u{{{ord(ch):x}}}")
    return '"' + "".join(out) + '"'


def _type_text(t, get=None) -> str:
    """Display spelling of a JSON type for signatures: `option<T>` / `result<T>`
    (the constructor spelling of ADR-0005), `list<T>` (ADR-0007), `unit`
    verbatim, primitives verbatim, and — since ADR-0006 — `{"rec": id}` as the
    tag of its stored declaration ('nota'; types render as a bare tag: the
    `#<8 hex>` stale-pointer suffix is typecheck's job, this projection shows
    what the declaration itself says). Anything malformed degrades to a
    readable marker instead of raising."""
    if isinstance(t, str):
        return t
    if isinstance(t, dict) and len(t) == 1:
        ((ctor, arg),) = t.items()
        if ctor == "rec":
            decl = _load(get, arg) if get else None
            tag = decl.get("tag") if isinstance(decl, dict) else None
            return tag if isinstance(tag, str) else f"rec#{_head(arg)}"
        if ctor == "list":
            return f"list<{_type_text(arg, get)}>"
        if ctor in ("option", "result"):
            return f"{ctor}<{_type_text(arg, get)}>"
    return "<type:?>" if t is not None else "?"


def _names_of(decl, get, field_names) -> list[str] | None:
    """Field names metadata for a record declaration (ADR-0006: outside the
    hash). `field_names(decl_hash)` is the optional `show` parameter; anything
    it fails to provide (None, not callable, not a list) degrades to
    positions, never raising."""
    if not callable(field_names) or not (isinstance(decl, str) and is_hash(decl)):
        return None
    try:
        names = field_names(decl)
    except Exception:
        return None
    return names if isinstance(names, list) else None


def _field_label(i, names) -> str:
    """Postfix form: `titulo` when position i has a name in the metadata,
    else `#i` (in a dot access a bare `1` would read like a number)."""
    if isinstance(names, list) and isinstance(i, int) and 0 <= i < len(names):
        name = names[i]
        if isinstance(name, str):
            return name
    return f"#{i}"


def _slot_label(i, names) -> str:
    """Braces form (mk / with): `titulo` when known, else the bare position
    (the ADR-0006 example is `nota { 0: ..., 1: ... }`)."""
    if isinstance(names, list) and isinstance(i, int) and 0 <= i < len(names):
        name = names[i]
        if isinstance(name, str):
            return name
    return str(i)


def _uses_binder(node, get, i: int) -> bool:
    """Whether the expression reads the binder added `i` binders deeper than
    `node` (i = 0 means "reads var{0} here", the match arm's payload query, for
    example). Lets and nested matches push binders; unknown children read as a
    miss. Never raises."""
    node = node if isinstance(node, dict) and node.get("k") in CHILD_FIELDS else _load(get, node)
    if node is None:
        return False
    kind = node.get("k")
    if kind == "var":
        return node.get("i") == i
    if kind == "let":
        return _uses_binder(node.get("body"), get, i + 1)
    if kind == "match":
        if _uses_binder(node.get("s"), get, i):
            return True
        bits = node.get("arms") or []
        return any(_uses_binder(a, get, i + 1) for a in bits)
    singles, lists = CHILD_FIELDS.get(kind, ((), ()))
    if any(_uses_binder(node.get(f), get, i) for f in singles):
        return True
    return any(_uses_binder(c, get, i) for f in lists for c in node.get(f) or [])


def _decl_of_receiver(r, get):
    """The record declaration a `get`/`with` receiver reveals, when it is
    literally built by `mk` (or a chain of `with` copies): the identity of the
    graph holds positions, so only that construction links field index to
    declaration. Anything else returns None (the field then renders by
    position). Never raises."""
    node = _load(get, r)
    if not isinstance(node, dict):
        return None
    kind = node.get("k")
    if kind == "mk":
        return node.get("decl")
    if kind == "with":
        return _decl_of_receiver(node.get("r"), get)
    if kind == "param":
        params, i = _FN_PARAMS.get(), node.get("i")
        if isinstance(i, int) and 0 <= i < len(params):
            t = params[i]
            if isinstance(t, dict) and isinstance(t.get("rec"), str):
                return t["rec"]
    return None


def _tag_of(decl, get) -> str:
    """Display tag of a record declaration (or `rec#<8 hex>` when it cannot
    be resolved — deterministic fallback, never raises)."""
    loaded = _load(get, decl)
    tag = loaded.get("tag") if isinstance(loaded, dict) else None
    return tag if isinstance(tag, str) else f"rec#{_head(decl)}"


def _fnref_text(f_ref, get, name_of) -> str:
    """Display name of the function a combinator's `f` stands for (ADR-0007):
    `f` is a `fnref{fn}` and the NAME belongs to the referenced `fn` node, so
    `name_of` is consulted with the fnref's target — the same callable that
    names `call` targets. Fallback `fn#<8 hex>`; anything malformed (not a
    fnref, missing fn) degrades to the fallback and never raises."""
    node = _load(get, f_ref)
    target = node.get("fn") if isinstance(node, dict) and node.get("k") == "fnref" else None
    if isinstance(target, str) and is_hash(target):
        given = name_of(target) if name_of else None
        return given if given else f"fn#{_head(target)}"
    return "fn#??"


def _fnref_target(node, get):
    """The `fn` hash a combinator's `f` (a stored `fnref`) applies, or None
    if the argument is malformed — collection guard, never raises."""
    node = _load(get, node) if not isinstance(node, dict) else node
    f_node = _load(get, node.get("f")) if isinstance(node, dict) else None
    target = f_node.get("fn") if isinstance(f_node, dict) and f_node.get("k") == "fnref" else None
    return target if isinstance(target, str) and is_hash(target) else None


def _node_effect(node) -> str | None:
    """The literal effect a node derives (ADR-0008): kv.read@ns for the KV
    reads, kv.write@ns for the writes, clock@host for `now` and print@stdout
    for `print`. None for every other kind and for a malformed `ns`."""
    kind = node.get("k")
    if kind in ("kv_get", "kv_list", "kv_keys", "kv_put", "kv_del"):
        ns = node.get("ns")
        if isinstance(ns, str) and ns:
            action = "kv.read@" if kind in ("kv_get", "kv_list", "kv_keys") else "kv.write@"
            return action + ns
    if kind == "now":
        return "clock@host"
    if kind == "print":
        return "print@stdout"
    return None


def _derive_effects(node, get) -> set[str]:
    """Effect set of an `fn` (ADR-0008): the union of every literal effect
    reachable from its body, following `call` and `fnref` into other fns and
    `selfcall` back into itself. Because `selfcall` denotes the same body
    already being walked, the closure IS the fixed point (monotone union).
    Malformed nodes contribute nothing; never raises."""
    effects: set[str] = set()
    stack = [node]
    fns_seen: set[str] = set()
    while stack:
        cur = stack.pop()
        if not isinstance(cur, dict) or cur.get("k") not in CHILD_FIELDS:
            continue
        effect = _node_effect(cur)
        if effect:
            effects.add(effect)
        kind = cur.get("k")
        if kind in ("call", "fnref"):
            target = cur.get("fn")
            if isinstance(target, str) and is_hash(target) and target not in fns_seen:
                fns_seen.add(target)
                stack.append(_load(get, target))
        singles, lists = CHILD_FIELDS[kind]
        for field in singles:
            if field == "fn":
                continue  # call/fnref targets: walked above, once
            stack.append(_load(get, cur.get(field)))
        for field in lists:
            for child in cur.get(field) or []:
                stack.append(_load(get, child))
    return effects


def _child(child, get, name_of, parent=None, d=0, field_names=None):
    """Render a child; `d` is the number of enclosing lets, and `parent =
    (precedence limit, on the right side?)` adds parentheses when the child
    binds weaker than the parent (or equally, on the right side of a
    left-associative operator)."""
    node = _load(get, child)
    if node is None:
        return _unknown(child)
    text, prec = _expr(node, get, name_of, d, field_names)
    if parent is not None:
        limit, right = parent
        if prec < limit or (right and prec == limit):
            return f"({text})"
    return text


def _match(node, get, name_of, d, field_names=None) -> str:
    """Project `match{s, arms}` as `match (s) { none => e0, some(v0) => e1 }`.

    Names are positional (canonical order, ADR-0005): the scrutinee's own
    constructor names the family when it is literally a constructor node
    (none/some -> [none, some]; ok/err -> [ok, err]), else the binding usage
    decides (arm 0 binds only in the result family). A payload arm displays
    its binder as `v{d}` — the next free absolute level, exactly what a
    `var{0}` inside the arm body reads — and is projected with one more
    enclosing binder; the `none` arm adds none. Malformed arm counts keep
    rendering positionally; this never raises."""
    scrut = _load(get, node.get("s"))
    fam = None
    if scrut is not None:
        fam = {
            "none": _OPTION_ARMS,
            "some": _OPTION_ARMS,
            "ok": _RESULT_ARMS,
            "err": _RESULT_ARMS,
        }.get(scrut.get("k"))
    arms = node.get("arms") or []
    if fam is None:
        fam = _RESULT_ARMS if (arms and _uses_binder(arms[0], get, 0)) else _OPTION_ARMS
    parts = []
    for i, arm in enumerate(arms):
        ctor = fam[i] if i < len(fam) else f"arm{i}"
        if ctor == "none":
            body = _child(arm, get, name_of, d=d, field_names=field_names)
            parts.append(f"none => {body}")
        else:
            payload = f"v{d}"
            body = _child(arm, get, name_of, d=d + 1, field_names=field_names)
            parts.append(f"{ctor}({payload}) => {body}")
    return (
        f"match ({_child(node.get('s'), get, name_of, d=d, field_names=field_names)}) "
        f"{{ {', '.join(parts)} }}"
    )


def _expr(node, get, name_of, d=0, field_names=None) -> tuple[str, int]:
    """Render an expression node into (text, precedence)."""
    kind = node.get("k")
    if kind == "lit":
        return str(node["v"]), _ATOM
    if kind in ("true", "false"):
        return kind, _ATOM
    if kind == "str":
        return _quote(node.get("v")), _ATOM
    if kind == "param":
        return f"p{node['i']}", _ATOM
    if kind == "var":
        # De Bruijn read: var 0 is the innermost enclosing let (name v{d-1}).
        i = node.get("i")
        if _int(i) and 0 <= i < d:
            return f"v{d - 1 - i}", _ATOM
        return f"<var:{i if _int(i) else '?'}>", _ATOM
    if kind == "not":
        # Prefix like TypeScript: parens only when the operand binds weaker
        # than `!` itself (e.g. `!(a && b)`, `!(1 == 2)`).
        operand = _child(
            node["a"], get, name_of, parent=(_NOT_PREC, False), d=d, field_names=field_names
        )
        return _NOT + operand, _NOT_PREC
    if kind in ("bin", "and", "or", "streq"):
        # bin carries its operator in `op`; and/or/streq are the kind itself.
        op = node.get("op", "streq" if kind == "streq" else kind)
        op_text = _INFIX.get(op)
        if op_text is None or op not in _INFIX_PREC:
            return "<unknown:?>", _ATOM  # malformed operator: keep showing, don't raise
        prec = _INFIX_PREC[op]
        left = _child(node["a"], get, name_of, parent=(prec, False), d=d, field_names=field_names)
        right = _child(node["b"], get, name_of, parent=(prec, True), d=d, field_names=field_names)
        return f"{left} {op_text} {right}", prec
    if kind == "concat":
        # Infix `a + b` only when unambiguous (both sides project as strings);
        # anywhere else the functional form keeps `+` arithmetic-only.
        left = _child(node["a"], get, name_of, d=d, field_names=field_names)
        right = _child(node["b"], get, name_of, d=d, field_names=field_names)
        if left.startswith('"') and right.startswith('"'):
            parens_l = _child(
                node["a"], get, name_of, parent=(5, False), d=d, field_names=field_names
            )
            parens_r = _child(
                node["b"], get, name_of, parent=(5, True), d=d, field_names=field_names
            )
            return f"{parens_l} + {parens_r}", 5
        return f"concat({left}, {right})", _ATOM
    if kind in ("none", "some", "ok", "err"):
        # nullary/1-ary constructor calls; none{t}/err{v,t} drop their
        # annotation (it is part of the identity, not of the readable text).
        arg = ", ".join(
            _child(node[f], get, name_of, d=d, field_names=field_names) for f in ("v",) if f in node
        )
        return f"{kind}({arg})", _ATOM
    if kind == "length":
        return f"length({_child(node['a'], get, name_of, d=d, field_names=field_names)})", _ATOM
    if kind == "repeat":
        s = _child(node["s"], get, name_of, d=d, field_names=field_names)
        n = _child(node["n"], get, name_of, d=d, field_names=field_names)
        return f"repeat({s}, {n})", _ATOM
    if kind == "match":
        return _match(node, get, name_of, d, field_names), _ATOM
    if kind == "atom":
        name = node.get("name")
        return f":{name}" if isinstance(name, str) else ":<?>", _ATOM
    if kind == "error":
        code = _child(node["code"], get, name_of, d=d, field_names=field_names)
        detail = _child(node["detail"], get, name_of, d=d, field_names=field_names)
        return f"error({code}, {detail})", _ATOM
    if kind == "mk":
        # `nota { titulo: ..., cuerpo: ... }` with the declaration's field
        # names; without them the positions themselves are the labels, since
        # the stored graph (like the identity encoding) only carries those.
        decl = node.get("decl")
        names = _names_of(decl, get, field_names)
        tag = _tag_of(decl, get)
        if not isinstance(node.get("fields"), list):
            return f"{tag} {{}}", _ATOM
        parts = []
        for i, f in enumerate(node["fields"]):
            label = _slot_label(i, names)
            value = _child(f, get, name_of, d=d, field_names=field_names)
            parts.append(f"{label}: {value}")
        return f"{tag} {{ {', '.join(parts)} }}" if parts else f"{tag} {{}}", _ATOM
    if kind == "get":
        # Postfix, TypeScript-like: it binds tighter than every operator, so
        # the receiver gets parens unless it is self-delimiting (_ATOM). Field
        # names need the record's DECLARATION, which the graph only carried at
        # construction: `mk` reveals it and a `with` copies it; a variable
        # receiver cannot, so names degrade to positions there (the type
        # information lives outside the projection's reach).
        i = node.get("i")
        names = _names_of(_decl_of_receiver(node.get("r"), get), get, field_names)
        label = f".{_field_label(i, names)}"
        receiver = _child(
            node["r"], get, name_of, parent=(_ATOM, False), d=d, field_names=field_names
        )
        return f"{receiver}{label}", _ATOM
    if kind == "with":
        i = node.get("i")
        r = _child(node["r"], get, name_of, d=d, field_names=field_names)
        names = _names_of(_decl_of_receiver(node.get("r"), get), get, field_names)
        label = _slot_label(i, names)
        value = _child(node["v"], get, name_of, d=d, field_names=field_names)
        return f"{{ ...{r}, {label}: {value} }}", _ATOM
    if kind == "rec":
        # A declaration renders only where it can be a root (child of `mk` is
        # identity machinery): `type nota = { 0: string, 1: string }`, fields
        # positional (names live in the metadata, not in this node).
        tag = node.get("tag") if isinstance(node.get("tag"), str) else "rec#<?>"
        fields = node.get("fields")
        if not isinstance(fields, list):
            return f"type {tag} = {{}}", _ATOM
        parts = [f"{i}: {_type_text(t, get)}" for i, t in enumerate(fields)]
        return f"type {tag} = {{ {', '.join(parts)} }}" if parts else f"type {tag} = {{}}", _ATOM
    if kind == "unit":
        # The single value of `unit` (ADR-0007): a characterless pair of
        # parentheses, like TypeScript's empty tuple literal.
        return "()", _ATOM
    if kind == "list":
        # `[a, b]`; an EMPTY literal projects its annotated element type —
        # `[] as list<i64>` — because only that spelling cannot reveal `t`
        # from the items. The annotation is part of the node either way.
        items = node.get("items")
        if isinstance(items, list) and items:
            parts = [_child(x, get, name_of, d=d, field_names=field_names) for x in items]
            return f"[{', '.join(parts)}]", _ATOM
        return f"[] as list<{_type_text(node.get('t'), get)}>", _ATOM
    if kind == "len":
        lst = _child(node["l"], get, name_of, d=d, field_names=field_names)
        return f"len({lst})", _ATOM
    if kind == "at":
        # TypeScript indexing, postfix like `get`: it binds tighter than
        # every operator, so a non-self-delimiting receiver takes parens.
        # The result is an option<T> (out of range = `none`, never a trap);
        # the type is NOT annotated here — the signature and the following
        # `match` carry it.
        receiver = _child(
            node["l"], get, name_of, parent=(_ATOM, False), d=d, field_names=field_names
        )
        idx = _child(node["idx"], get, name_of, d=d, field_names=field_names)
        return f"{receiver}[{idx}]", _ATOM
    if kind == "append":
        lst_a = _child(node["a"], get, name_of, d=d, field_names=field_names)
        lst_b = _child(node["b"], get, name_of, d=d, field_names=field_names)
        return f"append({lst_a}, {lst_b})", _ATOM
    if kind == "reverse":
        lst = _child(node["l"], get, name_of, d=d, field_names=field_names)
        return f"reverse({lst})", _ATOM
    if kind == "fnref":
        # Standalone fnref (an escaped use: E_FNREF_ESCAPE at check time):
        # keep it visibly NOT a value — the combinator branches below render
        # the same name WITHOUT the wrapper when it is their `f`.
        return f"fnref {_fnref_text(node, get, name_of)}", _ATOM
    if kind in ("map", "filter", "sort_by"):
        # Combinators read like calls whose second argument is the fn NAME
        # (ADR-0007): `map(l, validar, ctx)`; the ctx is always there.
        lst = _child(node["l"], get, name_of, d=d, field_names=field_names)
        fn_name = _fnref_text(node.get("f"), get, name_of)
        ctx = _child(node["ctx"], get, name_of, d=d, field_names=field_names)
        return f"{kind}({lst}, {fn_name}, {ctx})", _ATOM
    if kind == "fold":
        # fold carries the accumulator before the fn: `fold(l, init, f, ctx)`.
        lst = _child(node["l"], get, name_of, d=d, field_names=field_names)
        init = _child(node["init"], get, name_of, d=d, field_names=field_names)
        fn_name = _fnref_text(node.get("f"), get, name_of)
        ctx = _child(node["ctx"], get, name_of, d=d, field_names=field_names)
        return f"fold({lst}, {init}, {fn_name}, {ctx})", _ATOM
    if kind == "and_then":
        res = _child(node["r"], get, name_of, d=d, field_names=field_names)
        fn_name = _fnref_text(node.get("f"), get, name_of)
        ctx = _child(node["ctx"], get, name_of, d=d, field_names=field_names)
        return f"and_then({res}, {fn_name}, {ctx})", _ATOM
    if kind in ("kv_get", "kv_put", "kv_del", "kv_list", "kv_keys"):
        # A namespaced host-object call (ADR-0008): the node's `ns` scalar is
        # the object, the operation the method — `kv.notas.get("primer")`.
        # Self-delimiting like every other call: max precedence.
        ns = node.get("ns")
        op = {
            "kv_get": "get",
            "kv_put": "put",
            "kv_del": "delete",
            "kv_list": "list",
            "kv_keys": "keys",
        }[kind]
        fields = {
            "kv_get": ("key",),
            "kv_put": ("key", "v"),
            "kv_del": ("key",),
            "kv_list": ("prefix",),
            "kv_keys": ("prefix",),
        }[kind]
        args = ", ".join(
            _child(node.get(f), get, name_of, d=d, field_names=field_names) for f in fields
        )
        return f"kv.{ns if isinstance(ns, str) else '<?>'}.{op}({args})", _ATOM
    if kind == "now":
        # One value per invocation; the injection is invisible here.
        return "now()", _ATOM
    if kind == "print":
        return f"print({_child(node.get('s'), get, name_of, d=d, field_names=field_names)})", _ATOM
    if kind == "if":
        cond = _child(node["c"], get, name_of, parent=(0, True), d=d, field_names=field_names)
        yes = _child(node["t"], get, name_of, parent=(0, True), d=d, field_names=field_names)
        no = _child(node["e"], get, name_of, d=d, field_names=field_names)
        # else-chained ternary reads fine bare
        return f"{cond} ? {yes} : {no}", _TERNARY
    if kind == "call":
        target = node["fn"]
        if isinstance(target, str) and is_hash(target):
            label = _label(target, name_of)
        else:
            label = "fn"  # inline function node: no stored name to show
        args = ", ".join(
            _child(a, get, name_of, d=d, field_names=field_names) for a in node["args"]
        )
        return f"{label}({args})", _ATOM
    if kind == "selfcall":
        args = ", ".join(
            _child(a, get, name_of, d=d, field_names=field_names) for a in node["args"]
        )
        return f"self({args})", _ATOM
    if kind == "let":
        # Not on an fn-body spine -> IIFE (see module docstring): a one-node let
        # peels into consts of its own block, whatever encloses it.
        stmts: list[str] = []
        last = _body_stmts(node, get, name_of, d, stmts, field_names)
        stmts.append(f"return {last};")
        return "(() => { " + " ".join(stmts) + " })()", _ATOM
    return "<unknown:?>", _ATOM  # malformed node: keep showing, don't raise


def _body_stmts(child, get, name_of, d: int, stmts: list[str], field_names=None) -> str:
    """Peel a spine of lets into `const` statements and return the final
    expression. Used for fn bodies (spine -> const + return) and IIFE blocks;
    the let counter `d` keeps counting, so a binding's display name is a pure
    function of how many lets enclose it."""
    node = _load(get, child)
    if node is not None and node.get("k") == "let":
        value = _child(node.get("v"), get, name_of, d=d, field_names=field_names)
        stmts.append(f"const v{d} = {value};")
        return _body_stmts(node.get("body"), get, name_of, d + 1, stmts, field_names)
    return _child(child, get, name_of, d=d, field_names=field_names)


def _fn(node, get, name_of, h, field_names=None):
    """Render an fn node as `fn NAME(p0: i64, p1: bool): bool { ... }` per its
    declared `params`/`ret` (ADR-0004). Parameters print as `p<i>: <type>` and
    the declared return type follows the parens; a malformed fn (missing
    types) falls back to bare names and omits the return type.

    A body made of plain expressions keeps the one-line form; a body whose
    spine carries lets becomes a block with one `const` line per let."""
    name = _label(h, name_of) if h else "fn"
    params = node.get("params")
    if not isinstance(params, list):
        params = []
    args = ", ".join(f"p{i}: {_type_text(t, get)}" for i, t in enumerate(params))
    ret = _type_text(node.get("ret"), get)
    effects = sorted(_derive_effects(node, get))
    suffix = f"!{{{', '.join(effects)}}}" if effects else ""
    tail = f"): {ret}{suffix}" if isinstance(node.get("ret"), (str, dict)) else ")"
    stmts: list[str] = []
    token = _FN_PARAMS.set(params)
    try:
        last = _body_stmts(node.get("body"), get, name_of, 0, stmts, field_names)
    finally:
        _FN_PARAMS.reset(token)
    stmts.append(f"return {last};")
    if len(stmts) == 1:
        return f"fn {name}({args}{tail} {{ {stmts[0]} }}"
    body = "\n".join(f"  {s}" for s in stmts)
    return f"fn {name}({args}{tail} {{\n{body}\n}}"


def _collect(get, node, root_h, order: list, seen: set) -> None:
    """Collect hashes of `fn` nodes reachable through `call` and through the
    `fnref` a combinator applies (ADR-0007), pre-order, registering each once;
    recursion over fn bodies finds the nested ones."""
    kind = node.get("k")
    if kind == "call":
        target = node["fn"]
        if is_hash(target) and target != root_h and target not in seen:
            fn_node = _load(get, target)
            if fn_node is not None:
                seen.add(target)
                order.append(target)
                _collect(get, fn_node, root_h, order, seen)
    if kind in ("map", "filter", "fold", "and_then", "sort_by"):
        target = _fnref_target(node, get)
        if target is not None and target != root_h and target not in seen:
            fn_node = _load(get, target)
            if fn_node is not None and fn_node.get("k") == "fn":
                seen.add(target)
                order.append(target)
                _collect(get, fn_node, root_h, order, seen)
    singles, lists = CHILD_FIELDS.get(kind, ((), ()))
    for field in singles:
        if kind == "call" and field == "fn":
            continue  # already handled above as a referenced definition
        _follow(get, node.get(field), root_h, order, seen)
    for field in lists:
        for c in node.get(field) or []:
            _follow(get, c, root_h, order, seen)


def _follow(get, child, root_h, order: list, seen: set):
    child_node = _load(get, child)
    if child_node is not None:
        _collect(get, child_node, root_h, order, seen)


def show(get, h: str, name_of=lambda _h: None, field_names=None) -> str:
    """Project the subgraph rooted at `h` as TypeScript-like pseudocode.

    `get` maps a hash to its stored node dict (like Store.get); `name_of`
    optionally maps a hash to a human name and `field_names(decl_hash)`
    optionally returns the field-name metadata of a record declaration
    (ADR-0006). Unknown hashes never raise.
    """
    node = _load(get, h)
    if node is None:
        return _unknown(h)
    root_h = h if is_hash(h) else None
    order: list[str] = []
    _collect(get, node, root_h, order, set())
    parts = [
        _fn(_load(get, fn_hash), get, name_of, fn_hash, field_names) for fn_hash in reversed(order)
    ]
    if node["k"] == "fn":
        parts.append(_fn(node, get, name_of, root_h, field_names))
    else:
        text, _ = _expr(node, get, name_of, field_names=field_names)
        parts.append(text)
    return "\n".join(parts)
