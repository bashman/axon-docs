"""Lower IR v1 functions to WebAssembly and execute them with Wasmtime."""

from __future__ import annotations

import struct
import unicodedata
from collections.abc import Callable
from typing import Any

import wasmtime

from .ir import ATOM_RE, I64_MAX, I64_MIN, MAX_STRING_BYTES, GraphError, check_shape

GetNode = Callable[[str], dict | None]
FieldNames = Callable[[str, int], str | None]
Type = str | dict[str, "Type"]
MEMORY_LIMIT = 64 << 20
PAGE_SIZE = 1 << 16
MAX_LIST_LENGTH = 65_536

# Heap layout (all values are passed through WASM as i64):
# string/atom = [byte_len:u32][codepoint_len:u32][UTF-8]; option/result =
# [tag:i64][payload:i64] (none/some and ok/err use tags 0/1); error =
# [code_atom_ptr:i64][detail_option_ptr:i64]; record =
# [arity:i64][field_0:i64]...[field_n:i64]; list =
# [length:i64][element_0:i64]...[element_n:i64]. Objects are 8-byte aligned.


def _function_name(node_hash: str) -> str:
    return f"fn_{node_hash}"


def _align(size: int) -> int:
    return (size + 7) & ~7


def _wat_bytes(value: bytes) -> str:
    return "".join(f"\\{byte:02x}" for byte in value)


class _WatCompiler:
    def __init__(self, get: GetNode, ns_type: Callable[[str], Type] | None = None):
        self.get = get
        self.ns_type = ns_type
        self.nodes: dict[str, dict] = {}
        self.functions: dict[str, str] = {}
        self.pending: list[str] = []
        self.active_expressions: set[str] = set()
        self.trap_sites: dict[int, tuple[str, str, str]] = {}
        self.function_locals: list[str] = []
        self.function_temporary_count = 0
        self.static_strings: dict[bytes, int] = {}
        self.data_segments: list[tuple[int, bytes]] = []
        self.static_end = 8

    def load(self, node_hash: str) -> dict:
        if node_hash in self.nodes:
            return self.nodes[node_hash]
        try:
            node = self.get(node_hash)
        except GraphError:
            raise
        except (KeyError, LookupError):
            node = None
        if node is None:
            raise GraphError(node_hash, "E_UNKNOWN_REF", f"unknown node {node_hash}")
        check_shape(node, node_hash, inline=False)
        self.nodes[node_hash] = node
        return node

    def compile(self, entry: str) -> str:
        if self.load(entry)["k"] != "fn":
            raise GraphError(entry, "E_TYPE_MISMATCH", "entry must reference a fn node")
        self.pending.append(entry)
        while self.pending:
            function_hash = self.pending.pop(0)
            if function_hash not in self.functions:
                self.functions[function_hash] = self._compile_function(function_hash)
        pages = max(1, (self.static_end + PAGE_SIZE - 1) // PAGE_SIZE)
        if pages > MEMORY_LIMIT // PAGE_SIZE:
            raise GraphError(entry, "E_MEMORY", "static data exceeds the 64 MiB arena")
        imports = [
            '  (import "host" "normalize_concat" '
            "(func $normalize_concat (param i64 i64) (result i64)))",
        ]
        used_kinds = {node["k"] for node in self.nodes.values()}
        effect_imports = {
            "kv_get": '(func $host_kv_get (param i64 i64) (result i64))',
            "kv_put": '(func $host_kv_put (param i64 i64 i64) (result i64))',
            "kv_del": '(func $host_kv_del (param i64 i64) (result i64))',
            "kv_list": '(func $host_kv_list (param i64 i64) (result i64))',
            "kv_keys": '(func $host_kv_keys (param i64 i64) (result i64))',
            "now": '(func $host_now (result i64))',
            "print": '(func $host_print (param i64) (result i64))',
        }
        imports.extend(
            f'  (import "host" "{kind}" {declaration})'
            for kind, declaration in effect_imports.items()
            if kind in used_kinds
        )
        lines = [
            "(module",
            *imports,
            f'  (memory (export "memory") {pages} {MEMORY_LIMIT // PAGE_SIZE})',
            f'  (global $__heap (export "__heap") (mut i32) (i32.const {self.static_end}))',
            '  (global $__trap_site (export "__trap_site") (mut i32) (i32.const -1))',
            self._allocator_function(),
            self._byte_equality_function(),
            *self.functions.values(),
        ]
        lines.extend(
            f'  (data (i32.const {offset}) "{_wat_bytes(data)}")'
            for offset, data in self.data_segments
        )
        lines.extend([f'  (export "main" (func ${_function_name(entry)}))', ")"])
        return "\n".join(lines)

    @staticmethod
    def _allocator_function() -> str:
        return """  (func $__alloc (param $size i64) (param $site i32) (result i64)
    (local $old i32) (local $end i64) (local $pages i32)
    (local.set $old (global.get $__heap))
    (local.set $end (i64.add (i64.extend_i32_u (local.get $old)) (local.get $size)))
    (if (i64.gt_u (local.get $end) (i64.const 67108864))
      (then (global.set $__trap_site (local.get $site)) (unreachable)))
    (local.set $pages (i32.wrap_i64
      (i64.shr_u (i64.add (local.get $end) (i64.const 65535)) (i64.const 16))))
    (if (i32.gt_u (local.get $pages) (memory.size))
      (then (if (i32.eq (memory.grow (i32.sub (local.get $pages) (memory.size)))
                        (i32.const -1))
        (then (global.set $__trap_site (local.get $site)) (unreachable)))))
    (global.set $__heap (i32.wrap_i64 (local.get $end)))
    (i64.extend_i32_u (local.get $old)))"""

    @staticmethod
    def _byte_equality_function() -> str:
        return """  (func $__bytes_equal
      (param $a i64) (param $b i64) (param $n i64) (result i64)
    (local $i i64) (local $same i64)
    (local.set $same (i64.const 1))
    (block $done (loop $loop
      (br_if $done (i64.ge_u (local.get $i) (local.get $n)))
      (if (i32.ne
            (i32.load8_u (i32.wrap_i64 (i64.add (local.get $a) (local.get $i))))
            (i32.load8_u (i32.wrap_i64 (i64.add (local.get $b) (local.get $i)))))
        (then (local.set $same (i64.const 0)) (br $done)))
      (local.set $i (i64.add (local.get $i) (i64.const 1)))
      (br $loop)))
    (local.get $same))"""

    def _compile_function(self, function_hash: str) -> str:
        node = self.load(function_hash)
        if node["k"] != "fn":
            raise GraphError(
                function_hash, "E_TYPE_MISMATCH", "call target must reference a fn node"
            )
        params = "".join(" (param i64)" for _ in node["params"])
        self.function_locals = []
        self.function_temporary_count = 0
        body = self._compile_expression(node["body"], function_hash, (), ())
        locals_ = "".join(f" (local ${name} i64)" for name in self.function_locals)
        return (
            f"  (func ${_function_name(function_hash)}{params} "
            f"(result i64){locals_}\n    {body}\n  )"
        )

    def _compile_expression(
        self,
        node_hash: str,
        current_function: str,
        let_locals: tuple[str, ...],
        let_types: tuple[Type, ...],
    ) -> str:
        if node_hash in self.active_expressions:
            raise GraphError(node_hash, "E_BAD_NODE", "cyclic expression reference")
        self.active_expressions.add(node_hash)
        try:
            node = self.load(node_hash)
            kind = node["k"]
            if kind == "lit":
                return f"(i64.const {node['v']})"
            if kind in {"str", "atom"}:
                value = node["v"] if kind == "str" else node["name"]
                return f"(i64.const {self._static_string(value)})"
            if kind in {"true", "false"}:
                return f"(i64.const {1 if kind == 'true' else 0})"
            if kind == "unit":
                return "(i64.const 0)"
            if kind == "not":
                operand = self._compile_expression(
                    node["a"], current_function, let_locals, let_types
                )
                return f"(i64.extend_i32_u (i64.eqz {operand}))"
            if kind in {"and", "or"}:
                return self._compile_short_circuit(node, current_function, let_locals, let_types)
            if kind == "param":
                arity = len(self.load(current_function)["params"])
                if node["i"] >= arity:
                    raise GraphError(
                        node_hash,
                        "E_BAD_PARAM",
                        f"parameter {node['i']} is outside function arity {arity}",
                    )
                return f"(local.get {node['i']})"
            if kind == "var":
                if node["i"] >= len(let_locals):
                    raise GraphError(
                        node_hash,
                        "E_BAD_VAR",
                        f"variable {node['i']} is outside let depth {len(let_locals)}",
                    )
                return f"(local.get ${let_locals[-1 - node['i']]})"
            if kind == "let":
                depth = len(let_locals)
                local = self._ensure_let_local(depth)
                value_type = self._expression_type(node["v"], current_function, let_types)
                value = self._compile_expression(node["v"], current_function, let_locals, let_types)
                body = self._compile_expression(
                    node["body"], current_function, (*let_locals, local), (*let_types, value_type)
                )
                return f"(block (result i64) (local.set ${local} {value}) {body})"
            if kind == "bin":
                return self._compile_binary(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind == "if":
                condition = self._compile_expression(
                    node["c"], current_function, let_locals, let_types
                )
                then = self._compile_expression(node["t"], current_function, let_locals, let_types)
                otherwise = self._compile_expression(
                    node["e"], current_function, let_locals, let_types
                )
                return (
                    f"(if (result i64) (i64.ne {condition} (i64.const 0)) "
                    f"(then {then}) (else {otherwise}))"
                )
            if kind == "call":
                target_hash = node["fn"]
                target = self.load(target_hash)
                if target["k"] != "fn":
                    raise GraphError(
                        target_hash, "E_TYPE_MISMATCH", "call target must reference a fn node"
                    )
                self._check_arity(node_hash, len(target["params"]), node["args"])
                if target_hash not in self.functions and target_hash not in self.pending:
                    self.pending.append(target_hash)
                args = self._compile_arguments(
                    node["args"], current_function, let_locals, let_types
                )
                return f"(call ${_function_name(target_hash)}{args})"
            if kind == "selfcall":
                self._check_arity(
                    node_hash, len(self.load(current_function)["params"]), node["args"]
                )
                args = self._compile_arguments(
                    node["args"], current_function, let_locals, let_types
                )
                return f"(call ${_function_name(current_function)}{args})"
            if kind == "length":
                value = self._compile_expression(node["a"], current_function, let_locals, let_types)
                return f"(i64.extend_i32_u (i32.load offset=4 (i32.wrap_i64 {value})))"
            if kind == "streq":
                return self._compile_streq(node, current_function, let_locals, let_types)
            if kind == "concat":
                return self._compile_concat(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind == "repeat":
                return self._compile_repeat(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind in {"none", "some", "ok", "err", "error"}:
                return self._compile_heap_value(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind == "mk":
                return self._compile_record(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind == "get":
                return self._compile_record_get(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind == "with":
                return self._compile_record_with(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind == "list":
                return self._compile_list(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind == "len":
                value = self._compile_expression(
                    node["l"], current_function, let_locals, let_types
                )
                return f"(i64.load (i32.wrap_i64 {value}))"
            if kind == "at":
                return self._compile_list_at(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind == "append":
                return self._compile_list_append(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind == "reverse":
                return self._compile_list_reverse(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind in {"map", "filter", "fold", "and_then", "sort_by"}:
                return self._compile_combinator(
                    node_hash, node, current_function, let_locals, let_types
                )
            if kind in {"kv_get", "kv_put", "kv_del", "kv_list", "kv_keys"}:
                ns = f"(i64.const {self._static_string(node['ns'])})"
                field = "prefix" if kind in {"kv_list", "kv_keys"} else "key"
                argument = self._compile_expression(
                    node[field], current_function, let_locals, let_types
                )
                if kind == "kv_put":
                    value = self._compile_expression(
                        node["v"], current_function, let_locals, let_types
                    )
                    return f"(call $host_kv_put {ns} {argument} {value})"
                return f"(call $host_{kind} {ns} {argument})"
            if kind == "now":
                return "(call $host_now)"
            if kind == "print":
                value = self._compile_expression(
                    node["s"], current_function, let_locals, let_types
                )
                return f"(call $host_print {value})"
            if kind == "cap":
                raise GraphError(
                    node_hash,
                    "E_UNSUPPORTED",
                    "the WebAssembly backend does not support cap nodes; use the interpreter",
                )
            if kind == "fnref":
                raise GraphError(
                    node_hash,
                    "E_FNREF_ESCAPE",
                    "fnref may only be used directly as a combinator f argument",
                )
            if kind == "match":
                return self._compile_match(node_hash, node, current_function, let_locals, let_types)
            raise GraphError(node_hash, "E_TYPE_MISMATCH", f"unsupported expression {kind}")
        finally:
            self.active_expressions.remove(node_hash)

    def _compile_short_circuit(self, node, current_function, let_locals, let_types) -> str:
        left = self._compile_expression(node["a"], current_function, let_locals, let_types)
        right = self._compile_expression(node["b"], current_function, let_locals, let_types)
        condition = f"(i64.ne {left} (i64.const 0))"
        if node["k"] == "and":
            return f"(if (result i64) {condition} (then {right}) (else (i64.const 0)))"
        return f"(if (result i64) {condition} (then (i64.const 1)) (else {right}))"

    def _compile_binary(self, node_hash, node, current_function, let_locals, let_types) -> str:
        left = self._compile_expression(node["a"], current_function, let_locals, let_types)
        right = self._compile_expression(node["b"], current_function, let_locals, let_types)
        if node["op"] in {"add", "sub", "mul"}:
            return self._compile_checked_arithmetic(node_hash, node["op"], left, right)
        if node["op"] in {"div", "rem"}:
            return self._compile_checked_division(node_hash, node["op"], left, right)
        return (
            f"(i64.extend_i32_u ({'i64.lt_s' if node['op'] == 'lt' else 'i64.eq'} {left} {right}))"
        )

    def _compile_checked_arithmetic(self, node_hash, operator, left, right) -> str:
        site = self._register_trap(node_hash, "E_OVERFLOW", f"i64 {operator} overflow")
        left_local, right_local, result_local = self._new_temporary_locals("a", "b", "r")
        a, b, result = (f"(local.get ${n})" for n in (left_local, right_local, result_local))
        instruction = {"add": "i64.add", "sub": "i64.sub", "mul": "i64.mul"}[operator]
        trap = self._trap(site)
        if operator == "add":
            condition = (
                f"(i64.lt_s (i64.and (i64.xor {a} {result}) (i64.xor {b} {result})) (i64.const 0))"
            )
            checks = f"(if {condition} (then {trap}))"
        elif operator == "sub":
            condition = (
                f"(i64.lt_s (i64.and (i64.xor {a} {b}) (i64.xor {a} {result})) (i64.const 0))"
            )
            checks = f"(if {condition} (then {trap}))"
        else:
            exceptional = (
                f"(i32.or (i32.and (i64.eq {a} (i64.const -9223372036854775808)) "
                f"(i64.eq {b} (i64.const -1))) (i32.and (i64.eq {b} "
                f"(i64.const -9223372036854775808)) (i64.eq {a} (i64.const -1))))"
            )
            mismatch = f"(i64.ne (i64.div_s {result} {a}) {b})"
            checks = (
                f"(if {exceptional} (then {trap})) "
                f"(if (i64.ne {a} (i64.const 0)) "
                f"(then (if {mismatch} (then {trap}))))"
            )
        return (
            f"(block (result i64) (local.set ${left_local} {left}) "
            f"(local.set ${right_local} {right}) "
            f"(local.set ${result_local} ({instruction} {a} {b})) "
            f"{checks} {result})"
        )

    def _compile_checked_division(self, node_hash, operator, left, right) -> str:
        zero_site = self._register_trap(node_hash, "E_DIV_ZERO", f"i64 {operator} by zero")
        left_local, right_local = self._new_temporary_locals("a", "b")
        a, b = (f"(local.get ${n})" for n in (left_local, right_local))
        checks = f"(if (i64.eqz {b}) (then {self._trap(zero_site)}))"
        if operator == "div":
            overflow_site = self._register_trap(node_hash, "E_OVERFLOW", "i64 div overflow")
            overflow = (
                f"(i32.and (i64.eq {a} (i64.const -9223372036854775808)) "
                f"(i64.eq {b} (i64.const -1)))"
            )
            checks += f" (if {overflow} (then {self._trap(overflow_site)}))"
        instruction = "i64.div_s" if operator == "div" else "i64.rem_s"
        return (
            f"(block (result i64) (local.set ${left_local} {left}) "
            f"(local.set ${right_local} {right}) {checks} ({instruction} {a} {b}))"
        )

    def _compile_streq(self, node, current_function, let_locals, let_types) -> str:
        left_local, right_local, size_local = self._new_temporary_locals("a", "b", "n")
        left = self._compile_expression(node["a"], current_function, let_locals, let_types)
        right = self._compile_expression(node["b"], current_function, let_locals, let_types)
        a, b, size = (f"(local.get ${n})" for n in (left_local, right_local, size_local))
        return (
            f"(block (result i64) (local.set ${left_local} {left}) "
            f"(local.set ${right_local} {right}) (local.set ${size_local} "
            f"(i64.extend_i32_u (i32.load (i32.wrap_i64 {a})))) "
            f"(if (result i64) (i32.ne (i32.wrap_i64 {size}) "
            f"(i32.load (i32.wrap_i64 {b}))) (then (i64.const 0)) "
            f"(else (call $__bytes_equal (i64.add {a} (i64.const 8)) "
            f"(i64.add {b} (i64.const 8)) {size}))))"
        )

    def _compile_concat(self, node_hash, node, current_function, let_locals, let_types) -> str:
        names = self._new_temporary_locals("a", "b", "an", "bn", "n", "out")
        la, lb, lan, lbn, ln, lout = names
        a, b, an, bn, total, out = (f"(local.get ${name})" for name in names)
        left = self._compile_expression(node["a"], current_function, let_locals, let_types)
        right = self._compile_expression(node["b"], current_function, let_locals, let_types)
        limit_site = self._register_trap(node_hash, "E_STR_LIMIT", "concat exceeds 1 MiB")
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        fast = (
            f"(if (i64.gt_u {total} (i64.const {MAX_STRING_BYTES})) "
            f"(then {self._trap(limit_site)})) "
            f"(local.set ${lout} (call $__alloc "
            f"(i64.and (i64.add {total} (i64.const 15)) (i64.const -8)) "
            f"(i32.const {memory_site}))) "
            f"(i32.store (i32.wrap_i64 {out}) (i32.wrap_i64 {total})) "
            f"(i32.store offset=4 (i32.wrap_i64 {out}) "
            f"(i32.add (i32.load offset=4 (i32.wrap_i64 {a})) "
            f"(i32.load offset=4 (i32.wrap_i64 {b})))) "
            f"(memory.copy (i32.wrap_i64 (i64.add {out} (i64.const 8))) "
            f"(i32.wrap_i64 (i64.add {a} (i64.const 8))) (i32.wrap_i64 {an})) "
            f"(memory.copy (i32.wrap_i64 (i64.add (i64.add {out} (i64.const 8)) {an})) "
            f"(i32.wrap_i64 (i64.add {b} (i64.const 8))) (i32.wrap_i64 {bn})) {out}"
        )
        slow = (
            f"(local.set ${lout} (call $normalize_concat {a} {b})) "
            f"(if (i64.eq {out} (i64.const -2)) (then {self._trap(limit_site)})) "
            f"(if (i64.eq {out} (i64.const -1)) (then {self._trap(memory_site)})) {out}"
        )
        return (
            f"(block (result i64) (local.set ${la} {left}) (local.set ${lb} {right}) "
            f"(local.set ${lan} (i64.extend_i32_u (i32.load (i32.wrap_i64 {a})))) "
            f"(local.set ${lbn} (i64.extend_i32_u (i32.load (i32.wrap_i64 {b})))) "
            f"(local.set ${ln} (i64.add {an} {bn})) "
            f"(if (result i64) (i32.or (i64.eqz {bn}) "
            f"(i32.lt_u (i32.load8_u (i32.wrap_i64 (i64.add {b} (i64.const 8)))) "
            f"(i32.const 204))) (then {fast}) (else {slow})))"
        )

    def _compile_repeat(self, node_hash, node, current_function, let_locals, let_types) -> str:
        names = self._new_temporary_locals("s", "count", "bytes", "total", "out", "i")
        ls, lc, lb, lt, lo, li = names
        string, count, size, total, out, index = (f"(local.get ${n})" for n in names)
        source = self._compile_expression(node["s"], current_function, let_locals, let_types)
        repetitions = self._compile_expression(node["n"], current_function, let_locals, let_types)
        bad_site = self._register_trap(node_hash, "E_BAD_COUNT", "repeat count is negative")
        limit_site = self._register_trap(node_hash, "E_STR_LIMIT", "repeat exceeds 1 MiB")
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        make = (
            f"(local.set ${lt} (i64.mul {size} {count})) "
            f"(local.set ${lo} (call $__alloc "
            f"(i64.and (i64.add {total} (i64.const 15)) (i64.const -8)) "
            f"(i32.const {memory_site}))) "
            f"(i32.store (i32.wrap_i64 {out}) (i32.wrap_i64 {total})) "
            f"(i32.store offset=4 (i32.wrap_i64 {out}) "
            f"(i32.wrap_i64 (i64.mul (i64.extend_i32_u "
            f"(i32.load offset=4 (i32.wrap_i64 {string}))) {count}))) "
            f"(block $done (loop $copy (br_if $done (i64.ge_u {index} {count})) "
            f"(memory.copy (i32.wrap_i64 (i64.add (i64.add {out} (i64.const 8)) "
            f"(i64.mul {index} {size}))) (i32.wrap_i64 "
            f"(i64.add {string} (i64.const 8))) (i32.wrap_i64 {size})) "
            f"(local.set ${li} (i64.add {index} (i64.const 1))) (br $copy))) {out}"
        )
        return (
            f"(block (result i64) (local.set ${ls} {source}) (local.set ${lc} {repetitions}) "
            f"(if (i64.lt_s {count} (i64.const 0)) (then {self._trap(bad_site)})) "
            f"(local.set ${lb} (i64.extend_i32_u (i32.load (i32.wrap_i64 {string})))) "
            f"(if (result i64) (i64.eqz {size}) (then {string}) "
            f"(else (if (result i64) (i64.gt_u {count} "
            f"(i64.div_u (i64.const {MAX_STRING_BYTES}) {size})) "
            f"(then {self._trap(limit_site)} (i64.const 0)) (else {make})))))"
        )

    def _compile_heap_value(self, node_hash, node, current_function, let_locals, let_types) -> str:
        kind = node["k"]
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        pointer_local = self._new_temporary_locals("obj")[0]
        pointer = f"(local.get ${pointer_local})"
        if kind == "none":
            first, second = "(i64.const 0)", "(i64.const 0)"
        elif kind in {"some", "ok", "err"}:
            payload = self._compile_expression(node["v"], current_function, let_locals, let_types)
            first = "(i64.const 1)" if kind in {"some", "err"} else "(i64.const 0)"
            second = payload
        else:
            first = self._compile_expression(node["code"], current_function, let_locals, let_types)
            second = self._compile_expression(
                node["detail"], current_function, let_locals, let_types
            )
        return (
            f"(block (result i64) (local.set ${pointer_local} "
            f"(call $__alloc (i64.const 16) (i32.const {memory_site}))) "
            f"(i64.store (i32.wrap_i64 {pointer}) {first}) "
            f"(i64.store offset=8 (i32.wrap_i64 {pointer}) {second}) {pointer})"
        )

    def _record_declaration(self, node_hash: str, record_type: Type) -> tuple[str, dict]:
        if not isinstance(record_type, dict) or set(record_type) != {"rec"}:
            raise GraphError(node_hash, "E_TYPE_MISMATCH", "record expression expected")
        declaration_hash = record_type["rec"]
        declaration = self.load(declaration_hash)
        if declaration["k"] != "rec":
            raise GraphError(
                declaration_hash,
                "E_TYPE_MISMATCH",
                "record type must reference a rec declaration",
            )
        return declaration_hash, declaration

    @staticmethod
    def _check_field(node_hash: str, index: int, arity: int) -> None:
        if index >= arity:
            raise GraphError(
                node_hash,
                "E_BAD_FIELD",
                f"field index {index} is outside record arity {arity}",
            )

    def _compile_record(self, node_hash, node, current_function, let_locals, let_types) -> str:
        declaration = self.load(node["decl"])
        if declaration["k"] != "rec":
            raise GraphError(
                node["decl"],
                "E_TYPE_MISMATCH",
                "mk.decl must reference a rec declaration",
            )
        arity = len(declaration["fields"])
        self._check_arity(node_hash, arity, node["fields"])
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        pointer_local = self._new_temporary_locals("rec")[0]
        pointer = f"(local.get ${pointer_local})"
        stores = [f"(i64.store (i32.wrap_i64 {pointer}) (i64.const {arity}))"]
        for index, field in enumerate(node["fields"]):
            value = self._compile_expression(field, current_function, let_locals, let_types)
            stores.append(
                f"(i64.store offset={8 * (index + 1)} (i32.wrap_i64 {pointer}) {value})"
            )
        size = 8 * (arity + 1)
        return (
            f"(block (result i64) (local.set ${pointer_local} "
            f"(call $__alloc (i64.const {size}) (i32.const {memory_site}))) "
            f"{' '.join(stores)} {pointer})"
        )

    def _compile_record_get(self, node_hash, node, current_function, let_locals, let_types) -> str:
        record_type = self._expression_type(node["r"], current_function, let_types)
        _, declaration = self._record_declaration(node_hash, record_type)
        self._check_field(node_hash, node["i"], len(declaration["fields"]))
        record = self._compile_expression(node["r"], current_function, let_locals, let_types)
        return f"(i64.load offset={8 * (node['i'] + 1)} (i32.wrap_i64 {record}))"

    def _compile_record_with(self, node_hash, node, current_function, let_locals, let_types) -> str:
        record_type = self._expression_type(node["r"], current_function, let_types)
        _, declaration = self._record_declaration(node_hash, record_type)
        arity = len(declaration["fields"])
        self._check_field(node_hash, node["i"], arity)
        record_local, value_local, output_local = self._new_temporary_locals("rec", "value", "out")
        record = f"(local.get ${record_local})"
        value = f"(local.get ${value_local})"
        output = f"(local.get ${output_local})"
        compiled_record = self._compile_expression(
            node["r"], current_function, let_locals, let_types
        )
        compiled_value = self._compile_expression(
            node["v"], current_function, let_locals, let_types
        )
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        size = 8 * (arity + 1)
        return (
            f"(block (result i64) (local.set ${record_local} {compiled_record}) "
            f"(local.set ${value_local} {compiled_value}) "
            f"(local.set ${output_local} "
            f"(call $__alloc (i64.const {size}) (i32.const {memory_site}))) "
            f"(memory.copy (i32.wrap_i64 {output}) (i32.wrap_i64 {record}) "
            f"(i32.const {size})) "
            f"(i64.store offset={8 * (node['i'] + 1)} (i32.wrap_i64 {output}) {value}) "
            f"{output})"
        )

    def _compile_list(self, node_hash, node, current_function, let_locals, let_types) -> str:
        length = len(node["items"])
        if length > MAX_LIST_LENGTH:
            raise GraphError(
                node_hash,
                "E_LIST_LIMIT",
                f"list literal has {length} elements; maximum is {MAX_LIST_LENGTH}",
            )
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        pointer_local = self._new_temporary_locals("list")[0]
        pointer = f"(local.get ${pointer_local})"
        stores = [f"(i64.store (i32.wrap_i64 {pointer}) (i64.const {length}))"]
        for index, item in enumerate(node["items"]):
            value = self._compile_expression(item, current_function, let_locals, let_types)
            stores.append(
                f"(i64.store offset={8 * (index + 1)} (i32.wrap_i64 {pointer}) {value})"
            )
        return (
            f"(block (result i64) (local.set ${pointer_local} "
            f"(call $__alloc (i64.const {8 * (length + 1)}) "
            f"(i32.const {memory_site}))) {' '.join(stores)} {pointer})"
        )

    def _compile_list_at(self, node_hash, node, current_function, let_locals, let_types) -> str:
        list_local, index_local, output_local = self._new_temporary_locals("list", "index", "out")
        list_value, index, output = (
            f"(local.get ${name})" for name in (list_local, index_local, output_local)
        )
        compiled_list = self._compile_expression(
            node["l"], current_function, let_locals, let_types
        )
        compiled_index = self._compile_expression(
            node["idx"], current_function, let_locals, let_types
        )
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        in_range = (
            f"(i32.and (i64.ge_s {index} (i64.const 0)) "
            f"(i64.lt_u {index} (i64.load (i32.wrap_i64 {list_value}))))"
        )
        payload = (
            f"(i64.load (i32.wrap_i64 (i64.add {list_value} "
            f"(i64.mul (i64.add {index} (i64.const 1)) (i64.const 8)))))"
        )
        return (
            f"(block (result i64) (local.set ${list_local} {compiled_list}) "
            f"(local.set ${index_local} {compiled_index}) "
            f"(local.set ${output_local} "
            f"(call $__alloc (i64.const 16) (i32.const {memory_site}))) "
            f"(if {in_range} (then "
            f"(i64.store (i32.wrap_i64 {output}) (i64.const 1)) "
            f"(i64.store offset=8 (i32.wrap_i64 {output}) {payload})) "
            f"(else (i64.store (i32.wrap_i64 {output}) (i64.const 0)) "
            f"(i64.store offset=8 (i32.wrap_i64 {output}) (i64.const 0)))) {output})"
        )

    def _compile_list_append(
        self, node_hash, node, current_function, let_locals, let_types
    ) -> str:
        names = self._new_temporary_locals("a", "b", "an", "bn", "n", "out")
        la, lb, lan, lbn, ln, lout = names
        a, b, an, bn, length, output = (f"(local.get ${name})" for name in names)
        left = self._compile_expression(node["a"], current_function, let_locals, let_types)
        right = self._compile_expression(node["b"], current_function, let_locals, let_types)
        limit_site = self._register_trap(
            node_hash, "E_LIST_LIMIT", f"append exceeds {MAX_LIST_LENGTH} elements"
        )
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        return (
            f"(block (result i64) (local.set ${la} {left}) (local.set ${lb} {right}) "
            f"(local.set ${lan} (i64.load (i32.wrap_i64 {a}))) "
            f"(local.set ${lbn} (i64.load (i32.wrap_i64 {b}))) "
            f"(local.set ${ln} (i64.add {an} {bn})) "
            f"(if (i64.gt_u {length} (i64.const {MAX_LIST_LENGTH})) "
            f"(then {self._trap(limit_site)})) "
            f"(local.set ${lout} (call $__alloc "
            f"(i64.mul (i64.add {length} (i64.const 1)) (i64.const 8)) "
            f"(i32.const {memory_site}))) "
            f"(i64.store (i32.wrap_i64 {output}) {length}) "
            f"(memory.copy (i32.wrap_i64 (i64.add {output} (i64.const 8))) "
            f"(i32.wrap_i64 (i64.add {a} (i64.const 8))) "
            f"(i32.wrap_i64 (i64.mul {an} (i64.const 8)))) "
            f"(memory.copy (i32.wrap_i64 (i64.add (i64.add {output} (i64.const 8)) "
            f"(i64.mul {an} (i64.const 8)))) "
            f"(i32.wrap_i64 (i64.add {b} (i64.const 8))) "
            f"(i32.wrap_i64 (i64.mul {bn} (i64.const 8)))) {output})"
        )

    def _compile_list_reverse(
        self, node_hash, node, current_function, let_locals, let_types
    ) -> str:
        list_local, length_local, output_local, index_local = self._new_temporary_locals(
            "list", "n", "out", "i"
        )
        source, length, output, index = (
            f"(local.get ${name})"
            for name in (list_local, length_local, output_local, index_local)
        )
        compiled = self._compile_expression(node["l"], current_function, let_locals, let_types)
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        source_item = (
            f"(i64.load (i32.wrap_i64 (i64.add {source} (i64.mul "
            f"(i64.sub {length} {index}) (i64.const 8)))))"
        )
        output_address = (
            f"(i32.wrap_i64 (i64.add {output} (i64.mul "
            f"(i64.add {index} (i64.const 1)) (i64.const 8))))"
        )
        return (
            f"(block (result i64) (local.set ${list_local} {compiled}) "
            f"(local.set ${length_local} (i64.load (i32.wrap_i64 {source}))) "
            f"(local.set ${output_local} (call $__alloc "
            f"(i64.mul (i64.add {length} (i64.const 1)) (i64.const 8)) "
            f"(i32.const {memory_site}))) "
            f"(i64.store (i32.wrap_i64 {output}) {length}) "
            f"(block $reverse_done (loop $reverse_loop "
            f"(br_if $reverse_done (i64.ge_u {index} {length})) "
            f"(i64.store {output_address} {source_item}) "
            f"(local.set ${index_local} (i64.add {index} (i64.const 1))) "
            f"(br $reverse_loop))) {output})"
        )

    def _combinator_target(self, node_hash: str, reference_hash: str, arity: int) -> str:
        reference = self.load(reference_hash)
        if reference["k"] != "fnref":
            raise GraphError(node_hash, "E_TYPE_MISMATCH", "combinator f must be a fnref")
        target_hash = reference["fn"]
        target = self.load(target_hash)
        if target["k"] != "fn":
            raise GraphError(target_hash, "E_TYPE_MISMATCH", "fnref must reference a fn node")
        self._check_arity(node_hash, arity, target["params"])
        if target_hash not in self.functions and target_hash not in self.pending:
            self.pending.append(target_hash)
        return target_hash

    def _compile_combinator(
        self, node_hash, node, current_function, let_locals, let_types
    ) -> str:
        kind = node["k"]
        target_hash = self._combinator_target(
            node_hash, node["f"], 3 if kind == "fold" else 2
        )
        if kind == "and_then":
            return self._compile_and_then(
                node, target_hash, current_function, let_locals, let_types
            )
        if kind == "fold":
            return self._compile_fold(node, target_hash, current_function, let_locals, let_types)
        if kind == "sort_by":
            return self._compile_sort_by(
                node_hash, node, target_hash, current_function, let_locals, let_types
            )
        return self._compile_map_or_filter(
            node_hash, node, target_hash, current_function, let_locals, let_types
        )

    def _compile_map_or_filter(
        self, node_hash, node, target_hash, current_function, let_locals, let_types
    ) -> str:
        names = self._new_temporary_locals("list", "ctx", "n", "out", "i", "used", "x")
        ll, lc, ln, lo, li, lu, lx = names
        source, ctx, length, output, index, used, item = (
            f"(local.get ${name})" for name in names
        )
        compiled_list = self._compile_expression(
            node["l"], current_function, let_locals, let_types
        )
        compiled_ctx = self._compile_expression(
            node["ctx"], current_function, let_locals, let_types
        )
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")
        load_item = (
            f"(i64.load (i32.wrap_i64 (i64.add {source} (i64.mul "
            f"(i64.add {index} (i64.const 1)) (i64.const 8)))))"
        )
        destination = (
            f"(i32.wrap_i64 (i64.add {output} (i64.mul "
            f"(i64.add {used} (i64.const 1)) (i64.const 8))))"
        )
        call = f"(call ${_function_name(target_hash)} {ctx} {item})"
        if node["k"] == "map":
            body = (
                f"(i64.store {destination} {call}) "
                f"(local.set ${lu} (i64.add {used} (i64.const 1)))"
            )
        else:
            body = (
                f"(if (i64.ne {call} (i64.const 0)) (then "
                f"(i64.store {destination} {item}) "
                f"(local.set ${lu} (i64.add {used} (i64.const 1)))))"
            )
        return (
            f"(block (result i64) (local.set ${ll} {compiled_list}) "
            f"(local.set ${lc} {compiled_ctx}) "
            f"(local.set ${ln} (i64.load (i32.wrap_i64 {source}))) "
            f"(local.set ${lo} (call $__alloc "
            f"(i64.mul (i64.add {length} (i64.const 1)) (i64.const 8)) "
            f"(i32.const {memory_site}))) "
            f"(block $combine_done (loop $combine_loop "
            f"(br_if $combine_done (i64.ge_u {index} {length})) "
            f"(local.set ${lx} {load_item}) {body} "
            f"(local.set ${li} (i64.add {index} (i64.const 1))) "
            f"(br $combine_loop))) "
            f"(i64.store (i32.wrap_i64 {output}) {used}) {output})"
        )

    def _compile_fold(self, node, target_hash, current_function, let_locals, let_types) -> str:
        names = self._new_temporary_locals("list", "ctx", "n", "i", "acc")
        ll, lc, ln, li, la = names
        source, ctx, length, index, accumulator = (
            f"(local.get ${name})" for name in names
        )
        compiled_list = self._compile_expression(
            node["l"], current_function, let_locals, let_types
        )
        compiled_ctx = self._compile_expression(
            node["ctx"], current_function, let_locals, let_types
        )
        initial = self._compile_expression(node["init"], current_function, let_locals, let_types)
        item = (
            f"(i64.load (i32.wrap_i64 (i64.add {source} (i64.mul "
            f"(i64.add {index} (i64.const 1)) (i64.const 8)))))"
        )
        return (
            f"(block (result i64) (local.set ${ll} {compiled_list}) "
            f"(local.set ${lc} {compiled_ctx}) (local.set ${la} {initial}) "
            f"(local.set ${ln} (i64.load (i32.wrap_i64 {source}))) "
            f"(block $fold_done (loop $fold_loop "
            f"(br_if $fold_done (i64.ge_u {index} {length})) "
            f"(local.set ${la} (call ${_function_name(target_hash)} "
            f"{ctx} {accumulator} {item})) "
            f"(local.set ${li} (i64.add {index} (i64.const 1))) "
            f"(br $fold_loop))) {accumulator})"
        )

    def _compile_and_then(
        self, node, target_hash, current_function, let_locals, let_types
    ) -> str:
        result_local, ctx_local = self._new_temporary_locals("result", "ctx")
        result, ctx = (f"(local.get ${name})" for name in (result_local, ctx_local))
        compiled_result = self._compile_expression(
            node["r"], current_function, let_locals, let_types
        )
        compiled_ctx = self._compile_expression(
            node["ctx"], current_function, let_locals, let_types
        )
        payload = f"(i64.load offset=8 (i32.wrap_i64 {result}))"
        return (
            f"(block (result i64) (local.set ${result_local} {compiled_result}) "
            f"(local.set ${ctx_local} {compiled_ctx}) "
            f"(if (result i64) (i64.eqz (i64.load (i32.wrap_i64 {result}))) "
            f"(then (call ${_function_name(target_hash)} {ctx} {payload})) "
            f"(else {result})))"
        )

    def _compile_sort_by(
        self, node_hash, node, target_hash, current_function, let_locals, let_types
    ) -> str:
        names = self._new_temporary_locals(
            "list",
            "ctx",
            "n",
            "out",
            "keys",
            "tmp_values",
            "tmp_keys",
            "width",
            "left",
            "mid",
            "right",
            "i",
            "j",
            "k",
            "value",
            "key",
        )
        (
            ll,
            lc,
            ln,
            lo,
            lkeys,
            ltmp_values,
            ltmp_keys,
            lwidth,
            lleft,
            lmid,
            lright,
            li,
            lj,
            lk,
            lvalue,
            lkey,
        ) = names
        (
            source,
            ctx,
            length,
            output,
            keys,
            tmp_values,
            tmp_keys,
            width,
            left,
            mid,
            right,
            index_i,
            index_j,
            index_k,
            chosen_value,
            chosen_key,
        ) = (f"(local.get ${name})" for name in names)
        label = ln
        compiled_list = self._compile_expression(
            node["l"], current_function, let_locals, let_types
        )
        compiled_ctx = self._compile_expression(
            node["ctx"], current_function, let_locals, let_types
        )
        memory_site = self._register_trap(node_hash, "E_MEMORY", "64 MiB arena exhausted")

        def load(base: str, index: str, header: bool = False) -> str:
            offset = f"(i64.add {index} (i64.const 1))" if header else index
            return (
                f"(i64.load (i32.wrap_i64 (i64.add {base} "
                f"(i64.mul {offset} (i64.const 8)))))"
            )

        def address(base: str, index: str, header: bool = False) -> str:
            offset = f"(i64.add {index} (i64.const 1))" if header else index
            return (
                f"(i32.wrap_i64 (i64.add {base} "
                f"(i64.mul {offset} (i64.const 8))))"
            )

        initialize = (
            f"(block ${label}_init_done (loop ${label}_init "
            f"(br_if ${label}_init_done (i64.ge_u {index_i} {length})) "
            f"(local.set ${lvalue} {load(source, index_i, header=True)}) "
            f"(i64.store {address(output, index_i, header=True)} {chosen_value}) "
            f"(i64.store {address(keys, index_i)} "
            f"(call ${_function_name(target_hash)} {ctx} {chosen_value})) "
            f"(local.set ${li} (i64.add {index_i} (i64.const 1))) "
            f"(br ${label}_init)))"
        )
        choose_left = (
            f"(if (result i64) (i64.lt_u {index_i} {mid}) "
            f"(then (if (result i64) (i64.ge_u {index_j} {right}) "
            f"(then (i64.const 1)) "
            f"(else (i64.extend_i32_u (i64.le_s {load(keys, index_i)} "
            f"{load(keys, index_j)}))))) (else (i64.const 0)))"
        )
        select = (
            f"(if (i64.ne {choose_left} (i64.const 0)) (then "
            f"(local.set ${lvalue} {load(output, index_i, header=True)}) "
            f"(local.set ${lkey} {load(keys, index_i)}) "
            f"(local.set ${li} (i64.add {index_i} (i64.const 1)))) (else "
            f"(local.set ${lvalue} {load(output, index_j, header=True)}) "
            f"(local.set ${lkey} {load(keys, index_j)}) "
            f"(local.set ${lj} (i64.add {index_j} (i64.const 1)))))"
        )
        merge = (
            f"(block ${label}_merge_done (loop ${label}_merge "
            f"(br_if ${label}_merge_done (i64.ge_u {index_k} {right})) "
            f"{select} "
            f"(i64.store {address(tmp_values, index_k)} {chosen_value}) "
            f"(i64.store {address(tmp_keys, index_k)} {chosen_key}) "
            f"(local.set ${lk} (i64.add {index_k} (i64.const 1))) "
            f"(br ${label}_merge)))"
        )
        runs = (
            f"(block ${label}_runs_done (loop ${label}_runs "
            f"(br_if ${label}_runs_done (i64.ge_u {left} {length})) "
            f"(local.set ${lmid} (if (result i64) "
            f"(i64.lt_u (i64.add {left} {width}) {length}) "
            f"(then (i64.add {left} {width})) (else {length}))) "
            f"(local.set ${lright} (if (result i64) "
            f"(i64.lt_u (i64.add {left} (i64.mul {width} (i64.const 2))) {length}) "
            f"(then (i64.add {left} (i64.mul {width} (i64.const 2)))) "
            f"(else {length}))) "
            f"(local.set ${li} {left}) (local.set ${lj} {mid}) "
            f"(local.set ${lk} {left}) {merge} "
            f"(local.set ${lleft} {right}) (br ${label}_runs)))"
        )
        passes = (
            f"(block ${label}_sort_done (loop ${label}_sort "
            f"(br_if ${label}_sort_done (i64.ge_u {width} {length})) "
            f"(local.set ${lleft} (i64.const 0)) {runs} "
            f"(memory.copy (i32.wrap_i64 (i64.add {output} (i64.const 8))) "
            f"(i32.wrap_i64 {tmp_values}) "
            f"(i32.wrap_i64 (i64.mul {length} (i64.const 8)))) "
            f"(memory.copy (i32.wrap_i64 {keys}) (i32.wrap_i64 {tmp_keys}) "
            f"(i32.wrap_i64 (i64.mul {length} (i64.const 8)))) "
            f"(local.set ${lwidth} (i64.mul {width} (i64.const 2))) "
            f"(br ${label}_sort)))"
        )
        list_size = f"(i64.mul (i64.add {length} (i64.const 1)) (i64.const 8))"
        data_size = f"(i64.mul {length} (i64.const 8))"
        return (
            f"(block (result i64) (local.set ${ll} {compiled_list}) "
            f"(local.set ${lc} {compiled_ctx}) "
            f"(local.set ${ln} (i64.load (i32.wrap_i64 {source}))) "
            f"(local.set ${lo} (call $__alloc {list_size} (i32.const {memory_site}))) "
            f"(local.set ${lkeys} (call $__alloc {data_size} (i32.const {memory_site}))) "
            f"(local.set ${ltmp_values} "
            f"(call $__alloc {data_size} (i32.const {memory_site}))) "
            f"(local.set ${ltmp_keys} "
            f"(call $__alloc {data_size} (i32.const {memory_site}))) "
            f"(i64.store (i32.wrap_i64 {output}) {length}) {initialize} "
            f"(local.set ${lwidth} (i64.const 1)) {passes} {output})"
        )

    def _compile_match(self, node_hash, node, current_function, let_locals, let_types) -> str:
        scrutinized_type = self._expression_type(node["s"], current_function, let_types)
        constructor = (
            next(iter(scrutinized_type), None) if isinstance(scrutinized_type, dict) else None
        )
        if constructor not in {"option", "result"}:
            raise GraphError(
                node_hash, "E_TYPE_MISMATCH", "match.s must have option or result type"
            )
        if len(node["arms"]) != 2:
            raise GraphError(node_hash, "E_NON_EXHAUSTIVE", "match must have exactly 2 arms")
        object_local = self._new_temporary_locals("match")[0]
        object_value = f"(local.get ${object_local})"
        scrutinized = self._compile_expression(node["s"], current_function, let_locals, let_types)

        def arm(index: int, binds: bool, payload_type: Type) -> str:
            if not binds:
                return self._compile_expression(
                    node["arms"][index], current_function, let_locals, let_types
                )
            local = self._ensure_let_local(len(let_locals))
            body = self._compile_expression(
                node["arms"][index],
                current_function,
                (*let_locals, local),
                (*let_types, payload_type),
            )
            return (
                f"(block (result i64) (local.set ${local} "
                f"(i64.load offset=8 (i32.wrap_i64 {object_value}))) {body})"
            )

        payload = scrutinized_type[constructor]
        first = arm(0, constructor == "result", payload)
        second = arm(1, True, payload if constructor == "option" else "error")
        return (
            f"(block (result i64) (local.set ${object_local} {scrutinized}) "
            f"(if (result i64) (i64.eqz (i64.load (i32.wrap_i64 {object_value}))) "
            f"(then {first}) (else {second})))"
        )

    def _expression_type(
        self, node_hash: str, current_function: str, let_types: tuple[Type, ...]
    ) -> Type:
        node = self.load(node_hash)
        kind = node["k"]
        if kind in {"lit", "length", "len", "now"} or (
            kind == "bin" and node["op"] not in {"lt", "eq"}
        ):
            return "i64"
        if kind in {"true", "false", "not", "and", "or", "streq"} or (
            kind == "bin" and node["op"] in {"lt", "eq"}
        ):
            return "bool"
        if kind in {"str", "concat", "repeat"}:
            return "string"
        if kind == "atom":
            return "atom"
        if kind == "error":
            return "error"
        if kind in {"unit", "kv_put", "kv_del", "print"}:
            return "unit"
        if kind == "kv_get":
            payload = self.ns_type(node["ns"]) if self.ns_type is not None else "unit"
            return {"option": payload}
        if kind == "kv_list":
            payload = self.ns_type(node["ns"]) if self.ns_type is not None else "unit"
            return {"list": payload}
        if kind == "kv_keys":
            return {"list": "string"}
        if kind == "param":
            return self.load(current_function)["params"][node["i"]]
        if kind == "var":
            return let_types[-1 - node["i"]]
        if kind == "let":
            value_type = self._expression_type(node["v"], current_function, let_types)
            return self._expression_type(node["body"], current_function, (*let_types, value_type))
        if kind == "if":
            return self._expression_type(node["t"], current_function, let_types)
        if kind == "call":
            return self.load(node["fn"])["ret"]
        if kind == "selfcall":
            return self.load(current_function)["ret"]
        if kind == "none":
            return {"option": node["t"]}
        if kind == "some":
            return {"option": self._expression_type(node["v"], current_function, let_types)}
        if kind == "ok":
            return {"result": self._expression_type(node["v"], current_function, let_types)}
        if kind == "err":
            return {"result": node["t"]}
        if kind == "mk":
            declaration = self.load(node["decl"])
            if declaration["k"] != "rec":
                raise GraphError(
                    node["decl"],
                    "E_TYPE_MISMATCH",
                    "mk.decl must reference a rec declaration",
                )
            return {"rec": node["decl"]}
        if kind == "get":
            record_type = self._expression_type(node["r"], current_function, let_types)
            _, declaration = self._record_declaration(node_hash, record_type)
            self._check_field(node_hash, node["i"], len(declaration["fields"]))
            return declaration["fields"][node["i"]]
        if kind == "with":
            record_type = self._expression_type(node["r"], current_function, let_types)
            _, declaration = self._record_declaration(node_hash, record_type)
            self._check_field(node_hash, node["i"], len(declaration["fields"]))
            return record_type
        if kind == "list":
            return {"list": node["t"]}
        if kind == "at":
            list_type = self._expression_type(node["l"], current_function, let_types)
            if not isinstance(list_type, dict) or set(list_type) != {"list"}:
                raise GraphError(node_hash, "E_TYPE_MISMATCH", "at.l must have list type")
            return {"option": list_type["list"]}
        if kind in {"append", "filter", "sort_by", "reverse"}:
            field = "a" if kind == "append" else "l"
            return self._expression_type(node[field], current_function, let_types)
        if kind in {"map", "and_then"}:
            reference = self.load(node["f"])
            if reference["k"] != "fnref":
                raise GraphError(node_hash, "E_TYPE_MISMATCH", "combinator f must be a fnref")
            target = self.load(reference["fn"])
            if target["k"] != "fn":
                raise GraphError(reference["fn"], "E_TYPE_MISMATCH", "fnref must reference a fn")
            return target["ret"]
        if kind == "fold":
            return self._expression_type(node["init"], current_function, let_types)
        if kind == "fnref":
            raise GraphError(
                node_hash,
                "E_FNREF_ESCAPE",
                "fnref may only be used directly as a combinator f argument",
            )
        if kind == "match":
            scrutinized = self._expression_type(node["s"], current_function, let_types)
            constructor = next(iter(scrutinized))
            if constructor == "option":
                return self._expression_type(node["arms"][0], current_function, let_types)
            return self._expression_type(
                node["arms"][0], current_function, (*let_types, scrutinized[constructor])
            )
        raise GraphError(node_hash, "E_TYPE_MISMATCH", f"cannot determine type of {kind}")

    def _static_string(self, value: str) -> int:
        raw = value.encode("utf-8")
        if raw in self.static_strings:
            return self.static_strings[raw]
        data = struct.pack("<II", len(raw), len(value)) + raw
        offset = self.static_end
        self.static_end += _align(len(data))
        if self.static_end > MEMORY_LIMIT:
            raise GraphError(None, "E_MEMORY", "static data exceeds the 64 MiB arena")
        self.static_strings[raw] = offset
        self.data_segments.append((offset, data))
        return offset

    def _ensure_let_local(self, depth: int) -> str:
        name = f"let{depth}"
        if name not in self.function_locals:
            self.function_locals.append(name)
        return name

    def _new_temporary_locals(self, *suffixes: str) -> tuple[str, ...]:
        index = self.function_temporary_count
        self.function_temporary_count += 1
        names = tuple(f"tmp{index}_{suffix}" for suffix in suffixes)
        self.function_locals.extend(names)
        return names

    def _register_trap(self, node_hash: str, code: str, detail: str) -> int:
        site = len(self.trap_sites)
        self.trap_sites[site] = (node_hash, code, detail)
        return site

    @staticmethod
    def _trap(site: int) -> str:
        return f"(global.set $__trap_site (i32.const {site})) (unreachable)"

    def _compile_arguments(self, arguments, current_function, let_locals, let_types) -> str:
        compiled = [
            self._compile_expression(argument, current_function, let_locals, let_types)
            for argument in arguments
        ]
        return "".join(f" {argument}" for argument in compiled)

    @staticmethod
    def _check_arity(node_hash: str, expected: int, arguments: list[str]) -> None:
        if len(arguments) != expected:
            raise GraphError(
                node_hash, "E_ARITY", f"expected {expected} arguments, got {len(arguments)}"
            )


def _memory_objects(caller: wasmtime.Caller) -> tuple[wasmtime.Memory, wasmtime.Global]:
    memory = caller.get("memory")
    heap = caller.get("__heap")
    if not isinstance(memory, wasmtime.Memory) or not isinstance(heap, wasmtime.Global):
        raise RuntimeError("WASM runtime memory exports are unavailable")
    return memory, heap


def _host_allocate(
    memory: wasmtime.Memory, heap: wasmtime.Global, store: Any, size: int
) -> int | None:
    start = int(heap.value(store))
    end = start + _align(size)
    if end > MEMORY_LIMIT:
        return None
    required_pages = (end + PAGE_SIZE - 1) // PAGE_SIZE
    current_pages = memory.size(store)
    if required_pages > current_pages:
        try:
            memory.grow(store, required_pages - current_pages)
        except wasmtime.WasmtimeError:
            return None
    heap.set_value(store, end)
    return start


def _read_string(memory: wasmtime.Memory, store: Any, pointer: int) -> str:
    header = memory.read(store, pointer, pointer + 8)
    if len(header) != 8:
        raise ValueError("invalid string pointer")
    byte_length, _ = struct.unpack("<II", header)
    raw = bytes(memory.read(store, pointer + 8, pointer + 8 + byte_length))
    if len(raw) != byte_length:
        raise ValueError("invalid string length")
    return raw.decode("utf-8")


def _write_string(
    memory: wasmtime.Memory, heap: wasmtime.Global, store: Any, value: str
) -> int | None:
    raw = value.encode("utf-8")
    pointer = _host_allocate(memory, heap, store, 8 + len(raw))
    if pointer is None:
        return None
    memory.write(store, struct.pack("<II", len(raw), len(value)) + raw, pointer)
    return pointer


def _normalize_concat(caller: wasmtime.Caller, left: int, right: int) -> int:
    memory, heap = _memory_objects(caller)
    value = unicodedata.normalize(
        "NFC", _read_string(memory, caller, left) + _read_string(memory, caller, right)
    )
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        return -2
    pointer = _write_string(memory, heap, caller, value)
    return -1 if pointer is None else pointer


def to_wat(get: GetNode, entry: str) -> str:
    """Lower an entry function and every directly called function to a WAT module."""
    return _WatCompiler(get).compile(entry)


def _type_error(entry: str, role: str, expected: Type) -> GraphError:
    return GraphError(entry, "E_TYPE_MISMATCH", f"{role} must have type {expected}")


def _write_object(memory, heap, store, entry, first, second) -> int:
    pointer = _host_allocate(memory, heap, store, 16)
    if pointer is None:
        raise GraphError(entry, "E_MEMORY", "64 MiB arena exhausted")
    memory.write(store, struct.pack("<qq", first, second), pointer)
    return pointer


def _record_info(compiler: _WatCompiler, type_: Type) -> tuple[str, list[Type]]:
    declaration_hash = type_["rec"]
    declaration = compiler.load(declaration_hash)
    if declaration["k"] != "rec":
        raise GraphError(
            declaration_hash,
            "E_TYPE_MISMATCH",
            "record type must reference a rec declaration",
        )
    return declaration_hash, declaration["fields"]


def _record_field_names(
    declaration_hash: str, arity: int, field_names: FieldNames | None
) -> list[str | None]:
    if field_names is None:
        return [None] * arity
    return [field_names(declaration_hash, index) for index in range(arity)]


def _write_record(memory, heap, store, entry, fields: list[int]) -> int:
    pointer = _host_allocate(memory, heap, store, 8 * (len(fields) + 1))
    if pointer is None:
        raise GraphError(entry, "E_MEMORY", "64 MiB arena exhausted")
    memory.write(store, struct.pack(f"<{len(fields) + 1}q", len(fields), *fields), pointer)
    return pointer


def _write_list(memory, heap, store, entry, elements: list[int]) -> int:
    pointer = _host_allocate(memory, heap, store, 8 * (len(elements) + 1))
    if pointer is None:
        raise GraphError(entry, "E_MEMORY", "64 MiB arena exhausted")
    memory.write(store, struct.pack(f"<{len(elements) + 1}q", len(elements), *elements), pointer)
    return pointer


def _serialize(
    type_, value, memory, heap, store, entry, role, compiler, field_names
) -> int:
    if type_ == "i64":
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not I64_MIN <= value <= I64_MAX
        ):
            raise _type_error(entry, role, type_)
        return value
    if type_ == "bool":
        if not isinstance(value, bool):
            raise _type_error(entry, role, type_)
        return int(value)
    if type_ == "unit":
        if value is not None:
            raise _type_error(entry, role, type_)
        return 0
    if type_ == "string":
        if not isinstance(value, str):
            raise _type_error(entry, role, type_)
        try:
            normalized = unicodedata.normalize("NFC", value)
            raw = normalized.encode("utf-8")
        except UnicodeEncodeError:
            raise _type_error(entry, role, type_) from None
        if len(raw) > MAX_STRING_BYTES:
            raise GraphError(entry, "E_STR_LIMIT", f"{role} exceeds 1 MiB")
        pointer = _write_string(memory, heap, store, normalized)
        if pointer is None:
            raise GraphError(entry, "E_MEMORY", "64 MiB arena exhausted")
        return pointer
    if type_ == "atom":
        if (
            not isinstance(value, dict)
            or set(value) != {"atom"}
            or not isinstance(value["atom"], str)
            or ATOM_RE.fullmatch(value["atom"]) is None
        ):
            raise _type_error(entry, role, type_)
        pointer = _write_string(memory, heap, store, value["atom"])
        if pointer is None:
            raise GraphError(entry, "E_MEMORY", "64 MiB arena exhausted")
        return pointer
    if type_ == "error":
        if not isinstance(value, dict) or set(value) != {"code", "detail"}:
            raise _type_error(entry, role, type_)
        code = _serialize(
            "atom", value["code"], memory, heap, store, entry, f"{role}.code", compiler, field_names
        )
        detail = _serialize(
            {"option": "string"},
            value["detail"],
            memory,
            heap,
            store,
            entry,
            f"{role}.detail",
            compiler,
            field_names,
        )
        return _write_object(memory, heap, store, entry, code, detail)
    if not isinstance(type_, dict) or len(type_) != 1:
        raise _type_error(entry, role, type_)
    constructor, payload_type = next(iter(type_.items()))
    if constructor == "list":
        if not isinstance(value, list):
            raise _type_error(entry, role, type_)
        if len(value) > MAX_LIST_LENGTH:
            raise GraphError(
                entry,
                "E_LIST_LIMIT",
                f"{role} has {len(value)} elements; maximum is {MAX_LIST_LENGTH}",
            )
        elements = [
            _serialize(
                payload_type,
                item,
                memory,
                heap,
                store,
                entry,
                f"{role}[{index}]",
                compiler,
                field_names,
            )
            for index, item in enumerate(value)
        ]
        return _write_list(memory, heap, store, entry, elements)
    if constructor == "rec":
        declaration_hash, field_types = _record_info(compiler, type_)
        names = _record_field_names(declaration_hash, len(field_types), field_names)
        if all(name is not None for name in names):
            if not isinstance(value, dict) or set(value) != set(names):
                raise _type_error(entry, role, type_)
            values = [value[name] for name in names]
            roles = [f"{role}.{name}" for name in names]
        else:
            if not isinstance(value, list) or len(value) != len(field_types):
                raise _type_error(entry, role, type_)
            values = value
            roles = [f"{role}[{index}]" for index in range(len(field_types))]
        fields = [
            _serialize(
                field_type,
                field_value,
                memory,
                heap,
                store,
                entry,
                field_role,
                compiler,
                field_names,
            )
            for field_type, field_value, field_role in zip(
                field_types, values, roles, strict=True
            )
        ]
        return _write_record(memory, heap, store, entry, fields)
    if not isinstance(value, dict) or not isinstance(value.get("tag"), str):
        raise _type_error(entry, role, type_)
    tag = value["tag"]
    if constructor == "option" and tag == "none" and set(value) == {"tag"}:
        return _write_object(memory, heap, store, entry, 0, 0)
    payload_tag, encoded_tag = {"option": ("some", 1), "result": ("ok", 0)}.get(
        constructor, (None, None)
    )
    if tag == payload_tag and set(value) == {"tag", "v"}:
        payload = _serialize(
            payload_type,
            value["v"],
            memory,
            heap,
            store,
            entry,
            f"{role}.v",
            compiler,
            field_names,
        )
        return _write_object(memory, heap, store, entry, encoded_tag, payload)
    if constructor == "result" and tag == "err" and set(value) == {"tag", "v"}:
        payload = _serialize(
            "error",
            value["v"],
            memory,
            heap,
            store,
            entry,
            f"{role}.v",
            compiler,
            field_names,
        )
        return _write_object(memory, heap, store, entry, 1, payload)
    raise _type_error(entry, role, type_)


def _read_object(memory, store, pointer: int) -> tuple[int, int]:
    raw = memory.read(store, pointer, pointer + 16)
    if len(raw) != 16:
        raise ValueError("invalid heap object pointer")
    return struct.unpack("<qq", raw)


def _read_record(memory, store, pointer: int, arity: int) -> tuple[int, ...]:
    raw = memory.read(store, pointer, pointer + 8 * (arity + 1))
    if len(raw) != 8 * (arity + 1):
        raise ValueError("invalid record pointer")
    stored_arity, *fields = struct.unpack(f"<{arity + 1}q", raw)
    if stored_arity != arity:
        raise ValueError("record arity does not match its declaration")
    return tuple(fields)


def _read_list(memory, store, pointer: int) -> tuple[int, ...]:
    header = memory.read(store, pointer, pointer + 8)
    if len(header) != 8:
        raise ValueError("invalid list pointer")
    (length,) = struct.unpack("<q", header)
    if not 0 <= length <= MAX_LIST_LENGTH:
        raise ValueError("invalid list length")
    raw = memory.read(store, pointer + 8, pointer + 8 + 8 * length)
    if len(raw) != 8 * length:
        raise ValueError("invalid list elements")
    return struct.unpack(f"<{length}q", raw) if length else ()


def _deserialize(type_, value: int, memory, store, compiler, field_names):
    if type_ == "i64":
        return value
    if type_ == "bool":
        return bool(value)
    if type_ == "unit":
        return None
    if type_ == "string":
        return _read_string(memory, store, value)
    if type_ == "atom":
        return {"atom": _read_string(memory, store, value)}
    if type_ == "error":
        code, detail = _read_object(memory, store, value)
        return {
            "code": _deserialize("atom", code, memory, store, compiler, field_names),
            "detail": _deserialize(
                {"option": "string"}, detail, memory, store, compiler, field_names
            ),
        }
    constructor, payload_type = next(iter(type_.items()))
    if constructor == "list":
        return [
            _deserialize(payload_type, item, memory, store, compiler, field_names)
            for item in _read_list(memory, store, value)
        ]
    if constructor == "rec":
        declaration_hash, field_types = _record_info(compiler, type_)
        fields = _read_record(memory, store, value, len(field_types))
        decoded = [
            _deserialize(field_type, field, memory, store, compiler, field_names)
            for field_type, field in zip(field_types, fields, strict=True)
        ]
        names = _record_field_names(declaration_hash, len(field_types), field_names)
        if all(name is not None for name in names):
            return dict(zip(names, decoded, strict=True))
        return decoded
    tag, payload = _read_object(memory, store, value)
    if constructor == "option":
        if tag == 0:
            return {"tag": "none"}
        return {
            "tag": "some",
            "v": _deserialize(payload_type, payload, memory, store, compiler, field_names),
        }
    if tag == 0:
        return {
            "tag": "ok",
            "v": _deserialize(payload_type, payload, memory, store, compiler, field_names),
        }
    return {
        "tag": "err",
        "v": _deserialize("error", payload, memory, store, compiler, field_names),
    }


def run(
    get: GetNode,
    entry: str,
    args: list[Any],
    *,
    fuel: int = 10_000_000,
    field_names: FieldNames | None = None,
    host: Any = None,
) -> Any:
    """Compile and run an IR function with typed JSON boundary values."""
    ns_type = None if host is None else host.ns_type
    compiler = _WatCompiler(get, ns_type)
    entry_node = compiler.load(entry)
    if entry_node["k"] != "fn":
        raise GraphError(entry, "E_TYPE_MISMATCH", "entry must reference a fn node")
    params = entry_node["params"]
    if len(args) != len(params):
        raise GraphError(entry, "E_ARITY", f"expected {len(params)} arguments, got {len(args)}")
    if not isinstance(fuel, int) or isinstance(fuel, bool) or fuel < 0:
        raise GraphError(entry, "E_FUEL", "fuel must be a non-negative integer")

    trap_global = None
    store = None
    host_error: list[GraphError] = []
    try:
        wat = compiler.compile(entry)
        effects = {"kv_get", "kv_put", "kv_del", "kv_list", "kv_keys", "now", "print"}
        if host is None and any(node["k"] in effects for node in compiler.nodes.values()):
            raise GraphError(entry, "E_NO_HOST", "effectful execution requires a host")
        config = wasmtime.Config()
        config.consume_fuel = True
        engine = wasmtime.Engine(config)
        module = wasmtime.Module(engine, wasmtime.wat2wasm(wat))
        store = wasmtime.Store(engine)
        store.set_fuel(fuel)
        linker = wasmtime.Linker(engine)
        linker.define_func(
            "host",
            "normalize_concat",
            wasmtime.FuncType(
                [wasmtime.ValType.i64(), wasmtime.ValType.i64()], [wasmtime.ValType.i64()]
            ),
            _normalize_concat,
            access_caller=True,
        )

        def host_call(callback):
            def wrapped(caller, *values):
                try:
                    return callback(caller, *values)
                except GraphError as error:
                    host_error.append(error)
                    raise wasmtime.Trap(error.info["code"]) from None

            return wrapped

        def strings(caller, ns_pointer, value_pointer):
            memory, _ = _memory_objects(caller)
            return (
                _read_string(memory, caller, ns_pointer),
                _read_string(memory, caller, value_pointer),
            )

        def serialize_host(caller, type_, value, role):
            memory, heap = _memory_objects(caller)
            return _serialize(
                type_, value, memory, heap, caller, entry, role, compiler, field_names
            )

        def kv_get(caller, ns_pointer, key_pointer):
            ns, key = strings(caller, ns_pointer, key_pointer)
            return serialize_host(
                caller, {"option": host.ns_type(ns)}, host.kv_get(ns, key), "kv_get"
            )

        def kv_put(caller, ns_pointer, key_pointer, value_pointer):
            ns, key = strings(caller, ns_pointer, key_pointer)
            memory, _ = _memory_objects(caller)
            value = _deserialize(
                host.ns_type(ns), value_pointer, memory, caller, compiler, field_names
            )
            host.kv_put(ns, key, value)
            return 0

        def kv_del(caller, ns_pointer, key_pointer):
            ns, key = strings(caller, ns_pointer, key_pointer)
            host.kv_del(ns, key)
            return 0

        def kv_list(caller, ns_pointer, prefix_pointer):
            ns, prefix = strings(caller, ns_pointer, prefix_pointer)
            return serialize_host(
                caller, {"list": host.ns_type(ns)}, host.kv_list(ns, prefix), "kv_list"
            )

        def kv_keys(caller, ns_pointer, prefix_pointer):
            ns, prefix = strings(caller, ns_pointer, prefix_pointer)
            return serialize_host(
                caller, {"list": "string"}, host.kv_keys(ns, prefix), "kv_keys"
            )

        clock_value: list[int] = []

        def now(_caller):
            if not clock_value:
                value = host.clock()
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not I64_MIN <= value <= I64_MAX
                ):
                    raise _type_error(entry, "clock", "i64")
                clock_value.append(value)
            return clock_value[0]

        def host_print(caller, pointer):
            memory, _ = _memory_objects(caller)
            host.print(_read_string(memory, caller, pointer))
            return 0

        i64 = wasmtime.ValType.i64()
        imports = [
            ("kv_get", [i64, i64], kv_get),
            ("kv_put", [i64, i64, i64], kv_put),
            ("kv_del", [i64, i64], kv_del),
            ("kv_list", [i64, i64], kv_list),
            ("kv_keys", [i64, i64], kv_keys),
            ("now", [], now),
            ("print", [i64], host_print),
        ]
        for name, params_, callback in imports:
            linker.define_func(
                "host",
                name,
                wasmtime.FuncType(params_, [i64]),
                host_call(callback),
                access_caller=True,
            )
        instance = linker.instantiate(store, module)
        exports = instance.exports(store)
        trap_global = exports["__trap_site"]
        memory = exports["memory"]
        heap = exports["__heap"]
        encoded_args = [
            _serialize(
                type_,
                argument,
                memory,
                heap,
                store,
                entry,
                f"argument {index}",
                compiler,
                field_names,
            )
            for index, (type_, argument) in enumerate(zip(params, args, strict=True))
        ]
        result = int(exports["main"](store, *encoded_args))
        return _deserialize(entry_node["ret"], result, memory, store, compiler, field_names)
    except GraphError:
        raise
    except wasmtime.Trap as error:
        if host_error:
            raise host_error[0] from None
        if error.trap_code == wasmtime.TrapCode.UNREACHABLE and trap_global is not None:
            site = int(trap_global.value(store))
            trap = compiler.trap_sites.get(site)
            if trap is not None:
                node_hash, code, detail = trap
                raise GraphError(node_hash, code, detail) from None
        code = "E_FUEL" if error.trap_code == wasmtime.TrapCode.OUT_OF_FUEL else "E_TRAP"
        raise GraphError(entry, code, error.message) from None
    except wasmtime.WasmtimeError as error:
        message = str(error)
        code = "E_FUEL" if "fuel" in message.lower() else "E_TRAP"
        raise GraphError(entry, code, message) from None
    except (IndexError, OverflowError, TypeError, UnicodeError, ValueError) as error:
        raise GraphError(entry, "E_TRAP", str(error)) from None
