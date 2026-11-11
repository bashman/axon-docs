"""Transactional typed key/value overlay used by one AXON invocation."""

from __future__ import annotations

from .ir import MAX_STRING_BYTES, GraphError, encode_type
from .typecheck import render_type
from .values import MAX_LIST_LENGTH, decode_value, encode_value

_DELETED = object()


class KvTx:
    """Buffer KV changes and expose read-your-own-writes semantics."""

    def __init__(self, store, get, migrate=None):
        self.store = store
        self.get_node = get
        self.migrate = migrate
        self._pending: dict[tuple[str, str], bytes | object] = {}

    def _namespace_type(self, ns: str):
        namespaces = self.store.kv_namespaces()
        if ns not in namespaces:
            raise GraphError(ns, "E_UNKNOWN_NAMESPACE", f"unknown KV namespace {ns!r}")
        return namespaces[ns]

    @staticmethod
    def _key(value: str, role: str) -> None:
        if not isinstance(value, str):
            raise GraphError(None, "E_TYPE_MISMATCH", f"{role} must be a string")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise GraphError(None, "E_TYPE_MISMATCH", f"{role} must be valid UTF-8") from None
        if size > MAX_STRING_BYTES:
            raise GraphError(None, "E_STR_LIMIT", f"{role} exceeds 1 MiB")

    def _decode(self, ns: str, type_, row):
        stored_type, data = row
        if stored_type == encode_type(type_):
            return decode_value(self.get_node, type_, data)

        history = self.store.kv_type_history(ns)
        version = next(
            (
                index
                for index in range(len(history) - 1, -1, -1)
                if encode_type(history[index]["type"]) == stored_type
            ),
            None,
        )
        if version is None:
            raise GraphError(ns, "E_KV_TYPE", f"stored type does not match namespace {ns!r}")

        value = decode_value(self.get_node, history[version]["type"], data)
        for previous, following in zip(
            history[version:], history[version + 1:], strict=False
        ):
            fn_name = following.get("migration_fn")
            if not fn_name or self.migrate is None:
                self._migration_required(ns, fn_name, previous["type"], following["type"])
            try:
                value = self.migrate(fn_name, value)
                encode_value(self.get_node, following["type"], value)
            except GraphError:
                self._migration_required(ns, fn_name, previous["type"], following["type"])
        return value

    def _migration_required(self, ns: str, fn_name, old_type, new_type):
        old = render_type(old_type, self.store)
        new = render_type(new_type, self.store)
        if fn_name:
            detail = f"define fn {fn_name}: fn({old}) -> {new}"
        else:
            detail = f"define a migration: fn({old}) -> {new}"
        raise GraphError(ns, "E_SCHEMA_MIGRATION_REQUIRED", detail)

    def get(self, ns: str, key: str):
        type_ = self._namespace_type(ns)
        self._key(key, "key")
        pending = self._pending.get((ns, key), None)
        if pending is _DELETED:
            return {"tag": "none"}
        if isinstance(pending, bytes):
            return {"tag": "some", "v": decode_value(self.get_node, type_, pending)}
        row = self.store.kv_get_raw(ns, key)
        if row is None:
            return {"tag": "none"}
        return {"tag": "some", "v": self._decode(ns, type_, row)}

    def put(self, ns: str, key: str, value_json) -> None:
        type_ = self._namespace_type(ns)
        self._key(key, "key")
        self._pending[(ns, key)] = encode_value(self.get_node, type_, value_json)

    def delete(self, ns: str, key: str) -> None:
        self._namespace_type(ns)
        self._key(key, "key")
        self._pending[(ns, key)] = _DELETED

    def _scan(self, ns: str, prefix: str):
        type_ = self._namespace_type(ns)
        self._key(prefix, "prefix")
        rows = {
            key: (stored_type, data)
            for key, stored_type, data in self.store.kv_scan_raw(ns, prefix)
        }
        for (pending_ns, key), value in self._pending.items():
            if pending_ns != ns or not key.startswith(prefix):
                continue
            if value is _DELETED:
                rows.pop(key, None)
            else:
                rows[key] = (encode_type(type_), value)
        ordered = sorted(rows.items(), key=lambda item: item[0].encode("utf-8"))
        if len(ordered) > MAX_LIST_LENGTH:
            raise GraphError(ns, "E_LIST_LIMIT", "KV scan exceeds 65536 results")
        return type_, ordered

    def list(self, ns: str, prefix: str):
        type_, rows = self._scan(ns, prefix)
        return [self._decode(ns, type_, row) for _, row in rows]

    def keys(self, ns: str, prefix: str):
        _, rows = self._scan(ns, prefix)
        return [key for key, _ in rows]

    def commit(self) -> None:
        if not self._pending:
            return  # nothing to write: keep history free of empty commits
        namespaces = self.store.kv_namespaces()
        ops = []
        pending = sorted(
            self._pending.items(), key=lambda item: (item[0][0], item[0][1].encode("utf-8"))
        )
        for (ns, key), value in pending:
            if value is _DELETED:
                ops.append(("del", ns, key))
            else:
                ops.append(("put", ns, key, encode_type(namespaces[ns]), value))
        self.store.kv_commit(ops)
        self._pending.clear()

    def rollback(self) -> None:
        self._pending.clear()
