"""Type checker for stored IR nodes.

Types are JSON values (ADR-0005/0007): the primitives "i64", "bool", "string",
"atom", "error" and "unit", plus {"option": T}, {"result": T} (E is always
"error" in v1), {"list": T} and {"rec": <declaration id>}. Nodes and functions
store the canonical string form produced by `render_type`, e.g. "option<string>",
"list<nota>", "result<i64,error>" or "fn(i64,string)->option<string>";
`parse_fn_type` turns a stored signature back into JSON types so they can be
compared structurally.

A node that does not depend on its context stores its concrete type; a node with
a free `param`, `var` or `selfcall` stores "open" (its type is only known against
an enclosing signature). Checking is per `fn`, in a single pass against the
declared signature: `params` give the types of `param{i}`, the binder stack (the
enclosing `let`s and match payload arms) gives the types of `var{i}`, and the body
must match `ret`. A `fn` node is never entered from another body; `call` reads the
callee's stored signature instead.

Standalone nodes are typed by a lenient pass: when a binding that only an
enclosing `fn`/`let`/match can provide is needed, the node is "open". Inside a
`fn` the pass is strict, so an out-of-range `param`/`var` is E_BAD_PARAM/E_BAD_VAR.

Every `E_TYPE_MISMATCH`/`E_ARITY` detail names the role of the offending child in
its parent (`if.c`, `bin.a`, `let.body`, `match.s`, `arms[1]`, `call.args[0]`,
`fn.body`, ...) with the types involved: "if.c has type i64, expected bool".
`if`, `let` and `match` forward the expected type to the sub-expression that
decides their type, so the error points at the inner role instead of the whole
node.

The effect nodes of ADR-0008 (`kv_get`, `kv_put`, `kv_del`, `kv_list`, `kv_keys`,
`now`, `print`) type against the namespaces seeded by the spec, and a `fn`'s
stored type carries its derived effects as a trailing `!{a,b}` suffix (a pure
function has none).
"""

from . import ir
from .ir import GraphError

_PRIMS = ("i64", "bool", "string", "atom", "error", "unit")
_FNREF_COMBINATORS = ("map", "filter", "fold", "and_then", "sort_by")


class _NeedsContext(Exception):
    """A standalone node's type depends on an enclosing fn/let (its type is "open")."""


class _Env:
    """Typing context inside one `fn`: parameter types, return type, binder stack."""

    __slots__ = ("params", "ret", "lets", "strict")

    def __init__(self, params, ret, lets, strict):
        self.params = params
        self.ret = ret
        self.lets = lets
        self.strict = strict

    def push(self, t):
        """Bind one more enclosing value (innermost last) for the body."""
        return _Env(self.params, self.ret, [*self.lets, t], self.strict)


class _Ctx:
    """Per-`check` caches, the atom registry and the KV namespaces."""

    __slots__ = ("closed", "free", "atoms", "namespaces", "effects", "fn_effects")

    def __init__(self):
        self.closed = {}
        self.free = {}
        self.atoms = None
        self.namespaces = None
        self.effects = {}
        self.fn_effects = {}


# -- type strings ------------------------------------------------------------


def render_type(t, store=None) -> str:
    """Canonical string of a JSON type: "i64", "option<string>", "nota", "nota#ab12cd34", ...

    A nominal type is rendered with the label of its `rec` declaration; when the
    declaration is no longer the one its tag points to, the first 8 hex of its
    digest are appended so old values remain distinguishable.
    """
    if isinstance(t, str):
        return t
    if "option" in t:
        return f"option<{render_type(t['option'], store)}>"
    if "result" in t:
        return f"result<{render_type(t['result'], store)},error>"
    if "list" in t:
        return f"list<{render_type(t['list'], store)}>"
    return _render_nominal(t["rec"], store)


def _render_nominal(h, store) -> str:
    suffix = ir.digest_hex(h)[:8]
    if store is not None:
        node = store.get(h)
        if node is not None and node.get("k") == "rec":
            tag = node["tag"]
            if store.type_pointer(tag) == h:
                return tag
            return f"{tag}#{suffix}"
    return f"rec#{suffix}"


def fn_type(params, ret, store=None, effects=()) -> str:
    """The canonical string of a function type, with its derived effects if any.

    A pure function has no suffix; otherwise the effects are appended sorted and
    deduplicated: "fn(string)->option<nota>!{kv.read@notas}". Effects never appear
    on a parameter or return type, only on the whole function type.
    """
    base = "fn(" + ",".join(render_type(p, store) for p in params) + ")->" + render_type(ret, store)
    eff = sorted(set(effects))
    return base if not eff else base + "!{" + ",".join(eff) + "}"


def parse_effects(t) -> tuple:
    """Effect names of a stored fn type ("fn(...)->ret!{a,b}"); () when pure or not a fn."""
    if not isinstance(t, str):
        return ()
    core, sep, rest = t.partition("!")
    if not sep or not core.startswith("fn("):
        return ()
    if not (rest.startswith("{") and rest.endswith("}")):
        return ()
    body = rest[1:-1]
    return tuple(body.split(",")) if body else ()


def effects_of(type_text) -> list:
    """Effect names carried by a stored type's `!{...}` suffix, in written order.

    A pure type (or a non-function type) has no effects: the result is empty.
    """
    return list(parse_effects(type_text))


def parse_fn_type(t):
    """(params, ret) of a stored "fn(...)->ret" string, or None if it is not a fn.

    A trailing effect suffix (`!{...}`, ADR-0008) is ignored: the signature alone
    is what `call` needs.
    """
    if not isinstance(t, str):
        return None
    core = t.split("!", 1)[0]
    if not core.startswith("fn("):
        return None
    try:
        params, i = _parse_params(core, 3)
        if not core.startswith("->", i):
            return None
        ret, i = _parse_type(core, i + 2)
    except ValueError:
        return None
    if i != len(core):
        return None
    return params, ret


def _parse_params(s, i):
    params = []
    if s.startswith(")", i):
        return params, i + 1
    while True:
        t, i = _parse_type(s, i)
        params.append(t)
        if s.startswith(",", i):
            i += 1
        elif s.startswith(")", i):
            return params, i + 1
        else:
            raise ValueError("bad fn signature")


def _parse_type(s, i):
    if s.startswith("option<", i):
        inner, j = _parse_type(s, i + 7)
        if not s.startswith(">", j):
            raise ValueError("bad option type")
        return {"option": inner}, j + 1
    if s.startswith("list<", i):
        inner, j = _parse_type(s, i + 5)
        if not s.startswith(">", j):
            raise ValueError("bad list type")
        return {"list": inner}, j + 1
    if s.startswith("result<", i):
        inner, j = _parse_type(s, i + 7)
        if not s.startswith(",error>", j):
            raise ValueError("bad result type")
        return {"result": inner}, j + 7
    for p in _PRIMS:
        if s.startswith(p, i):
            return p, i + len(p)
    raise ValueError("bad type")


# -- entry point -------------------------------------------------------------


def check(store, node: dict) -> str:
    """Return the stored type of a node: a canonical "i64"/"bool"/"option<...>"/... or "open"."""
    where = _hash_of(node)
    ir.check_shape(node, where, inline=False)
    _validate_decl_refs(store, node, where)
    if node["k"] == "rec":
        return _check_rec(store, node, where)
    if node["k"] == "fn":
        return _check_fn(store, node)
    ctx = _Ctx()
    try:
        t = _infer(store, node, _Env([], None, [], False), ctx)
    except _NeedsContext:
        return "open"
    return render_type(t, store)


def _hash_of(node):
    try:
        return ir.node_hash(node)
    except (KeyError, TypeError, ValueError):
        return None


def _check_rec(store, node, where) -> str:
    """A `rec` declaration: its tag must be seedable or current; its type is the literal "decl".

    Deferred (ronda 5b): a *named root* whose signature mentions an `x_` tag should be
    rejected with E_TYPE_TAG_TAKEN when the name belongs to a spec. Auxiliary `x_`
    declarations are accepted here because only the spec registry can tell whether a
    name is ours; the check needs the service's name/spec knowledge, so it is left out.
    """
    tag = node["tag"]
    current = store.type_pointer(tag)
    if current is None:
        if not tag.startswith("x_"):
            raise GraphError(where, "E_UNKNOWN_TYPE_TAG", f"type tag '{tag}' is not seeded")
    elif current != where:
        raise GraphError(
            where, "E_TYPE_TAG_TAKEN", f"type tag '{tag}' is already declared as {current}"
        )
    return "decl"


def _check_fn(store, node) -> str:
    env = _Env(node["params"], node["ret"], [], True)
    ctx = _Ctx()
    body = _child(store, node["body"])
    _check(store, body, "fn.body", node["ret"], env, ctx, node["body"])
    return fn_type(node["params"], node["ret"], store, _fn_effects(store, node, ctx))


def _validate_decl_refs(store, node, where) -> None:
    """Every `{"rec": h}` in the node's own types must name a stored `rec` declaration."""
    types = [*node.get("params", []), node.get("ret"), node.get("t"), *node.get("fields", [])]
    for t in types:
        _validate_type(store, t, where)


def _validate_type(store, t, where) -> None:
    for ref in ir.type_refs(t):
        decl = store.get(ref)
        if decl is None:
            raise GraphError(where, "E_UNKNOWN_REF", f"type references unknown declaration {ref}")
        if decl["k"] != "rec":
            raise GraphError(
                ref, "E_TYPE_MISMATCH", f"type reference {ref} is not a rec declaration"
            )


def _child(store, h):
    nd = store.get(h)
    if nd is None:
        raise GraphError(h, "E_UNKNOWN_REF", f"{h} is not in the store")
    return nd


def _atoms(store, ctx):
    if ctx.atoms is None:
        ctx.atoms = set(store.atoms())
    return ctx.atoms


def _uses_context(store, node, ctx) -> bool:
    """True if typing `node` *may* need an enclosing fn (free param/var/selfcall).

    Deliberately conservative: a `var` is always treated as free, so this only
    ever *under*-reports context-freedom. It is used purely to decide whether a
    node's computed type may be cached; the precise "open" decision comes from the
    lenient pass in `check`. A distinct `fn` (and its `call` target) is
    context-free: its type is its own signature.
    """
    h = ir.node_hash(node)
    cached = ctx.free.get(h)
    if cached is not None:
        return cached
    kind = node["k"]
    if kind in ("param", "var", "selfcall"):
        out = True
    elif kind == "fn":
        out = False
    else:
        singles, lists = ir.CHILD_FIELDS[kind]
        out = False
        for field in singles:
            if kind == "call" and field == "fn":
                continue  # a fn target is a distinct, context-free value
            if _uses_context(store, _child(store, node[field]), ctx):
                out = True
                break
        if not out:
            for field in lists:
                for c in node[field]:
                    if _uses_context(store, _child(store, c), ctx):
                        out = True
                        break
                if out:
                    break
    ctx.free[h] = out
    return out


# -- checking against an expected type ---------------------------------------


def _check(store, node, role, want, env, ctx, h) -> None:
    """Require `node` (hash `h`) to have type `want`; name `role` in the error.

    `if`, `let` and `match` do not have a fixed type of their own, so they forward
    `want` to the sub-expression that decides the result and let that child raise.
    """
    kind = node["k"]
    if kind == "if":
        _expect(store, node["c"], "if.c", "bool", env, ctx)
        _check(store, _child(store, node["t"]), "if.t", want, env, ctx, node["t"])
        _check(store, _child(store, node["e"]), "if.e", want, env, ctx, node["e"])
        return
    if kind == "let":
        bound = _infer(store, _child(store, node["v"]), env, ctx, root=node["v"], role="let.v")
        if _is_var0(store, node["body"]):
            # The let's value is just the binding, so `want` constrains `v` too.
            _expect(store, node["v"], "let.v", want, env, ctx)
        _check(
            store,
            _child(store, node["body"]),
            "let.body",
            want,
            env.push(bound),
            ctx,
            node["body"],
        )
        return
    if kind == "match":
        for i, (arm, binder) in enumerate(_arm_plan(store, node, h, env, ctx)):
            aenv = env if binder is None else env.push(binder)
            _check(store, _child(store, arm), f"arms[{i}]", want, aenv, ctx, arm)
        return
    got = _infer(store, node, env, ctx, root=h, role=role)
    if got != want:
        raise GraphError(
            h,
            "E_TYPE_MISMATCH",
            f"{role} has type {render_type(got, store)}, expected {render_type(want, store)}",
        )


def _expect(store, h, role, want, env, ctx) -> None:
    """Require the child `h` to have type `want`, naming its role in the error."""
    _check(store, _child(store, h), role, want, env, ctx, h)


def _is_var0(store, h) -> bool:
    nd = store.get(h)
    return nd is not None and nd["k"] == "var" and nd["i"] == 0


# -- inference ---------------------------------------------------------------


def _infer(store, node, env, ctx, root=None, role=None):
    """Type of `node` under `env`, validating it. `root` is the child hash, if any.

    `role` names where the node sits in its parent. A `fnref` is not a value, so
    it is rejected with E_FNREF_ESCAPE as soon as it is typed at a role that is
    not a combinator's `f` (which is consumed without going through here).

    A context-free node has the same type in every environment, so it is computed
    once and cached. An `open` node is recomputed under each environment (the same
    subtree may be valid under one binder and not under another).
    """
    h = root if root is not None else ir.node_hash(node)
    if role is not None:
        _reject_fnref(h, node, role)
    cached = ctx.closed.get(h)
    if cached is not None:
        return cached
    kind = node["k"]
    if kind == "lit":
        t = "i64"
    elif kind in ("true", "false"):
        t = "bool"
    elif kind == "str":
        t = "string"
    elif kind == "param":
        i = node["i"]
        if not 0 <= i < len(env.params):
            if not env.strict:
                raise _NeedsContext()
            raise GraphError(h, "E_BAD_PARAM", f"param.i={i} outside 0..{len(env.params) - 1}")
        t = env.params[i]
    elif kind == "var":
        i = node["i"]
        if not 0 <= i < len(env.lets):
            if not env.strict:
                raise _NeedsContext()
            raise GraphError(
                h,
                "E_BAD_VAR",
                f"var.i={i} needs {i + 1} enclosing binder(s) but only {len(env.lets)} available",
            )
        t = env.lets[-1 - i]
    elif kind == "bin":
        _expect(store, node["a"], "bin.a", "i64", env, ctx)
        _expect(store, node["b"], "bin.b", "i64", env, ctx)
        t = "bool" if node["op"] in ("lt", "eq") else "i64"
    elif kind == "not":
        _expect(store, node["a"], "not.a", "bool", env, ctx)
        t = "bool"
    elif kind in ("and", "or"):
        _expect(store, node["a"], f"{kind}.a", "bool", env, ctx)
        _expect(store, node["b"], f"{kind}.b", "bool", env, ctx)
        t = "bool"
    elif kind == "concat":
        _expect(store, node["a"], "concat.a", "string", env, ctx)
        _expect(store, node["b"], "concat.b", "string", env, ctx)
        t = "string"
    elif kind == "length":
        _expect(store, node["a"], "length.a", "string", env, ctx)
        t = "i64"
    elif kind == "repeat":
        _expect(store, node["s"], "repeat.s", "string", env, ctx)
        _expect(store, node["n"], "repeat.n", "i64", env, ctx)
        t = "string"
    elif kind == "streq":
        _expect(store, node["a"], "streq.a", "string", env, ctx)
        _expect(store, node["b"], "streq.b", "string", env, ctx)
        t = "bool"
    elif kind == "atom":
        if node["name"] not in _atoms(store, ctx):
            raise GraphError(
                h, "E_UNKNOWN_ATOM", f"atom '{node['name']}' is not in the spec registry"
            )
        t = "atom"
    elif kind == "none":
        _validate_type(store, node["t"], h)
        t = {"option": node["t"]}
    elif kind == "some":
        v = _infer(store, _child(store, node["v"]), env, ctx, root=node["v"], role="some.v")
        t = {"option": v}
    elif kind == "ok":
        v = _infer(store, _child(store, node["v"]), env, ctx, root=node["v"], role="ok.v")
        t = {"result": v}
    elif kind == "err":
        _expect(store, node["v"], "err.v", "error", env, ctx)
        _validate_type(store, node["t"], h)
        t = {"result": node["t"]}
    elif kind == "error":
        _expect(store, node["code"], "error.code", "atom", env, ctx)
        _expect(store, node["detail"], "error.detail", {"option": "string"}, env, ctx)
        t = "error"
    elif kind == "if":
        _expect(store, node["c"], "if.c", "bool", env, ctx)
        branch = _infer(store, _child(store, node["t"]), env, ctx, root=node["t"], role="if.t")
        other = _infer(store, _child(store, node["e"]), env, ctx, root=node["e"], role="if.e")
        if branch != other:
            raise GraphError(
                h,
                "E_TYPE_MISMATCH",
                f"if.e has type {render_type(other, store)}, expected {render_type(branch, store)}",
            )
        t = branch
    elif kind == "let":
        bound = _infer(store, _child(store, node["v"]), env, ctx, root=node["v"], role="let.v")
        t = _infer(
            store,
            _child(store, node["body"]),
            env.push(bound),
            ctx,
            root=node["body"],
            role="let.body",
        )
    elif kind == "match":
        t = None
        for i, (arm, binder) in enumerate(_arm_plan(store, node, h, env, ctx)):
            aenv = env if binder is None else env.push(binder)
            at = _infer(store, _child(store, arm), aenv, ctx, root=arm, role=f"arms[{i}]")
            if t is None:
                t = at
            elif at != t:
                raise GraphError(
                    arm,
                    "E_TYPE_MISMATCH",
                    f"arms[{i}] has type {render_type(at, store)}, "
                    f"expected {render_type(t, store)}",
                )
    elif kind == "call":
        t = _infer_call(store, node, h, env, ctx)
    elif kind == "selfcall":
        if env.ret is None:
            if not env.strict:
                raise _NeedsContext()
            raise GraphError(h, "E_BAD_NODE", "selfcall outside a fn")
        _check_args(store, h, node["args"], env.params, env, ctx, what="selfcall")
        t = env.ret
    elif kind == "mk":
        t = _infer_mk(store, node, h, env, ctx)
    elif kind == "get":
        t = _infer_get(store, node, h, env, ctx)
    elif kind == "with":
        t = _infer_with(store, node, h, env, ctx)
    elif kind == "rec":
        t = "decl"
    elif kind == "unit":
        t = "unit"
    elif kind == "list":
        _validate_type(store, node["t"], h)
        for i, item in enumerate(node["items"]):
            _expect(store, item, f"list.items[{i}]", node["t"], env, ctx)
        t = {"list": node["t"]}
    elif kind == "len":
        lt = _infer(store, _child(store, node["l"]), env, ctx, root=node["l"], role="len.l")
        _require_list(store, lt, node["l"], "len.l")
        t = "i64"
    elif kind == "at":
        lt = _infer(store, _child(store, node["l"]), env, ctx, root=node["l"], role="at.l")
        elem = _require_list(store, lt, node["l"], "at.l")
        _expect(store, node["idx"], "at.idx", "i64", env, ctx)
        t = {"option": elem}
    elif kind == "append":
        ea = _require_list(
            store,
            _infer(store, _child(store, node["a"]), env, ctx, root=node["a"], role="append.a"),
            node["a"],
            "append.a",
        )
        eb = _require_list(
            store,
            _infer(store, _child(store, node["b"]), env, ctx, root=node["b"], role="append.b"),
            node["b"],
            "append.b",
        )
        if ea != eb:
            raise GraphError(
                node["b"],
                "E_TYPE_MISMATCH",
                f"append.b has type {render_type({'list': eb}, store)}, "
                f"expected {render_type({'list': ea}, store)}",
            )
        t = {"list": ea}
    elif kind == "reverse":
        t = _infer(store, _child(store, node["l"]), env, ctx, root=node["l"], role="reverse.l")
        _require_list(store, t, node["l"], "reverse.l")
    elif kind == "fnref":
        _fnref_target(store, node, env, ctx)
        t = "fnref"
    elif kind in _FNREF_COMBINATORS:
        t = _combinator(store, node, h, env, ctx)
    elif kind in ("kv_get", "kv_put", "kv_del", "kv_list", "kv_keys"):
        t = _infer_kv(store, node, h, env, ctx)
    elif kind == "cap":
        t = _infer_cap(store, node, h, env, ctx)
    elif kind == "now":
        t = "i64"
    elif kind == "print":
        _expect(store, node["s"], "print.s", "string", env, ctx)
        t = "unit"
    else:  # "fn": a function used as a value has its signature type
        _validate_decl_refs(store, node, h)
        t = fn_type(node["params"], node["ret"], store, _fn_effects(store, node, ctx))
    if not _uses_context(store, node, ctx):
        ctx.closed[h] = t
    return t


def _reject_fnref(h, node, role) -> None:
    """A `fnref` is not a value: only a combinator's `f` child may consume one."""
    if node["k"] == "fnref":
        raise GraphError(
            h,
            "E_FNREF_ESCAPE",
            f"{role} is a fnref; a fnref is only valid as `f` of "
            "map/filter/fold/and_then/sort_by",
        )


def _require_list(store, t, h, role):
    """The element type of a `list` type, or E_TYPE_MISMATCH naming `role`."""
    if isinstance(t, dict) and set(t) == {"list"}:
        return t["list"]
    raise GraphError(
        h, "E_TYPE_MISMATCH", f"{role} has type {render_type(t, store)}, expected a list"
    )


def _require_result(store, t, h, role):
    """The payload type of a `result<T,error>` type, or E_TYPE_MISMATCH naming `role`."""
    if isinstance(t, dict) and set(t) == {"result"}:
        return t["result"]
    raise GraphError(
        h, "E_TYPE_MISMATCH", f"{role} has type {render_type(t, store)}, expected a result"
    )


def _fnref_target(store, node, env, ctx):
    """The `fn` node referenced by a `fnref` `node`, validating the reference."""
    target = node["fn"]
    fn = store.get(target)
    if fn is None:
        raise GraphError(target, "E_UNKNOWN_REF", f"{target} is not in the store")
    if fn["k"] != "fn":
        try:
            got = render_type(_infer(store, fn, env, ctx, root=target), store)
        except _NeedsContext:
            got = "open"
        raise GraphError(
            target, "E_TYPE_MISMATCH", f"fnref.fn has type {got}, expected fn(...)->ret"
        )
    return fn


# Expected `f` signature and result type per combinator; `ctxT` (and `A` for fold)
# are the types of `ctx` and `init`, and `T` is the element type of the container.
def _combinator(store, node, h, env, ctx):
    kind = node["k"]
    if kind == "and_then":
        t = _require_result(
            store,
            _infer(store, _child(store, node["r"]), env, ctx, root=node["r"], role="and_then.r"),
            node["r"],
            "and_then.r",
        )
    else:
        t = _require_list(
            store,
            _infer(store, _child(store, node["l"]), env, ctx, root=node["l"], role=f"{kind}.l"),
            node["l"],
            f"{kind}.l",
        )
    ctx_t = _infer(
        store, _child(store, node["ctx"]), env, ctx, root=node["ctx"], role=f"{kind}.ctx"
    )
    acc = None
    if kind == "fold":
        acc = _infer(
            store, _child(store, node["init"]), env, ctx, root=node["init"], role="fold.init"
        )
    want, nparams = _combinator_expectation(kind, ctx_t, t, acc, store)
    fchild = node["f"]
    fnode = _child(store, fchild)
    if fnode["k"] != "fnref":
        got = _infer(store, fnode, env, ctx, root=fchild, role=f"{kind}.f")
        raise GraphError(
            fchild,
            "E_TYPE_MISMATCH",
            f"{kind}.f has type {render_type(got, store)}, expected fnref",
        )
    fn = _fnref_target(store, fnode, env, ctx)
    params, ret = fn["params"], fn["ret"]
    got = fn_type(params, ret, store)
    if len(params) != nparams:
        raise GraphError(fchild, "E_ARITY", f"{kind}.f expects {want}, got {got}")
    if _combinator_matches(kind, params, ret, ctx_t, t, acc):
        return _combinator_result(kind, t, ret, acc)
    raise GraphError(fchild, "E_TYPE_MISMATCH", f"{kind}.f expects {want}, got {got}")


def _combinator_expectation(kind, ctx_t, t, acc, store):
    """(expected `f` signature string, number of parameters) for one combinator."""
    c, e = render_type(ctx_t, store), render_type(t, store)
    if kind == "map":
        return f"fn({c}, {e}) -> U", 2
    if kind == "filter":
        return f"fn({c}, {e}) -> bool", 2
    if kind == "sort_by":
        return f"fn({c}, {e}) -> i64", 2
    if kind == "and_then":
        return f"fn({c}, {e}) -> result<U,error>", 2
    a = render_type(acc, store)
    return f"fn({c}, {a}, {e}) -> {a}", 3


def _combinator_matches(kind, params, ret, ctx_t, t, acc) -> bool:
    """True when the function behind a combinator's `f` has the required signature."""
    if kind == "fold":
        return params == [ctx_t, acc, t] and ret == acc
    if params != [ctx_t, t]:
        return False
    if kind == "map":
        return True
    if kind == "filter":
        return ret == "bool"
    if kind == "sort_by":
        return ret == "i64"
    return isinstance(ret, dict) and set(ret) == {"result"}  # and_then


def _combinator_result(kind, t, ret, acc):
    """The type produced by a combinator whose `f` already matched."""
    if kind == "map":
        return {"list": ret}
    if kind == "filter" or kind == "sort_by":
        return {"list": t}
    if kind == "and_then":
        return {"result": ret["result"]}
    return acc  # fold


def _decl_fields(store, decl, h):
    """Field types of a `rec` declaration; E_UNKNOWN_REF / E_TYPE_MISMATCH otherwise."""
    node = store.get(decl)
    if node is None:
        raise GraphError(h, "E_UNKNOWN_REF", f"record declaration {decl} is not in the store")
    if node["k"] != "rec":
        raise GraphError(h, "E_TYPE_MISMATCH", f"type reference {decl} is not a rec declaration")
    return node["fields"]


def _record_decl(store, rt, h, role):
    """The declaration hash behind a record type, naming `role` when it is not one."""
    if isinstance(rt, dict) and set(rt) == {"rec"}:
        return rt["rec"]
    raise GraphError(
        h, "E_TYPE_MISMATCH", f"{role} has type {render_type(rt, store)}, expected a record"
    )


def _infer_mk(store, node, h, env, ctx):
    decl = node["decl"]
    fields = _decl_fields(store, decl, h)
    if len(node["fields"]) != len(fields):
        raise GraphError(
            h, "E_ARITY", f"mk.fields has {len(node['fields'])} fields, expected {len(fields)}"
        )
    for i, (value, want) in enumerate(zip(node["fields"], fields, strict=True)):
        _expect(store, value, f"mk.fields[{i}]", want, env, ctx)
    return {"rec": decl}


def _infer_get(store, node, h, env, ctx):
    rt = _infer(store, _child(store, node["r"]), env, ctx, root=node["r"], role="get.r")
    fields = _decl_fields(store, _record_decl(store, rt, h, "get.r"), h)
    i = node["i"]
    if not 0 <= i < len(fields):
        raise GraphError(h, "E_BAD_FIELD", f"get.i={i} is outside 0..{len(fields) - 1}")
    return fields[i]


def _infer_with(store, node, h, env, ctx):
    rt = _infer(store, _child(store, node["r"]), env, ctx, root=node["r"], role="with.r")
    fields = _decl_fields(store, _record_decl(store, rt, h, "with.r"), h)
    i = node["i"]
    if not 0 <= i < len(fields):
        raise GraphError(h, "E_BAD_FIELD", f"with.i={i} is outside 0..{len(fields) - 1}")
    _expect(store, node["v"], "with.v", fields[i], env, ctx)
    return rt


def _arm_plan(store, node, h, env, ctx):
    """Validate a `match` and return [(arm_hash, binder_type_or_None), ...].

    The scrutinee fixes the constructors: `option<T>` -> (none, some) and
    `result<T,error>` -> (ok, err). A payload arm binds its payload as `var{0}`
    (`none` binds nothing). A missing/extra arm is E_NON_EXHAUSTIVE; a scrutinee
    that is not option/result is E_TYPE_MISMATCH.
    """
    s = node["s"]
    st = _infer(store, _child(store, s), env, ctx, root=s, role="match.s")
    if isinstance(st, dict) and "option" in st:
        what = "option"
        names, binders = ("none", "some"), (None, st["option"])
    elif isinstance(st, dict) and "result" in st:
        what = "result"
        names, binders = ("ok", "err"), (st["result"], "error")
    else:
        raise GraphError(
            s,
            "E_TYPE_MISMATCH",
            f"match.s has type {render_type(st, store)}, expected option or result",
        )
    arms = node["arms"]
    if len(arms) != len(names):
        if len(arms) < len(names):
            i = len(arms)
            binds = "nothing" if binders[i] is None else render_type(binders[i], store)
            raise GraphError(
                h, "E_NON_EXHAUSTIVE", f"arms[{i}] must be `{names[i]}`, binds {binds}"
            )
        raise GraphError(
            h, "E_NON_EXHAUSTIVE", f"arms[{len(names)}] is not a constructor of {what}"
        )
    return [(arms[i], binders[i]) for i in range(len(names))]


def _infer_call(store, node, h, env, ctx):
    fn = node["fn"]
    stored = store.type_of(fn)
    if stored is None:
        raise GraphError(fn, "E_UNKNOWN_REF", f"{fn} is not in the store")
    callee = store.get(fn)
    if callee is not None and callee["k"] == "fn":
        # Read the signature as JSON: a nominal label in the stored string cannot be
        # resolved back to its declaration hash without the type registry.
        params, ret = callee["params"], callee["ret"]
    else:
        sig = parse_fn_type(stored)
        if sig is None:
            raise GraphError(
                fn, "E_TYPE_MISMATCH", f"call.fn has type {stored}, expected fn(...)->ret"
            )
        params, ret = sig
    _check_args(store, h, node["args"], params, env, ctx, what="call")
    return ret


def _check_args(store, h, args, params, env, ctx, what) -> None:
    if len(args) != len(params):
        raise GraphError(h, "E_ARITY", f"{what}.args has {len(args)} args, expected {len(params)}")
    for i, (arg, want) in enumerate(zip(args, params, strict=True)):
        _expect(store, arg, f"{what}.args[{i}]", want, env, ctx)


# -- KV actor nodes and derived effects (ADR-0008) ---------------------------


def _namespace_type(store, ns, h, ctx):
    """Value type of a KV namespace; E_UNKNOWN_NAMESPACE when the spec did not seed it."""
    if ctx.namespaces is None:
        ctx.namespaces = store.kv_namespaces()
    t = ctx.namespaces.get(ns)
    if t is None:
        raise GraphError(h, "E_UNKNOWN_NAMESPACE", f"unknown KV namespace '{ns}'")
    _validate_type(store, t, h)
    return t


def _infer_kv(store, node, h, env, ctx):
    """Type of a KV node: `option<T>` / `list<T>` reads and `unit` writes."""
    kind = node["k"]
    t = _namespace_type(store, node["ns"], h, ctx)
    if kind == "kv_get":
        _expect(store, node["key"], "kv_get.key", "string", env, ctx)
        return {"option": t}
    if kind == "kv_list":
        _expect(store, node["prefix"], "kv_list.prefix", "string", env, ctx)
        return {"list": t}
    if kind == "kv_keys":
        _expect(store, node["prefix"], "kv_keys.prefix", "string", env, ctx)
        return {"list": "string"}
    _expect(store, node["key"], f"{kind}.key", "string", env, ctx)
    if kind == "kv_put":
        _expect(store, node["v"], "kv_put.v", t, env, ctx)
    return "unit"


def _infer_cap(store, node, h, env, ctx):
    """Check a provider contract and return the operation's derived result type."""
    from .providers import get_contract

    arg_nodes = [_child(store, ref) for ref in node["args"]]
    arg_types = [
        _infer(store, child, env, ctx, root=ref, role=f"cap.args[{index}]")
        for index, (ref, child) in enumerate(zip(node["args"], arg_nodes, strict=True))
    ]
    result, _effect = get_contract(node["provider"], h).signature(
        store, node["op"], arg_nodes, arg_types, h
    )
    return result


# Local effect of a node, before propagation through calls and combinator fnrefs.
_EFFECTS = {
    "kv_get": "kv.read",
    "kv_list": "kv.read",
    "kv_keys": "kv.read",
    "kv_put": "kv.write",
    "kv_del": "kv.write",
}


def _local_effect(store, node) -> set:
    """The effect a single node contributes on its own: "kv.read@ns", "clock@host", ..."""
    kind = node["k"]
    if kind in _EFFECTS:
        return {f"{_EFFECTS[kind]}@{node['ns']}"}
    if kind == "now":
        return {"clock@host"}
    if kind == "print":
        return {"print@stdout"}
    if kind == "cap":
        from .providers import get_contract

        operation = get_contract(node["provider"]).operations.get(node["op"])
        table_node = store.get(node["args"][0]) if node["args"] else None
        if operation is not None and table_node is not None and table_node.get("k") == "atom":
            return {f"{operation.effect}@{table_node['name']}"}
    return set()


def _fn_effects(store, node, ctx) -> set:
    """Effect set of a `fn`: the union over its body, its calls and its fnrefs.

    Recursion is a trivial fixed point: a `selfcall` adds nothing new, because the
    effects it would contribute are exactly those already gathered from the body.
    """
    h = ir.node_hash(node)
    cached = ctx.fn_effects.get(h)
    if cached is not None:
        return cached
    out = _effect_expr(store, node["body"], ctx)
    ctx.fn_effects[h] = out
    return out


def _effect_expr(store, h, ctx) -> set:
    """Effects contributed by the subexpression at `h`.

    A nested `fn` contributes nothing here: it is a distinct function, and only
    counts when a `call` or a combinator's `fnref` reaches it. A `call` adds the
    callee's effects (read from its stored signature) and a combinator adds those
    of the function behind its `f` fnref.
    """
    node = store.get(h)
    if node is None or node["k"] == "fn":
        return set()
    cached = ctx.effects.get(h)
    if cached is not None:
        return cached
    out = _local_effect(store, node)
    kind = node["k"]
    if kind == "call":
        out |= set(parse_effects(store.type_of(node["fn"]) or ""))
    elif kind in _FNREF_COMBINATORS:
        f = store.get(node["f"])
        if f is not None and f["k"] == "fnref":
            out |= set(parse_effects(store.type_of(f["fn"]) or ""))
    for child in ir.children(node):
        out |= _effect_expr(store, child, ctx)
    ctx.effects[h] = out
    return out


def check_effect_bound(store, name, type_text) -> None:
    """Reject a symbol whose derived effects exceed the spec's cap for `name`.

    The spec caps a root by name (ADR-0009): the effects carried by the stored
    type must be a subset of the cap. Without a cap the check is a no-op; with
    one, every effect outside it is raised as `E_EFFECT_EXCEEDED`, ordered.
    """
    cap = store.effect_bound(name)
    if cap is None:
        return
    derived = sorted(set(effects_of(type_text)))
    excess = sorted(set(derived) - set(cap))
    if excess:
        raise GraphError(
            name,
            "E_EFFECT_EXCEEDED",
            f"fn derives {{{', '.join(derived)}}} but the spec caps it at "
            f"{{{', '.join(cap)}}}; exceeds: [{', '.join(excess)}]",
        )

