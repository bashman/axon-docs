"""Agent-facing operations over the graph store: define, run, query_graph.

Every method returns a compact JSON-able dict; rejections come back as
{"ok": False, "error": {node, code, detail}} instead of raising.
"""

import difflib
import time

from . import interpreter, telemetry, typecheck, wasm
from . import spec as spec_mod
from .ir import HASH_RE, GraphError, children, flatten
from .kv import KvTx
from .memkv import MemoryKv
from .memsql import MemorySql
from .rebind import plan_replacement
from .show import show
from .store import Store
from .text import TextSyntaxError, from_text

MAX_QUERY_NODES = 50
MAX_COMPACT_FAILURES = 8
SUGGESTION_CUTOFF = 0.5


class _Host:
    """What the wasm runtime may touch during one invocation: the KV overlay, clock, stdout."""

    def __init__(self, tx, backend, sql_tx, clock_ms, lines):
        self._tx, self._backend, self._clock, self._lines = tx, backend, clock_ms, lines
        self._sql_tx = sql_tx
        self.kv_get, self.kv_put, self.kv_del = tx.get, tx.put, tx.delete
        self.kv_list, self.kv_keys = tx.list, tx.keys

    def ns_type(self, ns):
        return self._tx._namespace_type(ns)

    def clock(self):
        return self._clock

    def print(self, line):
        self._lines.append(line)

    def cap(self, provider, op, args):
        if provider != "sql":
            raise GraphError("cap", "E_UNKNOWN_PROVIDER", f"unknown provider '{provider}'")
        return self._sql_tx.call(op, args)


class _SpecState:
    """Runs a spec's cases on a fresh in-memory KV; the real state is restored afterwards."""

    def __init__(self, service):
        self._service = service
        self._mem = None
        self._sql = None

    def reset(self, state_json):
        svc = self._service
        self._mem = MemoryKv(svc.store)
        self._mem.load(state_json, svc.store.get)
        svc._kv_backend = self._mem

    def reset_tables(self, tables_json):
        svc = self._service
        self._sql = MemorySql(svc.store.table_schemas(), svc.store.get)
        self._sql.load(tables_json or {})
        svc._sql_backend = self._sql

    def snapshot(self):
        return self._mem.snapshot(self._service.store.get)

    def snapshot_tables(self):
        return self._sql.snapshot()


_HINTS = (
    ("E_TYPE_MISMATCH", "result<result<", "`ok` already wraps: return a result as is"),
    ("E_TYPE_MISMATCH", "option<option<", "`some` already wraps: return an option as is"),
)


def _with_hint(info: dict) -> dict:
    """Append a one-line fix to the error detail of the mistakes models repeat."""
    detail = info.get("detail")
    for code, needle, hint in _HINTS:
        if info.get("code") == code and isinstance(detail, str) and needle in detail:
            return {**info, "detail": f"{detail}; {hint}"}
    return info


class Service:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self._kv_backend = None  # set while a spec runs on an in-memory copy of the state
        self._sql_backend = None
        self._sql_persistent = MemorySql(self.store.table_schemas(), self.store.get)

    def set_sql_backend(self, backend) -> None:
        """Use an explicitly configured persistent SQL provider for root executions."""
        self._sql_persistent = backend

    def _guard(self, kind: str, payload: dict, fn):
        try:
            out = {"ok": True, **fn()}
        except GraphError as e:
            out = {"ok": False, "error": _with_hint(e.info)}
            if getattr(e, "stdout", None):
                out["stdout"] = e.stdout
        self.store.log(kind, payload, out)
        return out

    def _decl_of(self, ref, where):
        """A declaration id from a b3: id or a seeded type tag."""
        if isinstance(ref, str) and HASH_RE.match(ref):
            return ref
        if isinstance(ref, str) and "#" in ref:
            # `tag#<hex prefix>` as shown in type texts: an older declaration of that tag
            tag, _, prefix = ref.partition("#")
            h = self.store.resolve(prefix)
            node = self.store.get(h) or {}
            if node.get("k") != "rec" or node.get("tag") != tag:
                raise GraphError(
                    where, "E_UNKNOWN_TYPE_TAG", f"{ref!r} is not a declaration of {tag!r}"
                )
            return h
        h = self.store.type_pointer(ref) if isinstance(ref, str) else None
        if h is None:
            raise GraphError(
                where,
                "E_UNKNOWN_TYPE_TAG",
                f"no declaration is registered for {ref!r}; {self._known_types()}",
            )
        return h

    def _known_types(self) -> str:
        """Hint for an unknown type tag: registered tags and the older declarations of each."""
        current = self.store.types()
        older = []
        for tag, decls in sorted(self.store.declarations().items()):
            for decl in decls:
                if decl != current.get(tag):
                    names = ", ".join(self.store.field_names(decl) or [])
                    older.append(f"{tag}#{decl[3:11]} ({names})")
        text = f"registered: {', '.join(current) or 'none'}"
        if older:
            text += f"; older declarations (write them as tag#id): {'; '.join(older)}"
        return text

    def _bind_type(self, t):
        """Resolve {"rec": tag} to the current declaration id, at any nesting depth."""
        if isinstance(t, list):
            return [self._bind_type(x) for x in t]
        if isinstance(t, dict) and len(t) == 1:
            ((ctor, arg),) = t.items()
            return {ctor: self._decl_of(arg, "type") if ctor == "rec" else self._bind_type(arg)}
        return t

    def _field_index(self, decl, field, where):
        names = self.store.field_names(decl) or []
        if field not in names:
            raise GraphError(
                where, "E_BAD_FIELD", f"unknown field {field!r}; the declaration has {names}"
            )
        return names.index(field)

    def _bind_names(self, node):
        """Resolve names to ids before hashing, so the stored graph holds ids and positions only.

        call.fn names, {"rec": tag} types, mk{decl: tag, values: {...}} and
        get/with{field, of} are translated here; the typechecker verifies the result.
        """
        if isinstance(node, list):
            return [self._bind_names(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {k: self._bind_names(v) for k, v in node.items()}
        kind = out.get("k")
        for key in ("params", "ret", "t", "fields"):
            if kind in ("fn", "none", "err", "rec") and key in out:
                out[key] = self._bind_type(out[key])
        fn = out.get("fn")
        if kind in ("call", "fnref") and isinstance(fn, str) and not HASH_RE.match(fn):
            out["fn"] = self.store.resolve(fn)
        if kind == "mk":
            if isinstance(out.get("decl"), str):
                out["decl"] = self._decl_of(out["decl"], "mk.decl")
            if "values" in out:
                decl = out["decl"]
                if not isinstance(decl, str):
                    raise GraphError("mk", "E_BAD_NODE", "mk.values needs `decl` as a tag or id")
                names = self.store.field_names(decl) or []
                values = out.pop("values")
                if not isinstance(values, dict) or set(values) != set(names):
                    raise GraphError(
                        "mk", "E_BAD_FIELD", f"mk.values must provide exactly the fields {names}"
                    )
                out["fields"] = [values[n] for n in names]
        if kind in ("get", "with") and "field" in out:
            decl = self._decl_of(out.pop("of", None), f"{kind}.of")
            out["i"] = self._field_index(decl, out.pop("field"), kind)
        return out

    def define(self, node: dict, name: str | None = None) -> dict:
        """Create nodes (children may be inline) and return the root hash and its type."""

        def go():
            root, nodes = flatten(self._bind_names(node))
            added = 0
            for h, n in nodes:
                if self.store.get(h) is None:
                    self.store.put(h, n, typecheck.check(self.store, n))
                    added += 1
            if name:
                typecheck.check_effect_bound(self.store, name, self.store.type_of(root))
                self._check_http_handler(name, root)
                self.store.set_name(root, name)
                if self.store.type_of(root).startswith("fn("):
                    self.store.set_root(name, root)
            return {"hash": root, "type": self.store.type_of(root), "new_nodes": added}

        return self._guard("define", {"node": node, "name": name}, go)

    def _check_http_handler(self, name: str, root: str) -> None:
        """A route name can only point at the spec's exact request -> response function."""
        route_names = {
            route["handler"]
            for spec_name in self.store.specs()
            for route in self.store.get_spec(spec_name).get("routes", [])
        }
        if name not in route_names:
            return
        request = self.store.type_pointer("request")
        response = self.store.type_pointer("response")
        node = self.store.get(root) or {}
        expected_params = [{"rec": request}]
        expected_ret = {"rec": response}
        if node.get("k") != "fn" or node.get("params") != expected_params or node.get(
            "ret"
        ) != expected_ret:
            raise GraphError(
                name,
                "E_TYPE_MISMATCH",
                "HTTP route handler must have signature fn(request)->response",
            )

    def define_many(
        self, items: list[dict] | None = None, spec: str | None = None, src: str | None = None
    ) -> dict:
        """Define several named nodes in one call, in dependency order (ADR-0011).

        Each item is ``{"node": ..., "name": ...}``. Items may reference each other by name in
        ``call`` and ``fnref``; the batch is sorted so callees are defined first. It stops at the
        first failure: items defined before it stay (defining is idempotent), the result names the
        failing item. With ``spec``, a successful batch also returns the compact spec report.
        ``src`` is the compact text syntax (ADR-0012) with one definition per function; it is
        translated to the same items before anything else happens.
        """
        try:
            if src is not None:
                items = _items_from_text(src)
            ordered = _batch_order(items or [])
        except GraphError as e:
            out = {"ok": False, "error": e.info}
            self.store.log("define_many", {"names": [i.get("name") for i in items or []]}, out)
            return out
        defined = []
        superseded: list[str] = []
        for item in ordered:
            result = self._define_or_replace(item)
            if not result["ok"]:
                return {
                    "ok": False,
                    "failed": item.get("name"),
                    "error": _with_hint(result["error"]),
                    "defined": defined,
                }
            entry = {"name": item.get("name"), "hash": result["hash"], "type": result["type"]}
            if result.get("replaced"):
                entry["replaced"] = True
                entry["rebound"] = result["rebound"]
                if result.get("old"):
                    superseded.append(result["old"])
            defined.append(entry)
        out = {"ok": True, "defined": defined}
        stale = self._stale_dependents(superseded)
        if stale:
            out["stale"] = stale  # named functions still calling a superseded signature
        if spec is not None:
            out["spec"] = self.run_spec_compact(spec)
        return out

    def _query_text_many(self, refs: list[str], resolve) -> dict:
        """Several symbols in one reply; a function reached from more than one is printed once."""
        seen: set[str] = set()
        blocks: list[str] = []
        used: dict[str, list[str]] = {}
        for ref in refs:
            h = resolve(ref)
            text = show(
                self.store.get, h, self.store.name_of, self.store.field_names
            )
            keep, skipping = [], False
            for line in text.split("\n"):
                if line.startswith("fn "):
                    name = line[3:].split("(", 1)[0]
                    skipping = name in seen
                    seen.add(name)
                if not skipping:
                    keep.append(line)
            blocks.append("\n".join(keep))
            used[ref] = [u["name"] or u["hash"][:11] for u in self._used_by(h)]
        return {"text": "\n".join(b for b in blocks if b), "used_by": used}

    def _bound_hash(self, name: str | None) -> str | None:
        """The id a name currently points to, or None when it is unbound."""
        if not name:
            return None
        try:
            return self.store.resolve(name)
        except GraphError:
            return None

    def _define_or_replace(self, item: dict) -> dict:
        """Define a named item; a name already bound to ANOTHER hash is moved to the new one.

        The move is a `patch` (compare-and-swap on the id just read), so every dependent of the
        old definition is rebound to the new one in the same step and the effect bounds are
        checked. `rebound` counts the pointers moved, dependents included.
        """
        name = item.get("name")
        old = self._bound_hash(name)
        if old is None:
            return self.define(item["node"], name)
        result = self.define(item["node"])
        if not result["ok"] or result["hash"] == old:
            return self.define(item["node"], name) if result["ok"] else result
        kind = "symbol" if result["type"].startswith("fn(") else "name"
        change = {"pointer": {"kind": kind, "name": name}, "expected": old, "new": result["hash"]}
        moved = self.patch([change])
        signature_changed = False
        if not moved["ok"] and moved["error"]["code"] == "E_TYPE_MISMATCH":
            # a new signature cannot be pushed into the old dependents: move the name alone; the
            # dependents are expected to be redefined later in the same batch (see `stale`)
            moved = self.patch([{**change, "propagate": False}])
            signature_changed = True
        if not moved["ok"]:
            return moved
        return {
            **result,
            "replaced": True,
            "rebound": moved["applied"],
            "old": old if signature_changed else None,
        }

    def _stale_dependents(self, superseded: list[str]) -> list[str]:
        """Names bound to a function that still (transitively) calls a superseded definition."""
        stale: set[str] = set()
        for old in superseded:
            for user in self._used_by(old):
                name = user["name"]
                if name and self._bound_hash(name) == user["hash"]:
                    stale.add(name)
        return sorted(stale)

    def _migrate(self, fn_name: str, value):
        """Apply a schema migration (a pure root, by name) to one stored value."""
        h = self.store.resolve(fn_name)
        return wasm.run(self.store.get, h, [value], field_names=self.store.field_name)

    def run(self, ref: str, args: list | None = None, clock: int | None = None) -> dict:
        """Execute a function (by name, hash or unique prefix); args are typed JSON values.

        The invocation is a transaction (ADR-0008): KV writes are applied only if the root
        returns without a trap and without an `err` result. `clock` (ms since the epoch) is
        frozen for the whole run; when absent the real time is injected and recorded.
        """
        payload = {"ref": ref, "args": args}

        def go():
            h = self._resolve_with_suggestions(ref)
            clock_ms = int(time.time() * 1000) if clock is None else int(clock)
            payload["clock"] = clock_ms  # INV-21: every injected input is recorded
            backend = self._kv_backend or self.store
            tx = KvTx(backend, self.store.get, migrate=self._migrate)
            schemas = self.store.table_schemas()
            if (
                self._sql_backend is None
                and isinstance(self._sql_persistent, MemorySql)
                and self._sql_persistent.schemas != schemas
            ):
                self._sql_persistent = MemorySql(self.store.table_schemas(), self.store.get)
            sql_backend = self._sql_backend or self._sql_persistent
            sql_tx = sql_backend.begin()
            lines: list[str] = []
            host = _Host(tx, backend, sql_tx, clock_ms, lines)
            try:
                runner = interpreter.run if interpreter.uses_cap(self.store.get, h) else wasm.run
                result = runner(
                    self.store.get,
                    h,
                    list(args or []),
                    field_names=self.store.field_name,
                    host=host,
                )
            except GraphError as e:
                tx.rollback()
                sql_tx.rollback()
                e.stdout = lines
                raise
            if isinstance(result, dict) and result.get("tag") == "err":
                tx.rollback()
                sql_tx.rollback()
            else:
                try:
                    tx.commit()
                    sql_tx.commit()
                except BaseException:
                    sql_tx.rollback()
                    raise
            return {"result": result, **({"stdout": lines} if lines else {})}

        return self._guard("run", payload, go)

    def _name_suggestions(self, ref: str) -> list[str]:
        """Close root names for a failed lookup, so a typo never needs `roots` first."""
        names = sorted(self.store.roots())
        return difflib.get_close_matches(ref, names, n=3, cutoff=SUGGESTION_CUTOFF)

    def _resolve_with_suggestions(self, ref: str) -> str:
        """Resolve a ref and compactly enrich an unknown symbolic name."""
        try:
            return self.store.resolve(ref)
        except GraphError as error:
            detail = error.info.get("detail", "")
            if error.info.get("code") != "E_UNKNOWN_REF" or not detail.startswith(
                "no root or name "
            ):
                raise
            matches = self._name_suggestions(ref)
            if not matches:
                raise
            quoted = ", ".join(repr(match) for match in matches)
            raise GraphError(
                error.info["node"],
                error.info["code"],
                f"{detail}; did you mean {quoted}?",
            ) from error

    def query_graph(self, ref: str | list[str], depth: int = 1, format: str = "json") -> dict:
        """Return the exact subgraph around a symbol, never the whole graph.

        ``format="text"`` returns the readable projection (`show`) of the symbol and the
        functions it calls, plus the names of its transitive users, instead of node objects.
        A text lookup of a name that does not exist suggests the closest root names
        ("did you mean"), so a guess works without calling `roots` first.
        """

        def resolve_text(ref: str):
            """Resolve a ref in text mode; unknown names are reported with suggestions."""
            return self._resolve_with_suggestions(ref)

        def go():
            if isinstance(ref, list):
                if format != "text":
                    raise GraphError("query_graph", "E_BAD_NODE", "several refs need format=text")
                return self._query_text_many(ref, resolve_text)
            if format == "text":
                h = resolve_text(ref)
                used = [u["name"] or u["hash"][:11] for u in self._used_by(h)]
                text = show(
                    self.store.get,
                    h,
                    self.store.name_of,
                    self.store.field_names,
                )
                return {"root": h, "text": text, "used_by": used}
            h = self.store.resolve(ref)
            sub = self.store.subgraph(h, max(0, min(depth, 8)))
            truncated = len(sub) > MAX_QUERY_NODES
            return {
                "root": h,
                "nodes": [
                    {
                        "hash": x,
                        "name": self.store.name_of(x),
                        "type": self.store.type_of(x),
                        "node": n,
                        "children": children(n),
                    }
                    for x, n in sub[:MAX_QUERY_NODES]
                ],
                "dependents": self.store.dependents(h)[:MAX_QUERY_NODES],
                "used_by": self._used_by(h),
                "truncated": truncated,
            }

        return self._guard("query_graph", {"ref": ref, "depth": depth, "format": format}, go)

    def patch(self, changes: list[dict]) -> dict:
        """Atomically move roots/names: [{pointer: {kind, name}, expected, new, propagate}].

        Pointer kinds: root, name, or symbol (both at once). `expected` and `new` accept
        ids, names or prefixes (None = absent / delete). Moving a symbol from one function to
        another also rewrites every dependent (calls are linked by hash) unless
        `propagate` is false. Any stale expectation rejects the whole batch with E_STALE_ROOT.
        """

        def go():
            batch: list[dict] = []
            for c in changes:
                if not isinstance(c, dict):
                    raise GraphError("patch", "E_BAD_NODE", "change must be an object")
                ptr = c.get("pointer")
                kinds = (
                    ["root", "name"]
                    if isinstance(ptr, dict) and ptr.get("kind") == "symbol"
                    else [None]
                )
                for kind in kinds:
                    item = dict(c)
                    if kind:
                        item["pointer"] = {**ptr, "kind": kind}
                    for key in ("expected", "new"):
                        if item.get(key) is not None:
                            item[key] = self.store.resolve(item[key])
                    batch.append(item)
            moves, seen = [], set()
            for item in batch:
                both = item.get("expected") and item.get("new") and item["expected"] != item["new"]
                if both and item.get("propagate", True):
                    # fail fast on a stale expectation before planning the cascade
                    self.store.apply_cas(
                        [{**{k: item[k] for k in ("pointer", "expected")}, "new": item["expected"]}]
                    )
                    plan = plan_replacement(self.store, item["expected"], item["new"])
                    extra = plan["moves"]
                else:
                    extra = [{k: item.get(k) for k in ("pointer", "expected", "new")}]
                for m in extra:
                    key = (
                        m["pointer"]["kind"],
                        m["pointer"].get("namespace", "main"),
                        m["pointer"]["name"],
                    )
                    if key not in seen:
                        seen.add(key)
                        moves.append(m)
            for m in moves:
                if m["pointer"]["kind"] in ("root", "name") and m.get("new"):
                    typecheck.check_effect_bound(
                        self.store, m["pointer"]["name"], self.store.type_of(m["new"])
                    )
                    self._check_http_handler(m["pointer"]["name"], m["new"])
            self.store.apply_cas(moves)
            return {"applied": len(moves), "roots": self.store.roots()}

        return self._guard("patch", {"changes": changes}, go)

    def _used_by(self, h: str) -> list[dict]:
        """Functions that transitively reference `h`, climbing past inner expression nodes."""
        seen, queue, out = {h}, [h], []
        while queue and len(out) < MAX_QUERY_NODES:
            for p in self.store.dependents(queue.pop(0)):
                if p in seen:
                    continue
                seen.add(p)
                queue.append(p)
                if (self.store.get(p) or {}).get("k") == "fn":
                    out.append({"hash": p, "name": self.store.name_of(p)})
        return out

    def run_spec(self, name: str | None = None) -> dict:
        """Run a registered (human-written, read-only) spec by name against the current graph."""

        def go():
            body = self.store.get_spec(name)
            try:
                return spec_mod.run_cases(self.run, body, state=_SpecState(self), store=self.store)
            finally:
                self._kv_backend = None
                self._sql_backend = None

        return self._guard("run_spec", {"name": name}, go)

    def run_spec_compact(
        self, name: str | None = None, limit: int = MAX_COMPACT_FAILURES
    ) -> dict:
        """``run_spec`` reduced to totals plus the first failing step of each failing case.

        When every case passes the reply is minimal: ``{ok, spec, passed}`` — the totals
        below that only matter when something is broken.
        """
        report = self.run_spec(name)
        if not report.get("ok"):
            return report
        failures = []
        for case in report["cases"]:
            if case["status"] == "pass":
                continue
            steps = case["steps"]
            index = next((i for i, st in enumerate(steps) if st["status"] != "pass"), 0)
            step = steps[index]
            failures.append(
                {
                    "id": case["id"],
                    "step": index,
                    "fn": step.get("fn"),
                    "args": step.get("args"),
                    "expected": step.get("expected"),
                    "actual": step.get("actual"),
                }
            )
            if "state_actual" in case:
                failures[-1]["state_expected"] = case["state_expected"]
                failures[-1]["state_actual"] = case["state_actual"]
        if not failures:
            return {"ok": True, "spec": report["spec"], "passed": report["passed"]}
        out = {k: v for k, v in report.items() if k != "cases"}
        if failures:
            out["failures"] = failures[:limit]
            if len(failures) > limit:
                out["more_failures"] = len(failures) - limit
        return out

    def describe_spec(self, name: str | None = None) -> dict:
        """Describe a registered spec without exposing its cases or mutable graph state."""

        def go():
            selected = name
            if selected is None:
                available = self.store.specs()
                if len(available) == 1:
                    selected = available[0]
            body = self.store.get_spec(selected)
            return {"name": body["name"], "text": spec_mod.describe(body)}

        return self._guard("describe_spec", {"name": name}, go)

    def register_spec(self, body: dict) -> dict:
        """Human-side only (not an agent tool): register an immutable spec and its atoms."""
        def go():
            name = self.store.put_spec(body)
            if isinstance(self._sql_persistent, MemorySql):
                self._sql_persistent = MemorySql(self.store.table_schemas(), self.store.get)
            return {"name": name}

        return self._guard("register_spec", {"name": body.get("name")}, go)

    def telemetry(self) -> dict:
        return {"ok": True, **telemetry.report(self.store)}

    def roots(self) -> dict:
        return {"ok": True, "roots": self.store.roots()}

    def root_names(self, match: str | None = None, limit: int = 120) -> dict:
        """Sorted root names without hashes, optionally filtered by a substring."""
        names = sorted(n for n in self.store.roots() if not match or match in n)
        out = {"ok": True, "count": len(names), "names": names[:limit]}
        if len(names) > limit:
            out["truncated"] = f"showing {limit}; narrow it with roots(match=...)"
        return out


def _batch_names(node, names: set[str]) -> set[str]:
    """Names of other batch items a node refers to through ``call`` / ``fnref``."""
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            target = current.get("fn")
            if (
                current.get("k") in ("call", "fnref")
                and isinstance(target, str)
                and target in names
            ):
                found.add(target)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _items_from_text(src: str) -> list[dict]:
    """Parse ADR-0012 text into define_many items; a syntax error is reported with its position."""
    try:
        parsed = from_text(src)
    except TextSyntaxError as e:
        raise GraphError(
            "text", "E_TEXT_SYNTAX", f"{e.message} (line {e.line}, column {e.col})"
        ) from e
    if "k" in parsed:
        raise GraphError("text", "E_TEXT_SYNTAX", "expected named definitions: (fn name ...)")
    return [{"name": name, "node": node} for name, node in parsed.items()]


def _batch_order(items: list[dict]) -> list[dict]:
    """Sort batch items so every callee precedes its callers (stable among independent items)."""
    if not isinstance(items, list) or not items:
        raise GraphError("define_many", "E_BAD_BATCH", "items must be a non-empty list")
    names: dict[str, dict] = {}
    for position, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("node"), dict):
            raise GraphError("define_many", "E_BAD_BATCH", f"item {position} needs a node object")
        name = item.get("name")
        if name is not None:
            if name in names:
                raise GraphError("define_many", "E_BAD_BATCH", f"duplicate name {name!r}")
            names[name] = item
    deps = {id(item): _batch_names(item["node"], set(names)) - {item.get("name")} for item in items}
    ordered: list[dict] = []
    state: dict[int, int] = {}

    def visit(item: dict) -> None:
        if state.get(id(item)) == 2:
            return
        if state.get(id(item)) == 1:
            raise GraphError(
                "define_many", "E_BATCH_CYCLE", f"items form a cycle through {item.get('name')!r}"
            )
        state[id(item)] = 1
        for dep in sorted(deps[id(item)]):
            visit(names[dep])
        state[id(item)] = 2
        ordered.append(item)

    for item in items:
        visit(item)
    return ordered
