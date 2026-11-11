"""In-memory KV backend used to run specs on a copy of the state (the real KV is never touched)."""

from __future__ import annotations

from .ir import encode_type
from .values import decode_value, encode_value


class MemoryKv:
    """Implements the raw KV interface of the store over a dict; namespaces come from `store`."""

    def __init__(self, store):
        self._store = store
        self._rows: dict[tuple[str, str], tuple[bytes, bytes]] = {}

    def kv_namespaces(self):
        return self._store.kv_namespaces()

    def kv_get_raw(self, ns, key):
        return self._rows.get((ns, key))

    def kv_scan_raw(self, ns, prefix):
        keys = [k for (n, k) in self._rows if n == ns and k.startswith(prefix)]
        keys.sort(key=lambda k: k.encode("utf-8"))
        return [(k, *self._rows[(ns, k)]) for k in keys]

    def kv_commit(self, ops):
        for op in ops:
            if op[0] == "put":
                _, ns, key, type_bytes, value = op
                self._rows[(ns, key)] = (type_bytes, value)
            else:
                self._rows.pop((op[1], op[2]), None)

    def load(self, state: dict, get) -> None:
        """Replace the contents with a JSON state {ns: {key: value}}."""
        self._rows.clear()
        namespaces = self._store.kv_namespaces()
        for ns, entries in state.items():
            for key, value in entries.items():
                self._rows[(ns, key)] = (
                    encode_type(namespaces[ns]),
                    encode_value(get, namespaces[ns], value),
                )

    def snapshot(self, get) -> dict:
        """The contents as a JSON state; empty namespaces are omitted."""
        namespaces = self._store.kv_namespaces()
        out: dict = {}
        for (ns, key), (_, value) in sorted(self._rows.items()):
            out.setdefault(ns, {})[key] = decode_value(get, namespaces[ns], value)
        return out
