"""Export an AXON IR program to a readable, self-contained TypeScript module (Phase 3)."""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from . import ir
from .service import Service
from .store import Store

__all__ = ["export_ts"]


def _pascal(tag: str) -> str:
    return "".join(part.capitalize() for part in tag.split("_"))


class TsExporter:
    """Translates stored AXON IR nodes into a standalone TypeScript module."""

    def __init__(self, store: Store):
        self.store = store
        self.roots = store.roots()  # name -> hash
        self.kv_namespaces = store.kv_namespaces()
        self.type_pointers = store.types()
        self.var_counter = 0

        self.functions: dict[str, dict] = {}  # hash -> node
        self.fn_names: dict[str, str] = {}  # hash/name -> name
        # decl_hash -> (tag, [(name, type)])
        self.records: dict[str, tuple[str, list[tuple[str, Any]]]] = {}
        self.effectful: set[str] = set()
        self.call_graph: dict[str, set[str]] = {}

        self._collect_all()

    def _next_var(self) -> int:
        self.var_counter += 1
        return self.var_counter

    def _deref(self, ref: Any) -> dict | None:
        if isinstance(ref, dict):
            return ref
        if isinstance(ref, str) and ir.is_hash(ref):
            return self.store.get(ref)
        if isinstance(ref, str):
            h = self.store.resolve(ref)
            if h:
                return self.store.get(h)
        return None

    def resolve_decl(self, decl: str) -> str:
        if isinstance(decl, str) and ir.is_hash(decl):
            return decl
        if isinstance(decl, str):
            ptr = self.store.type_pointer(decl)
            if ptr:
                return ptr
            h = self.store.resolve(decl)
            if h:
                return h
        return decl

    def _collect_all(self) -> None:
        # 1. Identify all records registered in store
        for tag, decl_hash in self.type_pointers.items():
            decl_node = self.store.get(decl_hash)
            if decl_node and decl_node.get("k") == "rec":
                field_types = decl_node.get("fields", [])
                field_names = self.store.field_names(decl_hash) or [
                    f"f{i}" for i in range(len(field_types))
                ]
                self.records[decl_hash] = (tag, list(zip(field_names, field_types, strict=False)))

        # Also collect all rec nodes present in store
        for tag, decl_list in self.store.declarations().items():
            for decl_hash in decl_list:
                if decl_hash not in self.records:
                    decl_node = self.store.get(decl_hash)
                    if decl_node and decl_node.get("k") == "rec":
                        field_types = decl_node.get("fields", [])
                        field_names = self.store.field_names(decl_hash) or [
                            f"f{i}" for i in range(len(field_types))
                        ]
                        self.records[decl_hash] = (
                            tag,
                            list(zip(field_names, field_types, strict=False)),
                        )

        # 2. Traverse reachable functions starting from roots
        visited = set()

        def visit_fn(fn_h: str, name_hint: str | None = None) -> None:
            if fn_h in visited:
                return
            fn_node = self.store.get(fn_h)
            if not fn_node or fn_node.get("k") != "fn":
                return
            visited.add(fn_h)
            self.functions[fn_h] = fn_node
            name = name_hint or self.store.name_of(fn_h) or f"fn_{ir.digest_hex(fn_h)[:8]}"
            self.fn_names[fn_h] = name

            callees = set()

            def scan_calls(n_ref: Any) -> None:
                n = self._deref(n_ref)
                if not isinstance(n, dict):
                    return
                k = n.get("k")
                if k == "cap":
                    raise ir.GraphError(
                        ir.node_hash(n),
                        "E_UNSUPPORTED",
                        "the TypeScript exporter does not support cap nodes",
                    )
                if k == "call":
                    target = n.get("fn")
                    th = target if ir.is_hash(target) else self.store.resolve(target)
                    if th:
                        callees.add(th)
                        tname = self.store.name_of(th) or (
                            target if not target.startswith("b3:") else None
                        )
                        visit_fn(th, tname)
                elif k in ("map", "filter", "fold", "sort_by", "and_then"):
                    f_ref = n.get("f")
                    f_node = self._deref(f_ref)
                    if f_node and f_node.get("k") == "fnref":
                        target = f_node.get("fn")
                        th = target if ir.is_hash(target) else self.store.resolve(target)
                        if th:
                            callees.add(th)
                            tname = self.store.name_of(th) or (
                                target if not target.startswith("b3:") else None
                            )
                            visit_fn(th, tname)
                elif k == "rec":
                    decl_hash = ir.node_hash(n)
                    tag = n.get("tag", "record")
                    if decl_hash not in self.records:
                        field_types = n.get("fields", [])
                        field_names = self.store.field_names(decl_hash) or [
                            f"f{i}" for i in range(len(field_types))
                        ]
                        self.records[decl_hash] = (
                            tag,
                            list(zip(field_names, field_types, strict=False)),
                        )

                for field in ir.CHILD_FIELDS.get(k, ((), ()))[0]:
                    scan_calls(n.get(field))
                for field in ir.CHILD_FIELDS.get(k, ((), ()))[1]:
                    for item in n.get(field) or []:
                        scan_calls(item)

            scan_calls(fn_node.get("body"))
            self.call_graph[fn_h] = callees

        for rname, rh in sorted(self.roots.items()):
            visit_fn(rh, rname)

        # 3. Detect effectful functions
        # Direct effectful: any root, or any fn with kv_* / now
        direct_eff = set()
        for fn_h, fn_node in self.functions.items():
            if self.fn_names.get(fn_h) in self.roots:
                direct_eff.add(fn_h)
                continue

            has_eff = False

            def scan_eff(n_ref: Any) -> None:
                nonlocal has_eff
                if has_eff:
                    return
                n = self._deref(n_ref)
                if not isinstance(n, dict):
                    return
                k = n.get("k")
                if k in ("kv_get", "kv_put", "kv_del", "kv_list", "kv_keys", "now"):
                    has_eff = True
                    return
                for field in ir.CHILD_FIELDS.get(k, ((), ()))[0]:
                    scan_eff(n.get(field))
                for field in ir.CHILD_FIELDS.get(k, ((), ()))[1]:
                    for item in n.get(field) or []:
                        scan_eff(item)

            scan_eff(fn_node.get("body"))
            if has_eff:
                direct_eff.add(fn_h)

        self.effectful = set(direct_eff)
        changed = True
        while changed:
            changed = False
            for fn_h, callees in self.call_graph.items():
                if fn_h not in self.effectful and (callees & self.effectful):
                    self.effectful.add(fn_h)
                    changed = True

    def is_effectful_fn(self, fn_ref: str) -> bool:
        if ir.is_hash(fn_ref):
            return fn_ref in self.effectful
        # could be a function name
        for fh, name in self.fn_names.items():
            if name == fn_ref:
                return fh in self.effectful
        if fn_ref in self.roots:
            return True
        return False

    def ts_type(self, t: Any) -> str:
        if t == "i64":
            return "bigint"
        if t == "bool":
            return "boolean"
        if t == "string":
            return "string"
        if t == "unit":
            return "null"
        if t in ("error", "atom"):
            return "Err" if t == "error" else "string"
        if isinstance(t, dict):
            if "option" in t:
                return f"{self.ts_type(t['option'])} | null"
            if "result" in t:
                return f"Result<{self.ts_type(t['result'])}>"
            if "list" in t:
                inner = self.ts_type(t["list"])
                return f"({inner})[]" if "|" in inner else f"{inner}[]"
            if "rec" in t:
                decl = t["rec"]
                decl_h = self.resolve_decl(decl)
                if decl_h in self.records:
                    return _pascal(self.records[decl_h][0])
                if isinstance(decl, str) and not decl.startswith("b3:"):
                    return _pascal(decl)
                return "Record<string, unknown>"
        if isinstance(t, str):
            decl_h = self.resolve_decl(t)
            if decl_h in self.records:
                return _pascal(self.records[decl_h][0])
        return "unknown"

    def ts_record_type_of_ns(self, ns: str) -> str | None:
        ns_t = self.kv_namespaces.get(ns)
        if isinstance(ns_t, dict) and "rec" in ns_t:
            return self.ts_type(ns_t)
        if isinstance(ns_t, str):
            decl_h = self.resolve_decl(ns_t)
            if decl_h in self.records:
                return _pascal(self.records[decl_h][0])
        return None

    def resolve_type(self, t: Any) -> Any:
        if isinstance(t, str):
            ptr = self.store.type_pointer(t)
            if ptr:
                return {"rec": ptr}
        return t

    def infer_type(self, node_ref: Any, env_types: dict[str, list]) -> Any:
        node = self._deref(node_ref)
        if not isinstance(node, dict):
            return None
        k = node.get("k")
        if k == "lit":
            return "i64"
        if k in ("true", "false"):
            return "bool"
        if k == "str":
            return "string"
        if k == "unit":
            return "unit"
        if k == "now":
            return "i64"
        if k == "param":
            i = node.get("i", 0)
            params = env_types.get("params", [])
            return params[i] if i < len(params) else None
        if k == "var":
            i = node.get("i", 0)
            vars_ = env_types.get("vars", [])
            return vars_[-1 - i] if i < len(vars_) else None
        if k == "mk":
            decl = node.get("decl")
            return {"rec": self.resolve_decl(decl)}
        if k == "with":
            return self.infer_type(node.get("r"), env_types)
        if k == "kv_get":
            ns = node.get("ns")
            ns_t = self.resolve_type(self.kv_namespaces.get(ns))
            return {"option": ns_t} if ns_t else None
        if k == "kv_list":
            ns = node.get("ns")
            ns_t = self.resolve_type(self.kv_namespaces.get(ns))
            return {"list": ns_t} if ns_t else None
        if k == "call":
            fn_h = node.get("fn")
            target = self._deref(fn_h)
            if target:
                return self.resolve_type(target.get("ret"))
        if k == "at":
            lt = self.infer_type(node.get("l"), env_types)
            if isinstance(lt, dict) and "list" in lt:
                return {"option": lt["list"]}
        if k == "map":
            f_ref = node.get("f")
            f_node = self._deref(f_ref)
            target_h = f_node.get("fn") if f_node else None
            target = self._deref(target_h) if target_h else None
            if target:
                return {"list": self.resolve_type(target.get("ret"))}
        if k == "fold":
            return self.infer_type(node.get("init"), env_types)
        if k == "some":
            return {"option": self.infer_type(node.get("v"), env_types)}
        if k == "none":
            return {"option": self.resolve_type(node.get("t"))}
        if k == "ok":
            return {"result": self.infer_type(node.get("v"), env_types)}
        if k == "err":
            return {"result": self.resolve_type(node.get("t"))}
        return None

    def get_field_name(self, r_ref: Any, index: int, env_types: dict[str, list]) -> str:
        r_type = self.infer_type(r_ref, env_types)
        decl = None
        if isinstance(r_type, dict) and "rec" in r_type:
            decl = r_type["rec"]
        elif isinstance(r_type, str):
            decl = self.resolve_decl(r_type)
        if decl:
            decl_h = self.resolve_decl(decl)
            names = self.store.field_names(decl_h)
            if names and 0 <= index < len(names):
                return names[index]

        r_node = self._deref(r_ref)
        if r_node and r_node.get("k") == "mk":
            decl = r_node.get("decl")
            decl_h = self.resolve_decl(decl)
            names = self.store.field_names(decl_h)
            if names and 0 <= index < len(names):
                return names[index]

        return f"f{index}"

    def get_fnref_target(self, f_ref: Any) -> tuple[str, bool]:
        f_node = self._deref(f_ref)
        target = f_node.get("fn") if f_node else f_ref
        th = target if ir.is_hash(target) else self.store.resolve(target)
        name = self.fn_names.get(th) or (
            target if not target.startswith("b3:") else f"fn_{ir.digest_hex(th)[:8]}"
        )
        is_eff = self.is_effectful_fn(th or target)
        return name, is_eff

    def is_option_type(self, s_type: Any, s_node: Any) -> bool:
        if isinstance(s_type, dict):
            if "option" in s_type:
                return True
            if "result" in s_type:
                return False
        if isinstance(s_node, dict):
            k = s_node.get("k")
            if k in ("kv_get", "at", "none", "some"):
                return True
            if k in ("ok", "err"):
                return False
            if k == "call":
                target = self._deref(s_node.get("fn"))
                if target:
                    ret = target.get("ret")
                    if isinstance(ret, dict):
                        if "option" in ret:
                            return True
                        if "result" in ret:
                            return False
        return True  # default to option

    def compile_expr(
        self, node_ref: Any, scope: list[str], env_types: dict[str, list], current_fn: str
    ) -> str:
        node = self._deref(node_ref)
        if not isinstance(node, dict):
            return "null"
        k = node.get("k")

        if k == "lit":
            v = node.get("v", 0)
            return f"{v}n" if v >= 0 else f"({v}n)"
        if k == "str":
            v = unicodedata.normalize("NFC", node.get("v", ""))
            return json.dumps(v, ensure_ascii=False)
        if k == "true":
            return "true"
        if k == "false":
            return "false"
        if k in ("unit", "none"):
            return "null"
        if k == "now":
            return "ctx.now()"
        if k == "param":
            return f"p{node.get('i', 0)}"
        if k == "var":
            i = node.get("i", 0)
            return scope[-1 - i] if i < len(scope) else f"v_unknown_{i}"

        if k == "bin":
            op = node.get("op")
            a = self.compile_expr(node.get("a"), scope, env_types, current_fn)
            b = self.compile_expr(node.get("b"), scope, env_types, current_fn)
            if op == "add":
                return f"add64({a}, {b})"
            if op == "sub":
                return f"sub64({a}, {b})"
            if op == "mul":
                return f"mul64({a}, {b})"
            if op == "div":
                return f"div64({a}, {b})"
            if op == "rem":
                return f"rem64({a}, {b})"
            if op == "lt":
                return f"({a} < {b})"
            if op == "eq":
                return f"({a} === {b})"
            return f"({a} == {b})"

        if k == "not":
            a = self.compile_expr(node.get("a"), scope, env_types, current_fn)
            return f"!({a})"
        if k == "and":
            a = self.compile_expr(node.get("a"), scope, env_types, current_fn)
            b = self.compile_expr(node.get("b"), scope, env_types, current_fn)
            return f"({a} && {b})"
        if k == "or":
            a = self.compile_expr(node.get("a"), scope, env_types, current_fn)
            b = self.compile_expr(node.get("b"), scope, env_types, current_fn)
            return f"({a} || {b})"
        if k == "concat":
            a = self.compile_expr(node.get("a"), scope, env_types, current_fn)
            b = self.compile_expr(node.get("b"), scope, env_types, current_fn)
            return f"({a} + {b}).normalize(\"NFC\")"
        if k == "length":
            a = self.compile_expr(node.get("a"), scope, env_types, current_fn)
            return f"BigInt([...{a}].length)"
        if k == "repeat":
            s = self.compile_expr(node.get("s"), scope, env_types, current_fn)
            n = self.compile_expr(node.get("n"), scope, env_types, current_fn)
            return f"repeat_str({s}, {n})"
        if k == "streq":
            a = self.compile_expr(node.get("a"), scope, env_types, current_fn)
            b = self.compile_expr(node.get("b"), scope, env_types, current_fn)
            return f"({a} === {b})"

        if k == "some":
            return self.compile_expr(node.get("v"), scope, env_types, current_fn)
        if k == "ok":
            v = self.compile_expr(node.get("v"), scope, env_types, current_fn)
            return f"ok({v})"
        if k == "err":
            v = self.compile_expr(node.get("v"), scope, env_types, current_fn)
            return f"({{ ok: false, error: {v} }})"
        if k == "error":
            code_ref = node.get("code")
            code_node = self._deref(code_ref)
            if isinstance(code_node, dict) and code_node.get("k") == "atom":
                code_name = code_node.get("name", "")
            elif isinstance(code_ref, str) and not code_ref.startswith("b3:"):
                code_name = code_ref
            else:
                code_name = str(code_ref)
            code_str = json.dumps(code_name)
            detail_node = self._deref(node.get("detail"))
            if detail_node and detail_node.get("k") == "some":
                detail_expr = self.compile_expr(detail_node.get("v"), scope, env_types, current_fn)
            else:
                detail_expr = "null"
            return f"{{ code: {code_str}, detail: {detail_expr} }}"
        if k == "atom":
            return json.dumps(node.get("name", ""))

        if k == "call":
            fn_ref = node.get("fn")
            th = fn_ref if ir.is_hash(fn_ref) else self.store.resolve(fn_ref)
            callee_name = self.fn_names.get(th) or (
                fn_ref if not fn_ref.startswith("b3:") else f"fn_{ir.digest_hex(th)[:8]}"
            )
            is_eff = self.is_effectful_fn(th or fn_ref)
            args = [
                self.compile_expr(a, scope, env_types, current_fn)
                for a in node.get("args", [])
            ]
            if is_eff:
                args = ["ctx"] + args
            return f"{callee_name}({', '.join(args)})"

        if k == "selfcall":
            is_eff = self.is_effectful_fn(current_fn)
            args = [
                self.compile_expr(a, scope, env_types, current_fn)
                for a in node.get("args", [])
            ]
            if is_eff:
                args = ["ctx"] + args
            return f"{current_fn}({', '.join(args)})"

        if k == "mk":
            decl = node.get("decl")
            decl_h = self.resolve_decl(decl)
            names = self.store.field_names(decl_h) or []
            fields = []
            for i, f_node in enumerate(node.get("fields", [])):
                fname = names[i] if i < len(names) else f"f{i}"
                fval = self.compile_expr(f_node, scope, env_types, current_fn)
                fields.append(f"{fname}: {fval}")
            return f"{{ {', '.join(fields)} }}"

        if k == "get":
            r_expr = self.compile_expr(node.get("r"), scope, env_types, current_fn)
            fname = self.get_field_name(node.get("r"), node.get("i", 0), env_types)
            return f"{r_expr}.{fname}"

        if k == "with":
            r_expr = self.compile_expr(node.get("r"), scope, env_types, current_fn)
            v_expr = self.compile_expr(node.get("v"), scope, env_types, current_fn)
            fname = self.get_field_name(node.get("r"), node.get("i", 0), env_types)
            return f"{{ ...{r_expr}, {fname}: {v_expr} }}"

        if k == "list":
            items = [
                self.compile_expr(item, scope, env_types, current_fn)
                for item in node.get("items", [])
            ]
            return f"[{', '.join(items)}]"
        if k == "len":
            l_expr = self.compile_expr(node.get("l"), scope, env_types, current_fn)
            return f"BigInt({l_expr}.length)"
        if k == "at":
            l_expr = self.compile_expr(node.get("l"), scope, env_types, current_fn)
            idx_expr = self.compile_expr(node.get("idx"), scope, env_types, current_fn)
            in_bounds = f"({idx_expr} >= 0n && {idx_expr} < BigInt({l_expr}.length))"
            return f"({in_bounds} ? {l_expr}[Number({idx_expr})] : null)"
        if k == "append":
            a_expr = self.compile_expr(node.get("a"), scope, env_types, current_fn)
            b_expr = self.compile_expr(node.get("b"), scope, env_types, current_fn)
            return f"[...{a_expr}, ...{b_expr}]"
        if k == "reverse":
            l_expr = self.compile_expr(node.get("l"), scope, env_types, current_fn)
            return f"[...{l_expr}].reverse()"

        if k == "map":
            l_expr = self.compile_expr(node.get("l"), scope, env_types, current_fn)
            target_name, target_is_eff = self.get_fnref_target(node.get("f"))
            ctx_val = self.compile_expr(node.get("ctx"), scope, env_types, current_fn)
            call = (
                f"{target_name}(ctx, {ctx_val}, item)"
                if target_is_eff
                else f"{target_name}({ctx_val}, item)"
            )
            return f"{l_expr}.map((item) => {call})"
        if k == "filter":
            l_expr = self.compile_expr(node.get("l"), scope, env_types, current_fn)
            target_name, target_is_eff = self.get_fnref_target(node.get("f"))
            ctx_val = self.compile_expr(node.get("ctx"), scope, env_types, current_fn)
            call = (
                f"{target_name}(ctx, {ctx_val}, item)"
                if target_is_eff
                else f"{target_name}({ctx_val}, item)"
            )
            return f"{l_expr}.filter((item) => {call})"
        if k == "fold":
            l_expr = self.compile_expr(node.get("l"), scope, env_types, current_fn)
            init_expr = self.compile_expr(node.get("init"), scope, env_types, current_fn)
            target_name, target_is_eff = self.get_fnref_target(node.get("f"))
            ctx_val = self.compile_expr(node.get("ctx"), scope, env_types, current_fn)
            call = (
                f"{target_name}(ctx, {ctx_val}, acc, item)"
                if target_is_eff
                else f"{target_name}({ctx_val}, acc, item)"
            )
            return f"{l_expr}.reduce((acc, item) => {call}, {init_expr})"
        if k == "sort_by":
            l_expr = self.compile_expr(node.get("l"), scope, env_types, current_fn)
            target_name, target_is_eff = self.get_fnref_target(node.get("f"))
            ctx_val = self.compile_expr(node.get("ctx"), scope, env_types, current_fn)
            call = (
                f"{target_name}(ctx, {ctx_val}, item)"
                if target_is_eff
                else f"{target_name}({ctx_val}, item)"
            )
            return f"sortBy({l_expr}, (item) => {call})"
        if k == "and_then":
            r_expr = self.compile_expr(node.get("r"), scope, env_types, current_fn)
            target_name, target_is_eff = self.get_fnref_target(node.get("f"))
            ctx_val = self.compile_expr(node.get("ctx"), scope, env_types, current_fn)
            call = (
                f"{target_name}(ctx, {ctx_val}, __res.value)"
                if target_is_eff
                else f"{target_name}({ctx_val}, __res.value)"
            )
            return f"((__res) => __res.ok ? {call} : __res)({r_expr})"

        if k == "kv_get":
            ns = node.get("ns")
            key_expr = self.compile_expr(node.get("key"), scope, env_types, current_fn)
            rec_t = self.ts_record_type_of_ns(ns)
            as_cast = f" as {rec_t} | null" if rec_t else ""
            return f"(ctx.kv.get({json.dumps(ns)}, {key_expr}){as_cast})"
        if k == "kv_put":
            ns = node.get("ns")
            key_expr = self.compile_expr(node.get("key"), scope, env_types, current_fn)
            v_expr = self.compile_expr(node.get("v"), scope, env_types, current_fn)
            return f"(ctx.kv.put({json.dumps(ns)}, {key_expr}, {v_expr}), null)"
        if k == "kv_del":
            ns = node.get("ns")
            key_expr = self.compile_expr(node.get("key"), scope, env_types, current_fn)
            return f"(ctx.kv.del({json.dumps(ns)}, {key_expr}), null)"
        if k == "kv_list":
            ns = node.get("ns")
            pref_expr = self.compile_expr(node.get("prefix"), scope, env_types, current_fn)
            rec_t = self.ts_record_type_of_ns(ns)
            as_cast = f" as ({rec_t})[]" if rec_t else ""
            return f"(ctx.kv.list({json.dumps(ns)}, {pref_expr}){as_cast})"
        if k == "kv_keys":
            ns = node.get("ns")
            pref_expr = self.compile_expr(node.get("prefix"), scope, env_types, current_fn)
            return f"ctx.kv.keys({json.dumps(ns)}, {pref_expr})"
        if k == "print":
            s_expr = self.compile_expr(node.get("s"), scope, env_types, current_fn)
            return f"console.log({s_expr})"

        # Expression-position let, if, match
        if k in ("let", "match", "if"):
            inner_stmts = self.emit_statements(
                node, scope, env_types, indent=1, current_fn=current_fn
            )
            inner_text = "\n".join(inner_stmts)
            return f"(() => {{\n{inner_text}\n}})()"

        return "null"

    def emit_statements(
        self,
        node_ref: Any,
        scope: list[str],
        env_types: dict[str, list],
        indent: int,
        current_fn: str,
    ) -> list[str]:
        pad = "  " * indent
        node = self._deref(node_ref)
        if not isinstance(node, dict):
            return [f"{pad}return null;"]

        k = node.get("k")

        if k == "let":
            v_ref = node.get("v")
            v_node = self._deref(v_ref)
            v_k = v_node.get("k") if isinstance(v_node, dict) else None

            # Handle side-effecting kv_put/kv_del where unit result is discarded
            if v_k in ("kv_put", "kv_del"):
                ns = v_node.get("ns")
                key_expr = self.compile_expr(v_node.get("key"), scope, env_types, current_fn)
                if v_k == "kv_put":
                    val_expr = self.compile_expr(v_node.get("v"), scope, env_types, current_fn)
                    line = f"{pad}ctx.kv.put({json.dumps(ns)}, {key_expr}, {val_expr});"
                else:
                    line = f"{pad}ctx.kv.del({json.dumps(ns)}, {key_expr});"
                new_env = {"params": env_types["params"], "vars": env_types["vars"] + ["unit"]}
                new_scope = scope + ["null"]
                rest = self.emit_statements(
                    node.get("body"), new_scope, new_env, indent, current_fn
                )
                return [line] + rest

            v_expr = self.compile_expr(v_ref, scope, env_types, current_fn)
            v_type = self.infer_type(v_ref, env_types)
            ident = f"v{self._next_var()}"
            line = f"{pad}const {ident} = {v_expr};"
            new_env = {"params": env_types["params"], "vars": env_types["vars"] + [v_type]}
            new_scope = scope + [ident]
            rest = self.emit_statements(
                node.get("body"), new_scope, new_env, indent, current_fn
            )
            return [line] + rest

        if k == "if":
            c_expr = self.compile_expr(node.get("c"), scope, env_types, current_fn)
            t_stmts = self.emit_statements(node.get("t"), scope, env_types, indent + 1, current_fn)
            e_stmts = self.emit_statements(node.get("e"), scope, env_types, indent + 1, current_fn)
            return (
                [f"{pad}if ({c_expr}) {{"]
                + t_stmts
                + [f"{pad}}} else {{"]
                + e_stmts
                + [f"{pad}}}"]
            )

        if k == "match":
            s_ref = node.get("s")
            s_expr = self.compile_expr(s_ref, scope, env_types, current_fn)
            s_type = self.infer_type(s_ref, env_types)
            s_node = self._deref(s_ref)
            temp = f"match_{self._next_var()}"
            line = f"{pad}const {temp} = {s_expr};"
            is_opt = self.is_option_type(s_type, s_node)
            arms = node.get("arms", [])

            if is_opt:
                elem_type = s_type.get("option") if isinstance(s_type, dict) else None
                none_stmts = self.emit_statements(arms[0], scope, env_types, indent + 1, current_fn)
                v_ident = f"v{self._next_var()}"
                new_env = {"params": env_types["params"], "vars": env_types["vars"] + [elem_type]}
                new_scope = scope + [v_ident]
                some_stmts = [f"{pad}  const {v_ident} = {temp};"] + self.emit_statements(
                    arms[1], new_scope, new_env, indent + 1, current_fn
                )
                return (
                    [line, f"{pad}if ({temp} === null) {{"]
                    + none_stmts
                    + [f"{pad}}} else {{"]
                    + some_stmts
                    + [f"{pad}}}"]
                )
            else:
                val_type = s_type.get("result") if isinstance(s_type, dict) else None
                v_ident = f"v{self._next_var()}"
                new_env_ok = {"params": env_types["params"], "vars": env_types["vars"] + [val_type]}
                ok_stmts = [f"{pad}  const {v_ident} = {temp}.value;"] + self.emit_statements(
                    arms[0], scope + [v_ident], new_env_ok, indent + 1, current_fn
                )
                err_ident = f"v{self._next_var()}"
                new_env_err = {"params": env_types["params"], "vars": env_types["vars"] + ["error"]}
                err_stmts = [f"{pad}  const {err_ident} = {temp}.error;"] + self.emit_statements(
                    arms[1], scope + [err_ident], new_env_err, indent + 1, current_fn
                )
                return (
                    [line, f"{pad}if ({temp}.ok) {{"]
                    + ok_stmts
                    + [f"{pad}}} else {{"]
                    + err_stmts
                    + [f"{pad}}}"]
                )

        expr = self.compile_expr(node, scope, env_types, current_fn)
        return [f"{pad}return {expr};"]

    def export(self) -> str:
        chunks = [_HEADER]

        # Export record interfaces
        if self.records:
            chunks.append("// --- Record Types ---")
            sorted_records = sorted(self.records.items(), key=lambda item: item[1][0])
            for _decl_h, (tag, fields) in sorted_records:
                name = _pascal(tag)
                flines = [f"  {fname}: {self.ts_type(ftype)};" for fname, ftype in fields]
                chunks.append(f"export interface {name} {{\n" + "\n".join(flines) + "\n}\n")

        # Topologically sort functions: callees before callers, roots last
        deps = {fh: set(self.call_graph.get(fh, ())) for fh in self.functions}
        ordered = []
        visited = set()

        def visit(fh: str) -> None:
            if fh in visited:
                return
            visited.add(fh)
            for callee in sorted(deps.get(fh, ())):
                if callee in self.functions:
                    visit(callee)
            ordered.append(fh)

        # Non-roots first
        for fh in sorted(self.functions, key=lambda h: self.fn_names.get(h, "")):
            if self.fn_names.get(fh) not in self.roots:
                visit(fh)
        # Roots last
        for fh in sorted(self.functions, key=lambda h: self.fn_names.get(h, "")):
            if self.fn_names.get(fh) in self.roots:
                visit(fh)

        chunks.append("// --- Functions ---")
        for fn_h in ordered:
            fn_node = self.functions[fn_h]
            name = self.fn_names[fn_h]
            is_eff = fn_h in self.effectful
            params = [self.ts_type(t) for t in fn_node.get("params", [])]
            ret = self.ts_type(fn_node.get("ret"))

            param_decls = []
            if is_eff:
                param_decls.append("ctx: Ctx")
            param_decls.extend(f"p{i}: {t}" for i, t in enumerate(params))

            self.var_counter = 0
            env_types = {
                "params": [self.resolve_type(t) for t in fn_node.get("params", [])],
                "vars": [],
            }
            body_lines = self.emit_statements(
                fn_node.get("body"), [], env_types, indent=1, current_fn=name
            )
            body_text = "\n".join(body_lines)

            sig = f"export function {name}({', '.join(param_decls)}): {ret}"
            chunks.append(f"{sig} {{\n{body_text}\n}}\n")

        return "\n".join(chunks)


_HEADER = """// Generated by AXON TypeScript Exporter (ADR-0013 / Fase 3)
// Zero lock-in: standalone TypeScript module matching the AXON runtime contract.

export class Trap extends Error {
  readonly code: string;
  constructor(code: string, message?: string) {
    super(message ?? code);
    this.name = "Trap";
    this.code = code;
  }
}

export const I64_MIN = -(2n ** 63n);
export const I64_MAX = 2n ** 63n - 1n;

export function check_i64(value: bigint, op: string = "i64"): bigint {
  if (value < I64_MIN || value > I64_MAX) {
    throw new Trap("E_OVERFLOW", `${op} overflows i64`);
  }
  return value;
}

export function add64(a: bigint, b: bigint): bigint {
  return check_i64(a + b, "add64");
}

export function sub64(a: bigint, b: bigint): bigint {
  return check_i64(a - b, "sub64");
}

export function mul64(a: bigint, b: bigint): bigint {
  return check_i64(a * b, "mul64");
}

export function div64(a: bigint, b: bigint): bigint {
  if (b === 0n) throw new Trap("E_DIV_ZERO", "division by zero");
  if (a === I64_MIN && b === -1n) throw new Trap("E_OVERFLOW", "div64 overflows i64");
  return a / b;
}

export function rem64(a: bigint, b: bigint): bigint {
  if (b === 0n) throw new Trap("E_DIV_ZERO", "remainder by zero");
  if (a === I64_MIN && b === -1n) return 0n;
  return a % b;
}

export function repeat_str(s: string, n: bigint): string {
  if (n < 0n) throw new Trap("E_BAD_ARG", "repeat count negative");
  if (n === 0n || s.length === 0) return "";
  const maxBytes = 1 << 20;
  if (n > BigInt(maxBytes)) throw new Trap("E_STRING_LIMIT", "string too large");
  const count = Number(n);
  const out = s.repeat(count);
  if (new TextEncoder().encode(out).length > maxBytes) {
    throw new Trap("E_STRING_LIMIT", "string too large");
  }
  return out;
}

export interface Err {
  readonly code: string;
  readonly detail: string | null;
}

export type Result<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: Err };

export function ok<T>(value: T): Result<T> {
  return { ok: true, value };
}

export function err(code: string, detail: string | null = null): Result<never> {
  return { ok: false, error: { code, detail } };
}

export function isResult(value: unknown): value is Result<unknown> {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as { readonly ok?: unknown };
  if (candidate.ok === true) return "value" in value;
  if (candidate.ok === false) return "error" in value;
  return false;
}

export function sortBy<T>(items: T[], key: (item: T) => bigint): T[] {
  return items
    .map((item) => ({ item, key: key(item) }))
    .sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
    .map((entry) => entry.item);
}

export interface Kv {
  get(ns: string, key: string): unknown;
  put(ns: string, key: string, value: unknown): void;
  del(ns: string, key: string): void;
  list(ns: string, prefix: string): unknown[];
  keys(ns: string, prefix: string): string[];
}

export interface Ctx {
  readonly kv: Kv;
  now(): bigint;
  print?(s: string): void;
}

const encoder = new TextEncoder();
export function compareKeys(a: string, b: string): number {
  const left = encoder.encode(a);
  const right = encoder.encode(b);
  const length = Math.min(left.length, right.length);
  for (let i = 0; i < length; i += 1) {
    const diff = (left[i] ?? 0) - (right[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return left.length - right.length;
}

const DELETED = Symbol("deleted");

export class MemoryKv implements Kv {
  private readonly base = new Map<string, Map<string, unknown>>();
  private pending: Map<string, Map<string, unknown | typeof DELETED>> | null = null;

  private rows(ns: string): Map<string, unknown> {
    let r = this.base.get(ns);
    if (!r) {
      r = new Map();
      this.base.set(ns, r);
    }
    return r;
  }

  seed(ns: string, entries: Record<string, unknown>): void {
    const r = this.rows(ns);
    for (const [k, v] of Object.entries(entries)) {
      r.set(k, v);
    }
  }

  begin(): void {
    this.pending = new Map();
  }

  commit(): void {
    if (this.pending === null) return;
    for (const [ns, changes] of this.pending) {
      const r = this.rows(ns);
      for (const [k, v] of changes) {
        if (v === DELETED) r.delete(k);
        else r.set(k, v);
      }
    }
    this.pending = null;
  }

  rollback(): void {
    this.pending = null;
  }

  get(ns: string, key: string): unknown {
    const staged = this.pending?.get(ns)?.get(key);
    if (staged !== undefined) return staged === DELETED ? null : staged;
    return this.rows(ns).get(key) ?? null;
  }

  put(ns: string, key: string, value: unknown): void {
    if (this.pending !== null) {
      let stageNs = this.pending.get(ns);
      if (!stageNs) {
        stageNs = new Map();
        this.pending.set(ns, stageNs);
      }
      stageNs.set(key, value);
      return;
    }
    this.rows(ns).set(key, value);
  }

  del(ns: string, key: string): void {
    if (this.pending !== null) {
      let stageNs = this.pending.get(ns);
      if (!stageNs) {
        stageNs = new Map();
        this.pending.set(ns, stageNs);
      }
      stageNs.set(key, DELETED);
      return;
    }
    this.rows(ns).delete(key);
  }

  private scan(ns: string, prefix: string): [string, unknown][] {
    const merged = new Map<string, unknown>(this.rows(ns));
    if (this.pending) {
      const stageNs = this.pending.get(ns);
      if (stageNs) {
        for (const [k, v] of stageNs) {
          if (v === DELETED) merged.delete(k);
          else merged.set(k, v);
        }
      }
    }
    const matched: [string, unknown][] = [];
    for (const [k, v] of merged) {
      if (k.startsWith(prefix)) matched.push([k, v]);
    }
    matched.sort(([a], [b]) => compareKeys(a, b));
    if (matched.length > 65536) throw new Trap("E_LIST_LIMIT", "too many rows");
    return matched;
  }

  list(ns: string, prefix: string): unknown[] {
    return this.scan(ns, prefix).map(([, v]) => v);
  }

  keys(ns: string, prefix: string): string[] {
    return this.scan(ns, prefix).map(([k]) => k);
  }

  snapshot(): Record<string, Record<string, unknown>> {
    const out: Record<string, Record<string, unknown>> = {};
    for (const [ns, rows] of this.base) {
      if (rows.size > 0) {
        const obj: Record<string, unknown> = {};
        for (const [k, v] of rows) obj[k] = v;
        out[ns] = obj;
      }
    }
    return out;
  }
}

export class TestCtx implements Ctx {
  readonly kv: MemoryKv;
  private clock: bigint | null = null;

  constructor() {
    this.kv = new MemoryKv();
  }

  now(): bigint {
    if (this.clock === null) throw new Trap("E_CLOCK_EXHAUSTED");
    return this.clock;
  }

  setClock(clock: bigint | null): void {
    this.clock = clock;
  }
}

export function createContext(): TestCtx {
  return new TestCtx();
}

export function invoke(
  ctx: TestCtx,
  fn: (c: Ctx, ...args: unknown[]) => unknown,
  args: readonly unknown[],
  clock?: bigint,
): { result: unknown } | { error: string } {
  ctx.setClock(clock ?? null);
  ctx.kv.begin();
  try {
    const returned = fn(ctx, ...args);
    if (isResult(returned)) {
      if (returned.ok) {
        ctx.kv.commit();
        return { result: returned.value };
      }
      ctx.kv.rollback();
      return { error: returned.error.code };
    }
    ctx.kv.commit();
    return { result: returned };
  } catch (err: unknown) {
    ctx.kv.rollback();
    if (err instanceof Trap) return { error: err.code };
    throw err;
  }
}

export function snapshot(ctx: TestCtx): Record<string, Record<string, unknown>> {
  return ctx.kv.snapshot();
}
"""


def export_ts(service_or_items: Any) -> str:
    """Export an AXON Service, Store, or items list to a TypeScript module string."""
    if hasattr(service_or_items, "store"):
        store = service_or_items.store
    elif isinstance(service_or_items, Store):
        store = service_or_items
    elif isinstance(service_or_items, (list, dict)):
        svc = Service()
        if isinstance(service_or_items, dict) and "name" not in service_or_items:
            items = [{"name": k, "node": v} for k, v in service_or_items.items()]
        elif isinstance(service_or_items, dict):
            items = [service_or_items]
        else:
            items = [
                {"name": item[0], "node": item[1]} if isinstance(item, (tuple, list)) else item
                for item in service_or_items
            ]
        svc.define_many(items)
        store = svc.store
    else:
        tname = type(service_or_items).__name__
        raise TypeError(f"expected Service, Store, list or dict, got {tname}")

    return TsExporter(store).export()
