"""MCP server exposing the AXON graph to an agent: define, run, query, verify."""

import argparse
import contextvars
import json

import anyio
from mcp.server import runner as mcp_runner
from mcp.server.mcpserver import MCPServer
from mcp.shared._compat import resync_tracer

from .http_cases import run_registered_cases, validate_cases_document
from .service import Service
from .store import Store

INSTRUCTIONS = (
    "AXON builds programs by mutating a typed graph, not by writing files. "
    "Every node is a JSON object whose discriminant field is `k`, e.g. "
    '{"k":"lit","v":1}, {"k":"param","i":0}, '
    '{"k":"bin","op":"add|sub|mul|lt|eq|div|rem","a":..,"b":..}, '
    '{"k":"if","c":..,"t":..,"e":..}, {"k":"call","fn":<id or name>,"args":[..]}, '
    '{"k":"selfcall","args":[..]}, {"k":"fn","params":["i64",..],"ret":"i64","body":..}, '
    '{"k":"let","v":..,"body":..} (binds v; refer to it with {"k":"var","i":0}, '
    "i = De Bruijn index, 0 = innermost let of the same fn). "
    "div/rem truncate toward zero (rem(-7,2) = -1); dividing by zero is E_DIV_ZERO. "
    "Children are b3:<64 hex> ids or nested nodes. Types: i64, bool, string, atom, error, "
    '{"option":T}, {"result":T} (result error type is always `error`). '
    'Bool nodes: {"k":"true"}, {"k":"false"}, not{a}, and{a,b}, or{a,b} (and/or short-circuit). '
    'Strings (UTF-8, NFC): {"k":"str","v":..}, concat{a,b}, length{a} (code points), '
    "repeat{s,n}, streq{a,b}. "
    "Options/results: none{t}, some{v}, ok{v}, err{v,t} (v:error), "
    "error{code:atom,detail:option<string>}, "
    '{"k":"atom","name":..} (must be registered by the spec). '
    "match{s,arms}: arms in constructor order (option: none,some; result: ok,err); "
    "payload arms bind var{0}; no wildcard, wrong arm count is E_NON_EXHAUSTIVE. "
    "Records are nominal and declared by the spec (read-only): "
    'type {"rec": "nota"}; build with '
    '{"k":"mk","decl":"nota","values":{"titulo":..,"cuerpo":..}}, read with '
    '{"k":"get","r":..,"field":"titulo","of":"nota"}, copy-update with '
    '{"k":"with","r":..,"field":..,"of":"nota","v":..}. Run args/results use field names. '
    'Also unit ({"k":"unit"}) and {"list": T}: list{t,items<=16}, len{l}, at{l,idx} (option), '
    "append{a,b}, reverse{l}. Combinators take a context FIRST: map/filter/sort_by{l,f,ctx}, "
    'fold{l,init,f,ctx}, and_then{r,f,ctx}, where f is {"k":"fnref","fn":<name or id>} of a '
    "top-level fn (fnref anywhere else is E_FNREF_ESCAPE): f(ctx,x), fold f(ctx,acc,x); pass "
    "unit when no context is needed. sort_by is stable ascending by an i64 key. "
    "State lives in KV namespaces declared by the spec (typed): "
    "kv_get{ns,key} -> option<T>, kv_put{ns,key,v}, kv_del{ns,key} -> unit, "
    "kv_list{ns,prefix} -> list<T> and kv_keys{ns,prefix} -> list<string>, both ordered by key; "
    "now{} -> i64 ms since epoch (one value per run); print{s}. Each root run is a transaction: "
    "writes are kept only if it neither traps nor returns an err result; reads see your own "
    "writes. The fn type shows derived effects, e.g. fn(string)->unit!{kv.write@notas}. "
    "SQL insert_auto takes a table and its non-id fields in declaration order, assigning "
    "max(id)+1. "
    "lt/eq take i64 and return bool; 'if' needs a bool condition and equally typed branches. "
    "Every fn declares its param types and its return type. Recursion uses selfcall. "
    "A name already bound to another hash is only moved with `patch` (CAS). "
    "Call `describe_spec()` first to discover the registered spec name and its records, atoms, "
    "KV namespaces, SQL tables, HTTP routes and expected root signatures. "
    "When visible HTTP cases are registered, call `run_http_cases()` after each batch to replay "
    "them in isolated in-memory state and inspect the first failed request of each case. "
    "For an HTTP spec, `run_spec` may legitimately report zero graph cases; that is not HTTP "
    "verification and must not replace a successful `run_http_cases()` report. "
    "Work in few calls: send every function of a module in ONE `define_many` (it orders items "
    "by dependencies) and pass the spec name to get the compact `run_spec` report in the same "
    "reply; `define` is for a single node. "
    "Errors come back as {ok:false, error:{node,code,detail}}."
)


INSTRUCTIONS_TEXT = (
    "AXON builds programs by mutating a typed graph, not by writing files. Call "
    "`describe_spec()` first to discover the registered spec name and all declarations. "
    "When the server has visible HTTP cases, call `run_http_cases()` after each batch; it replays "
    "only the human-registered visible cases in isolated in-memory state. "
    "For HTTP work, `run_spec` may report zero graph cases; do not declare completion until "
    "`run_http_cases()` passes. "
    "Write code as "
    "S-expressions and send ALL the functions of a step in ONE `define_many(src=..., spec=<spec "
    "name>)`: one `(fn name (param types) ret body)` per function, any order (the batch is "
    "ordered by dependencies); the reply carries the compact spec report. Read code with "
    '`query_graph(ref, format="text")`: TypeScript-like pseudocode of the function, the '
    "functions it calls and `used_by`; pass a LIST of names to read several in one call. "
    "`define_many` also REPLACES a name that already exists: its dependents are rebound; if a "
    "signature changed, send the dependents in the same batch and watch `stale` in the reply. "
    "`roots(match=...)` lists names; a wrong ref in text query_graph gets did-you-mean hints. "
    "`patch` (CAS) is only for moving pointers by hand; ids may be unique prefixes (>= 8 hex). "
    'Nodes are positional: integers, "strings", true, false, unit, $i = parameter i, '
    "@i = let variable (De Bruijn, 0 = innermost). "
    "(bin add|sub|mul|div|rem|lt|eq a b) (if c t e) (call name arg...) (selfcall arg...) "
    "(name arg...) also calls a function; (let v body) (not a) (and a b) (or a b) "
    "(concat a b) (length a) (repeat s n) (streq a b) "
    "(none T) (some v) (ok v) (err v T; v may be a bare (atom E_X)) "
    "(atom NAME) (error code detail) with code (atom E_X) and "
    'detail (none string) or (some "msg"); (match s arm...) arms in constructor order '
    "(option: none, some; result: ok, err), payload arms bind @0, no wildcard; "
    "records are declared by the spec: (mk nota titulo=a cuerpo=b) (get r nota titulo) "
    "(with r nota titulo v); lists: (list T item...) (len l) (at l i) (append a b) (reverse l) "
    "(map l f ctx) (filter l f ctx) (fold l init f ctx) (and_then r f ctx) (sort_by l f ctx), "
    "f is the NAME of a top-level fn called as f(ctx, x) (fold: f(ctx, acc, x)); pass unit "
    "when no context is needed; sort_by is stable ascending by an i64 key; "
    "KV namespaces are declared by the spec: (kv_get ns key) -> option<T>, (kv_put ns key v), "
    "(kv_del ns key), (kv_list ns prefix) -> list<T>, (kv_keys ns prefix), ordered by key; "
    "(now) -> i64 ms; (print s). Types: i64 bool string atom error unit option<T> list<T> "
    "result<T> (also written (result T)) and record names; "
    "every fn declares param and return types; (result error type "
    "is always `error`). Example: (fn doble (i64) i64 (bin mul $0 2)) "
    "(fn mas1 (i64) i64 (match (some $0) 0 (bin add @0 1))). "
    "div/rem truncate toward zero; dividing by zero is E_DIV_ZERO. Each root run is a "
    "transaction: writes are kept only if it neither traps nor returns an err result. "
    "SQL insert_auto takes a table and its non-id fields in declaration order, assigning "
    "max(id)+1. "
    "The fn type shows derived effects, e.g. fn(string)->unit!{kv.write@notas}. "
    "Errors come back as {ok:false, error:{node,code,detail}}; syntax errors carry line and column."
)


def _run_stdio(server: MCPServer):
    """Finish each accepted request before reading the next frame (including EOF)."""
    original = mcp_runner.JSONRPCDispatcher

    class DrainingDispatcher(original):
        async def run(
            self,
            on_request,
            on_notify,
            on_notify_intercept=None,
            *,
            task_status=anyio.TASK_STATUS_IGNORED,
        ):
            self._on_notify_intercept = on_notify_intercept
            try:
                async with self._write_stream:
                    async with anyio.create_task_group() as tasks:
                        self._tg = tasks
                        self._running = True
                        task_status.started()
                        async with self._read_stream:
                            async for item in self._read_stream:
                                sender_ctx: contextvars.Context | None = getattr(
                                    self._read_stream, "last_context", None
                                )
                                await self._dispatch(item, on_request, on_notify, sender_ctx)
                self._running = False
                self._closed = True
                self._fan_out_closed()
            finally:
                self._running = False
                self._closed = True
                self._tg = None
                self._fan_out_closed()
                await resync_tracer()

    async def run():
        await server.run_stdio_async()

    mcp_runner.JSONRPCDispatcher = DrainingDispatcher
    try:
        anyio.run(run)
    finally:
        mcp_runner.JSONRPCDispatcher = original


def build_server(
    service: Service, text_mode: bool = False, http_cases: dict | None = None
) -> MCPServer:
    server = MCPServer("axon", instructions=INSTRUCTIONS_TEXT if text_mode else INSTRUCTIONS)

    @server.tool()
    def define(node: dict, name: str | None = None) -> dict:
        """Create a node (children may be nested inline). Returns {hash, type, new_nodes}.
        Give `name` to register a human-readable name (and a root, if the node is a fn)."""
        return service.define(node, name)

    @server.tool()
    def define_many(
        items: list[dict] | None = None, spec: str | None = None, src: str | None = None
    ) -> dict:
        """Define SEVERAL named nodes in ONE call (prefer this to many `define` calls).
        `items` is [{node, name}] or, in text form, `src` holds one `(fn name ...)` per function;
        items may call each other by name in `call`/`fnref`, and
        the batch is ordered by dependencies for you. It stops at the first failure and says
        which item failed (earlier items stay defined). Pass `spec` (a registered spec name)
        to get the compact spec report in the same reply: totals plus the first failing step
        of each failing case. Returns {defined: [{name, hash, type}], spec?}."""
        return service.define_many(items, spec, src)

    @server.tool()
    def run(ref: str, args: list | None = None) -> dict:
        """Run function `ref` (name, hash or unique hash prefix) with typed JSON args: ints,
        true/false, strings, {"atom": n}, {"tag": "none"|"some"|"ok"|"err", "v": ..}.
        Returns {result}."""
        return service.run(ref, args)

    @server.tool()
    def query_graph(ref: str | list[str], depth: int = 1, format: str = "json") -> dict:
        """Return the subgraph around `ref` (up to `depth` levels, max 8) and its dependents.
        With format="text": readable pseudocode of `ref` and the functions it calls, plus the
        names of the functions that use it (`used_by`). `ref` may be a LIST of names with
        format="text": read several functions in ONE call (each shared callee printed once).
        In text mode a name that does not exist comes back with did-you-mean suggestions."""
        return service.query_graph(ref, depth, format)

    @server.tool()
    def patch(changes: list[dict]) -> dict:
        """Atomically move roots/names (compare-and-swap). Each change is
        {pointer: {kind: "symbol"|"root"|"name", name}, expected, new}; expected/new are ids,
        names or prefixes (null = must not exist / delete). A stale `expected` rejects the whole
        batch with E_STALE_ROOT; kind "symbol" moves the name and its root together. Redefine a
        name by patching it from its current hash to the new one."""
        return service.patch(changes)

    @server.tool()
    def run_spec(name: str | None = None, verbose: bool = False) -> dict:
        """Run the registered, read-only spec `name` against the current graph. By default the
        reply is compact: totals plus the first failing step of each failing case (id, fn,
        args, expected, actual and, for state mismatches, the expected and actual state);
        `verbose` returns every case. Roots returning result<T,error> are observed at the
        frontier: ok(v) matches expect.result v (T), err(e) matches expect.error e.code. A
        domain error needs a result root."""
        return service.run_spec(name) if verbose else service.run_spec_compact(name)

    @server.tool()
    def describe_spec(name: str | None = None) -> dict:
        """Describe a registered, read-only spec in compact text: records and field types,
        atoms, KV namespaces, SQL tables and constraints, HTTP routes and policies, and expected
        root signatures. If `name` is omitted, the only registered spec is selected."""
        return service.describe_spec(name)

    @server.tool()
    def run_http_cases(step: int | None = None) -> dict:
        """Replay only the human-registered visible HTTP cases against the current graph.
        Each case gets a fresh in-memory SQL seed, sessions, rate history and X-Test-Time clock.
        Returns totals plus the first failed request of each failing case (id, request, expected,
        actual). The agent cannot register or replace cases."""
        return run_registered_cases(service, http_cases, step=step)

    @server.tool()
    def roots(match: str | None = None) -> dict:
        """List named entry points. JSON mode: {name: hash}. Text mode: sorted names only
        (address functions by name); `match` keeps names containing that substring."""
        if text_mode:
            return service.root_names(match)
        out = service.roots()
        if match:
            out["roots"] = {n: h for n, h in out["roots"].items() if match in n}
        return out

    return server


def main(
    db_path: str = ":memory:",
    spec_paths: list[str] | None = None,
    text_mode: bool = False,
    backend: str = "memory",
    pg_dsn: str | None = None,
    http_case_paths: list[str] | None = None,
) -> None:
    """Serve over stdio. Specs are registered here by the human, never through an agent tool."""
    service = Service(Store(db_path))
    for path in spec_paths or []:
        with open(path, encoding="utf-8") as f:
            result = service.register_spec(json.load(f))
        if not result["ok"]:
            raise SystemExit(f"spec {path}: {result['error']}")
    if backend == "postgres":
        from .pgsql import PostgresSql

        service.set_sql_backend(
            PostgresSql(service.store.table_schemas(), service.store.get, pg_dsn or "")
        )
    elif backend != "memory":
        raise ValueError(f"unknown SQL backend {backend!r}")
    registered_cases = None
    for path in http_case_paths or []:
        with open(path, encoding="utf-8") as source:
            current = validate_cases_document(json.load(source))
        if registered_cases is not None:
            raise SystemExit("only one HTTP cases document may be registered")
        registered_cases = current
    _run_stdio(build_server(service, text_mode, registered_cases))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", nargs="?", default=":memory:")
    parser.add_argument("spec", nargs="*")
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--http-cases", action="append", default=[])
    parsed = parser.parse_args()
    main(parsed.db, parsed.spec, parsed.text, http_case_paths=parsed.http_cases)
