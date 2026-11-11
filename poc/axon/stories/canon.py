"""Canonical form of an HTTP spec (docs/requisitos-historias.md v3, A1.3).

Two specs are "equivalent" when their canonical forms are equal:

- `name` is dropped (the compiler may use any project name);
- `types` are kept exactly: tag, field names, field types and order — that is what
  the store hashes, so a rename or a reorder is a different spec, not an equivalent one.
  The hash is computed by registering the spec in a throwaway in-memory store, so the
  canonical type id is the store's own `decl_hash` and not a second encoding;
- `routes` keep `method`, `path`, `auth` and `rate`; `handler` is replaced by its
  type shape (`fn(request)->response`), so handler names are free but a wrong shape
  still differs; routes are compared as a set (their declaration order is not semantic);
- `atoms` and `effects` are compared as sets (their order is not semantic); every
  other key is compared as-is.
"""

from __future__ import annotations

import json

from ..service import Service
from ..store import Store


def _type_hashes(spec: dict) -> dict[str, str | None]:
    """`{tag: decl_hash}` as the store computes it, from a throwaway in-memory store."""
    try:
        service = Service(Store())
        registered = service.register_spec(spec)
    except Exception:  # a malformed spec has no canonical types; validate_spec reports it
        return {}
    if not registered.get("ok"):
        return {}
    store = service.store
    return {entry["tag"]: store.type_pointer(entry["tag"]) for entry in spec.get("types", [])}


def canon(spec: dict) -> dict:
    """Canonical, JSON-ready form of `spec` for the A1.3 equivalence rule."""
    hashes = _type_hashes(spec)
    types = [
        {
            "tag": entry["tag"],
            "fields": [
                {"name": field["name"], "type": field["type"]} for field in entry["fields"]
            ],
            "decl": hashes.get(entry["tag"]),
        }
        for entry in spec.get("types", [])
    ]
    routes = [
        {
            "method": route["method"],
            "path": route["path"],
            "auth": route["auth"],
            "rate": route["rate"],
            "handler": "fn(request)->response",
        }
        for route in spec.get("routes", [])
    ]
    # A1.3 keeps route *names* out of the comparison, so the same API reached by routes
    # declared in another order is equivalent: routes are compared as a set.
    routes.sort(key=lambda route: (route["method"], route["path"]))
    rest = {}
    for key, value in spec.items():
        if key in ("name", "types", "routes"):
            continue
        if key in ("atoms", "effects"):
            if isinstance(value, dict):
                rest[key] = {k: sorted(v) for k, v in sorted(value.items())}
            else:
                rest[key] = sorted(value)
        else:
            rest[key] = value
    return {"types": types, "routes": routes, "rest": rest}


def canon_json(spec: dict) -> str:
    """Stable text of `canon(spec)`, handy for diffs."""
    return json.dumps(canon(spec), sort_keys=True, ensure_ascii=False, indent=2)


__all__ = ["canon", "canon_json"]
