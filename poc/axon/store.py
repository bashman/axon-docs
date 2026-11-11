"""SQLite-backed content-addressed store for IR nodes.

Nodes are immutable: an id always maps to the same canonical payload. Names and
roots are mutable pointers, so both require compare-and-swap (`apply_cas`).
`put` verifies the caller's id before storing anything.
"""

import json
import re
import sqlite3
import time

from . import ir, typecheck
from .ir import GraphError
from .spec import is_effect, normalize_providers, normalize_tables, validate_spec

# A short reference is matched against the digest (the id without its `b3:` prefix).
_PREFIX_RE = re.compile(r"^[0-9a-f]{8,64}$")
MAX_CANDIDATES = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS names (
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (namespace, name)
);
CREATE INDEX IF NOT EXISTS names_hash ON names(hash);
CREATE TABLE IF NOT EXISTS roots (
    name TEXT PRIMARY KEY,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS root_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    old_hash TEXT,
    new_hash TEXT,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    feedback TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atoms (
    name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS specs (
    name TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS types (
    tag TEXT PRIMARY KEY,
    decl_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS field_names (
    decl_hash TEXT NOT NULL,
    idx INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (decl_hash, idx)
);
CREATE TABLE IF NOT EXISTS edges (
    parent TEXT NOT NULL,
    child TEXT NOT NULL,
    PRIMARY KEY (parent, child)
);
CREATE INDEX IF NOT EXISTS edges_child ON edges(child);
CREATE TABLE IF NOT EXISTS kv_namespaces (
    ns TEXT PRIMARY KEY,
    type_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    ns TEXT NOT NULL,
    key TEXT NOT NULL,
    type_bytes BLOB NOT NULL,
    value BLOB NOT NULL,
    PRIMARY KEY (ns, key)
);
CREATE TABLE IF NOT EXISTS effect_bounds (
    name TEXT PRIMARY KEY,
    effects_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv_type_history (
    ns TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type_json TEXT NOT NULL,
    migration_fn TEXT,
    PRIMARY KEY (ns, seq)
);
CREATE TABLE IF NOT EXISTS sql_tables (
    name TEXT PRIMARY KEY,
    schema_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_grants (
    name TEXT PRIMARY KEY,
    operations_json TEXT NOT NULL
);
"""


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalize_kv_op(op):
    """Validate one KV op: ("put", ns, key, type_bytes, value) or ("del", ns, key)."""
    if not isinstance(op, (tuple, list)) or not op:
        raise GraphError(op, "E_BAD_NODE", "each KV op must be a tuple")
    kind = op[0]
    if kind == "put":
        if len(op) != 5:
            raise GraphError(op, "E_BAD_NODE", "a 'put' op is (put, ns, key, type_bytes, value)")
        _, ns, key, type_bytes, value = op
        _check_kv_key(ns, key)
        if not isinstance(type_bytes, (bytes, bytearray)):
            raise GraphError(op, "E_BAD_NODE", "put.type_bytes must be bytes")
        if not isinstance(value, (bytes, bytearray)):
            raise GraphError(op, "E_BAD_NODE", "put.value must be bytes")
        return ("put", ns, key, bytes(type_bytes), bytes(value))
    if kind == "del":
        if len(op) != 3:
            raise GraphError(op, "E_BAD_NODE", "a 'del' op is (del, ns, key)")
        _, ns, key = op
        _check_kv_key(ns, key)
        return ("del", ns, key)
    raise GraphError(op, "E_BAD_NODE", "KV op kind must be 'put' or 'del'")


def _check_kv_key(ns, key):
    if not isinstance(ns, str) or ir.ATOM_RE.match(ns) is None:
        raise GraphError(ns, "E_BAD_NODE", f"namespace must match {ir.ATOM_RE.pattern}")
    if not isinstance(key, str) or not key:
        raise GraphError(key, "E_BAD_NODE", "KV key must be a non-empty string")


class Store:
    """Graph store on top of SQLite: ':memory:' or a file path."""

    def __init__(self, path=":memory:"):
        self.path = path
        # Autocommit mode: statements are durable immediately and `apply_cas` can
        # own an explicit BEGIN/COMMIT/ROLLBACK transaction.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.executescript(_SCHEMA)

    # -- nodes ---------------------------------------------------------------

    def put(self, h, node, type_str):
        """Store a node under its id. Idempotent; raises ValueError on id mismatch."""
        if not isinstance(type_str, str) or not type_str:
            raise ValueError("type must be a non-empty string")
        if not ir.is_hash(h):
            raise ValueError("hash must be a b3:<64 hex> id")
        actual = ir.node_hash(node)
        if h != actual:
            raise ValueError(f"hash mismatch: given {h!r}, computed {actual!r}")
        ir.check_shape(node, h, inline=False)
        self._conn.execute(
            "INSERT OR IGNORE INTO nodes(hash, kind, payload, type) VALUES(?, ?, ?, ?)",
            (h, node["k"], ir.canonical(node).decode(), type_str),
        )
        for child in ir.children(node):
            self._conn.execute(
                "INSERT OR IGNORE INTO edges(parent, child) VALUES(?, ?)", (h, child)
            )

    def get(self, h):
        row = self._conn.execute("SELECT payload FROM nodes WHERE hash = ?", (h,)).fetchone()
        return None if row is None else json.loads(row[0])

    def type_of(self, h):
        row = self._conn.execute("SELECT type FROM nodes WHERE hash = ?", (h,)).fetchone()
        return None if row is None else row[0]

    # -- references ----------------------------------------------------------

    def resolve(self, ref):
        """Name (main namespace) | full id | unique digest prefix (>= 8 hex) -> id."""
        if not isinstance(ref, str):
            raise GraphError(ref, "E_UNKNOWN_REF", "reference must be a string")
        named = self._name_hash(ref, "main")
        if named is not None:
            return named
        if ir.HASH_RE.match(ref):
            if self.get(ref) is not None:
                return ref
            raise GraphError(ref, "E_UNKNOWN_REF", f"no node with id {ref}")
        prefix = ref.lower()
        if not _PREFIX_RE.match(prefix):
            if ir.ATOM_RE.match(ref):
                raise GraphError(ref, "E_UNKNOWN_REF", f"no root or name {ref!r}")
            raise GraphError(
                ref,
                "E_UNKNOWN_REF",
                "reference must be a name, a b3:<64 hex> id or a hex prefix of at least 8 chars",
            )
        rows = [
            row[0]
            for row in self._conn.execute(
                "SELECT hash FROM nodes WHERE hash LIKE ? ORDER BY hash", ("b3:" + prefix + "%",)
            )
        ]
        if len(rows) == 1:
            return rows[0]
        if not rows:
            raise GraphError(ref, "E_UNKNOWN_REF", f"no node matches prefix {ref}")
        listed = ", ".join(rows[:MAX_CANDIDATES])
        if len(rows) > MAX_CANDIDATES:
            listed += ", ..."
        raise GraphError(
            ref, "E_AMBIGUOUS_PREFIX", f"prefix {ref} matches {len(rows)} nodes: {listed}"
        )

    # -- names and roots -----------------------------------------------------

    def set_name(self, h, name, namespace="main"):
        """Point (namespace, name) at h. Idempotent; E_STALE_ROOT if it points elsewhere."""
        self._check_pointer_name(name, namespace)
        if self.get(h) is None:
            raise GraphError(h, "E_UNKNOWN_REF", f"no node with id {h}")
        current = self._name_hash(name, namespace)
        if current is None:
            self._conn.execute(
                "INSERT INTO names(namespace, name, hash) VALUES(?, ?, ?)", (namespace, name, h)
            )
        elif current != h:
            raise GraphError(
                name, "E_STALE_ROOT", f"name '{name}' points to {current}; use apply_cas"
            )

    def name_of(self, h):
        """First name for h in alphabetical order, or None."""
        row = self._conn.execute(
            "SELECT name FROM names WHERE hash = ? ORDER BY name LIMIT 1", (h,)
        ).fetchone()
        return None if row is None else row[0]

    def names_of(self, h):
        """All names for h, in alphabetical order."""
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT name FROM names WHERE hash = ? ORDER BY name", (h,)
            )
        ]

    def _name_hash(self, name, namespace="main"):
        row = self._conn.execute(
            "SELECT hash FROM names WHERE namespace = ? AND name = ?", (namespace, name)
        ).fetchone()
        return None if row is None else row[0]

    def set_root(self, name, h):
        """Point root `name` at h. Idempotent; E_STALE_ROOT if it points elsewhere."""
        self._check_pointer_name(name)
        if self.get(h) is None:
            raise GraphError(h, "E_UNKNOWN_REF", f"no node with id {h}")
        row = self._conn.execute("SELECT hash FROM roots WHERE name = ?", (name,)).fetchone()
        current = None if row is None else row[0]
        if current is None:
            self._conn.execute("INSERT INTO roots(name, hash) VALUES(?, ?)", (name, h))
            self._record_root(name, None, h)
        elif current != h:
            raise GraphError(
                name, "E_STALE_ROOT", f"root '{name}' points to {current}; use apply_cas"
            )

    def roots(self):
        return dict(self._conn.execute("SELECT name, hash FROM roots ORDER BY name"))

    def pointers_of(self, h):
        """Names, roots and type tags that point at `h`, as apply_cas pointers.

        Names carry their namespace and come first (sorted by namespace then name),
        then roots and type tags (sorted by name). A root/type pointer has no
        namespace: `apply_cas` treats it as the `main` namespace, which is how
        `define` binds roots.
        """
        out = [
            {"kind": "name", "name": name, "namespace": namespace}
            for namespace, name in self._conn.execute(
                "SELECT namespace, name FROM names WHERE hash = ? ORDER BY namespace, name", (h,)
            )
        ]
        out.extend(
            {"kind": "root", "name": name}
            for (name,) in self._conn.execute(
                "SELECT name FROM roots WHERE hash = ? ORDER BY name", (h,)
            )
        )
        out.extend(
            {"kind": "type", "name": tag}
            for (tag,) in self._conn.execute(
                "SELECT tag FROM types WHERE decl_hash = ? ORDER BY tag", (h,)
            )
        )
        return out

    # -- nominal type registry ----------------------------------------------

    def type_pointer(self, tag):
        """The declaration id a type tag currently resolves to, or None."""
        row = self._conn.execute("SELECT decl_hash FROM types WHERE tag = ?", (tag,)).fetchone()
        return None if row is None else row[0]

    def declarations(self):
        """Every stored record declaration as ``{tag: [decl ids]}`` (current and older ones)."""
        out: dict[str, list[str]] = {}
        for h, payload in self._conn.execute(
            "SELECT hash, payload FROM nodes WHERE kind = 'rec' ORDER BY hash"
        ):
            out.setdefault(json.loads(payload).get("tag"), []).append(h)
        return out

    def types(self):
        """Registered type tags, sorted, as a {tag: decl_hash} mapping."""
        return dict(self._conn.execute("SELECT tag, decl_hash FROM types ORDER BY tag"))

    def set_type(self, tag, h):
        """Point type tag `tag` at declaration `h`. Idempotent; E_TYPE_TAG_TAKEN on conflict.

        A tag names one declaration: re-pointing it at another is an explicit,
        CAS-protected change (see `apply_cas` with `pointer.kind = "type"`), never a
        silent overwrite.
        """
        if not isinstance(tag, str) or ir.ATOM_RE.match(tag) is None:
            raise GraphError(tag, "E_BAD_NODE", f"type tag must match {ir.ATOM_RE.pattern}")
        if self.get(h) is None:
            raise GraphError(h, "E_UNKNOWN_REF", f"no node with id {h}")
        current = self.type_pointer(tag)
        if current is None:
            self._conn.execute("INSERT INTO types(tag, decl_hash) VALUES(?, ?)", (tag, h))
            self._record_root(tag, None, h)
        elif current != h:
            raise GraphError(
                tag, "E_TYPE_TAG_TAKEN", f"type tag '{tag}' is already declared as {current}"
            )

    def put_field_names(self, decl, names):
        """Attach field names (metadata, outside the hash) to a `rec` declaration.

        `names` must have one entry per declared field; the declaration is looked up
        and its arity checked. Re-putting replaces the names for that declaration.
        """
        node = self.get(decl)
        if node is None:
            raise GraphError(decl, "E_UNKNOWN_REF", f"no node with id {decl}")
        if node["k"] != "rec":
            raise GraphError(
                decl, "E_TYPE_MISMATCH", f"field names need a rec declaration, got {node['k']}"
            )
        names = list(names)
        fields = node["fields"]
        if len(names) != len(fields):
            raise GraphError(
                decl, "E_ARITY", f"declaration has {len(fields)} fields, got {len(names)} names"
            )
        seen = set()
        for name in names:
            if not isinstance(name, str) or not name:
                raise GraphError(decl, "E_BAD_FIELD", f"field name must be non-empty, got {name!r}")
            if name in seen:
                raise GraphError(decl, "E_BAD_FIELD", f"duplicate field name {name!r}")
            seen.add(name)
        self._conn.execute("DELETE FROM field_names WHERE decl_hash = ?", (decl,))
        for i, name in enumerate(names):
            self._conn.execute(
                "INSERT INTO field_names(decl_hash, idx, name) VALUES(?, ?, ?)", (decl, i, name)
            )
        self.log("put_field_names", {"decl": decl, "names": names}, {"ok": True})

    def field_names(self, decl):
        """Field names of a declaration in order, or None when it has none."""
        rows = self._conn.execute(
            "SELECT name FROM field_names WHERE decl_hash = ? ORDER BY idx", (decl,)
        ).fetchall()
        return [row[0] for row in rows] or None

    def field_name(self, decl, i):
        """Name of field `i` of a declaration, or None (the wasm boundary callback)."""
        row = self._conn.execute(
            "SELECT name FROM field_names WHERE decl_hash = ? AND idx = ?", (decl, int(i))
        ).fetchone()
        return None if row is None else row[0]

    def root_history(self, name, limit=50):
        """Root changes for `name`, most recent first."""
        rows = self._conn.execute(
            "SELECT id, name, old_hash, new_hash, ts FROM root_history "
            "WHERE name = ? ORDER BY id DESC LIMIT ?",
            (name, int(limit)),
        )
        return [
            {
                "id": row[0],
                "name": row[1],
                "old_hash": row[2],
                "new_hash": row[3],
                "ts": row[4],
            }
            for row in rows
        ]

    def _record_root(self, name, old_hash, new_hash):
        self._conn.execute(
            "INSERT INTO root_history(name, old_hash, new_hash, ts) VALUES(?, ?, ?, ?)",
            (name, old_hash, new_hash, time.time()),
        )

    # -- compare-and-swap ----------------------------------------------------

    def apply_cas(self, changes):
        """Apply [{pointer, expected, new}] atomically, or reject the whole batch.

        `expected`/`new` are ids or None (None = "does not exist" for expected,
        "delete the pointer" for new). Any mismatch raises E_STALE_ROOT and rolls
        back every change; a `new` that is not in `nodes` raises E_UNKNOWN_REF.
        """
        batch = [self._normalize_change(c) for c in changes]
        for change in batch:
            new = change["new"]
            if new is not None and self.get(new) is None:
                raise GraphError(new, "E_UNKNOWN_REF", f"node {new} is not in the store")
        self._conn.execute("BEGIN")
        try:
            for change in batch:
                pointer = change["pointer"]
                current = self._pointer_get(pointer)
                if current != change["expected"]:
                    desc = {
                        "root": f"root '{pointer['name']}'",
                        "name": f"name '{pointer['name']}'",
                        "type": f"type tag '{pointer['name']}'",
                    }[pointer["kind"]]
                    if current is None:
                        detail = f"{desc} does not exist; use apply_cas"
                    else:
                        detail = f"{desc} points to {current}; use apply_cas"
                    raise GraphError(pointer["name"], "E_STALE_ROOT", detail)
            for change in batch:
                self._pointer_apply(change["pointer"], change["new"])
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return None

    def _normalize_change(self, change):
        if not isinstance(change, dict):
            raise GraphError("apply_cas", "E_BAD_NODE", "change must be an object")
        pointer = self._normalize_pointer(change.get("pointer"))
        expected = change.get("expected")
        new = change.get("new")
        for label, value in (("expected", expected), ("new", new)):
            if value is not None and not ir.is_hash(value):
                raise GraphError(
                    "apply_cas", "E_BAD_NODE", f"{label} must be None or a b3:<64 hex> id"
                )
        return {"pointer": pointer, "expected": expected, "new": new}

    def _normalize_pointer(self, pointer):
        if not isinstance(pointer, dict):
            raise GraphError("apply_cas", "E_BAD_NODE", "pointer must be an object")
        kind = pointer.get("kind")
        name = pointer.get("name")
        namespace = pointer.get("namespace", "main")
        self._check_pointer_name(name, namespace)
        if kind not in ("root", "name", "type"):
            raise GraphError(
                "apply_cas", "E_BAD_NODE", "pointer.kind must be 'root', 'name' or 'type'"
            )
        return {"kind": kind, "name": name, "namespace": namespace}

    def _check_pointer_name(self, name, namespace="main"):
        if not isinstance(name, str) or not name:
            raise GraphError("apply_cas", "E_BAD_NODE", "pointer name must be a non-empty string")
        if not isinstance(namespace, str) or not namespace:
            raise GraphError("apply_cas", "E_BAD_NODE", "namespace must be a non-empty string")

    def _pointer_get(self, pointer):
        if pointer["kind"] == "root":
            row = self._conn.execute(
                "SELECT hash FROM roots WHERE name = ?", (pointer["name"],)
            ).fetchone()
        elif pointer["kind"] == "type":
            row = self._conn.execute(
                "SELECT decl_hash FROM types WHERE tag = ?", (pointer["name"],)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT hash FROM names WHERE namespace = ? AND name = ?",
                (pointer["namespace"], pointer["name"]),
            ).fetchone()
        return None if row is None else row[0]

    def _pointer_apply(self, pointer, new):
        if pointer["kind"] == "root":
            old = self._pointer_get(pointer)
            if new is None:
                self._conn.execute("DELETE FROM roots WHERE name = ?", (pointer["name"],))
            else:
                self._conn.execute(
                    "INSERT INTO roots(name, hash) VALUES(?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET hash = excluded.hash",
                    (pointer["name"], new),
                )
            if old != new:
                self._record_root(pointer["name"], old, new)
        elif pointer["kind"] == "type":
            old = self._pointer_get(pointer)
            if new is None:
                self._conn.execute("DELETE FROM types WHERE tag = ?", (pointer["name"],))
            else:
                self._conn.execute(
                    "INSERT INTO types(tag, decl_hash) VALUES(?, ?) "
                    "ON CONFLICT(tag) DO UPDATE SET decl_hash = excluded.decl_hash",
                    (pointer["name"], new),
                )
            if old != new:
                self._record_root(pointer["name"], old, new)
        elif new is None:
            self._conn.execute(
                "DELETE FROM names WHERE namespace = ? AND name = ?",
                (pointer["namespace"], pointer["name"]),
            )
        else:
            self._conn.execute(
                "INSERT INTO names(namespace, name, hash) VALUES(?, ?, ?) "
                "ON CONFLICT(namespace, name) DO UPDATE SET hash = excluded.hash",
                (pointer["namespace"], pointer["name"], new),
            )

    # -- graph queries -------------------------------------------------------

    def dependents(self, h):
        """Nodes that reference h directly (parents), sorted."""
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT parent FROM edges WHERE child = ? ORDER BY parent", (h,)
            )
        ]

    def subgraph(self, h, depth):
        """BFS from h following children; depth=0 returns only h."""
        depth = int(depth)
        if depth < 0:
            raise ValueError("depth must be >= 0")
        if self.get(h) is None:
            raise GraphError(h, "E_UNKNOWN_REF", f"no node with id {h}")
        out = []
        seen = set()
        frontier = [h]
        for level in range(depth + 1):
            nxt = []
            for cur in frontier:
                if cur in seen:
                    continue
                seen.add(cur)
                nd = self.get(cur)
                if nd is None:
                    continue
                out.append((cur, nd))
                if level < depth:
                    nxt.extend(ir.children(nd))
            frontier = nxt
        return out

    # -- atoms -------------------------------------------------------------------

    def register_atoms(self, names):
        """Seed atom names from the spec (idempotent). Returns the names newly added.

        Each name must match `ir.ATOM_RE`; the registry is seeded by the spec, not
        by the agent. Every call is recorded in `history`.
        """
        names = list(names)
        for name in names:
            if not isinstance(name, str) or ir.ATOM_RE.match(name) is None:
                raise GraphError(name, "E_BAD_NODE", f"atom name must match {ir.ATOM_RE.pattern}")
        added = []
        for name in names:
            cur = self._conn.execute("INSERT OR IGNORE INTO atoms(name) VALUES(?)", (name,))
            if cur.rowcount:
                added.append(name)
        self.log("register_atoms", {"names": names}, {"ok": True, "added": added})
        return added

    def atoms(self):
        """Registered atom names, sorted."""
        return [row[0] for row in self._conn.execute("SELECT name FROM atoms ORDER BY name")]

    # -- specs -------------------------------------------------------------------

    def put_spec(self, spec):
        """Register a spec (immutable). Returns its name; a different body is E_SPEC_EXISTS.

        The body is validated with `spec.validate_spec` and stored canonically, so
        re-putting the same spec is idempotent. The spec's types, KV namespaces
        (evolving them with migrations, ADR-0009) and effect caps are seeded, its
        atoms are registered and the call is recorded in `history`.

        A new spec may **evolve a domain type** (ADR-0006/0009): re-declaring a
        seeded tag with another structure moves the tag pointer (the old
        declaration stays in the store). The move, the KV namespaces it changes,
        the effect caps and the atoms are all-or-nothing: a namespace that holds
        rows and changes type without a declared migration aborts the whole spec
        with E_SCHEMA_MIGRATION_REQUIRED and leaves nothing behind (a single
        transaction, written before the body so retrying is possible).
        """
        validate_spec(spec)
        name = spec["name"]
        body = _dumps(spec)
        row = self._conn.execute("SELECT body FROM specs WHERE name = ?", (name,)).fetchone()
        if row is not None and row[0] != body:
            raise GraphError(
                name, "E_SPEC_EXISTS", f"spec '{name}' already exists with a different body"
            )
        self._conn.execute("BEGIN")
        try:
            tag_hash = self._seed_types(spec.get("types") or [])
            self._seed_tables(spec.get("tables") or [], spec.get("types") or [], tag_hash)
            self.put_provider_grants(spec.get("providers") or {})
            migrations = {m["ns"]: m["fn"] for m in spec.get("migrations") or []}
            self._seed_namespaces(spec.get("kv") or [], tag_hash, migrations)
            self.put_effect_bounds(spec.get("effects") or {})
            if row is None:
                self._conn.execute(
                    "INSERT INTO specs(name, body, created_at) VALUES(?, ?, ?)",
                    (name, body, time.time()),
                )
            self.register_atoms(spec["atoms"])
            self.log("put_spec", {"name": name, "atoms": spec["atoms"]}, {"ok": True})
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return name

    def _seed_types(self, types_spec):
        """Create the spec's `rec` declarations, seed `types` and `field_names`.

        Declarations are processed in order, so a `{"rec": tag}` field can only name
        an earlier declaration (validated by `spec.validate_spec`). Each node is
        stored with type "decl" and then re-checked by `typecheck`, which now sees
        its tag as current. Returns the {tag: declaration id} mapping.
        """
        tag_hash = {}
        if not types_spec:
            return tag_hash
        for entry in types_spec:
            tag = entry["tag"]
            fields = [self._resolve_type(field["type"], tag_hash) for field in entry["fields"]]
            node = {"k": "rec", "tag": tag, "fields": fields}
            h = ir.node_hash(node)
            if self.get(h) is None:
                self.put(h, node, "decl")
            self._seed_type_pointer(tag, h)
            self.put_field_names(h, [field["name"] for field in entry["fields"]])
            typecheck.check(self, node)
            tag_hash[tag] = h
        self.log("put_types", {"tags": [e["tag"] for e in types_spec]}, {"ok": True})
        return tag_hash

    def _seed_type_pointer(self, tag, h):
        """Point a spec's type tag at `h`: create it, keep it or move it.

        A brand-new tag is registered; the current declaration is idempotent. A
        *domain* tag already seeded with another declaration is CAS-moved (ADR-0006
        evolution), keeping the old declaration. An auxiliary `x_` tag is the
        agent's and never moves: a different declaration keeps E_TYPE_TAG_TAKEN.
        Runs inside `put_spec`'s transaction, so it never opens one of its own.
        """
        current = self.type_pointer(tag)
        if current is None or current == h:
            self.set_type(tag, h)
            return
        if tag.startswith("x_"):
            raise GraphError(
                tag, "E_TYPE_TAG_TAKEN", f"type tag '{tag}' is already declared as {current}"
            )
        self._move_type(tag, current, h)

    def _move_type(self, tag, expected, new):
        """CAS-move a type tag from `expected` to `new`, recording the move (ADR-0006)."""
        current = self.type_pointer(tag)
        if current != expected:
            raise GraphError(
                tag, "E_STALE_ROOT", f"type tag '{tag}' points to {current}; use apply_cas"
            )
        self._pointer_apply({"kind": "type", "name": tag, "namespace": "main"}, new)
        self.log("move_type", {"tag": tag, "from": expected, "to": new}, {"ok": True})

    def _seed_namespaces(self, kv_spec, tag_hash, migrations=None):
        """Declare the spec's KV namespaces, resolving `{"rec": tag}` types like `types`.

        A namespace already declared with a different type is evolved with
        `set_kv_namespace_type` (ADR-0009); the spec's `migrations` supply the
        function name that migrates the stored values to the new type.
        """
        migrations = migrations or {}
        current = self.kv_namespaces()
        for entry in kv_spec:
            ns = entry["ns"]
            t = self._resolve_type(entry["type"], tag_hash)
            if ns in current and _dumps(current[ns]) != _dumps(t):
                self.set_kv_namespace_type(ns, t, migrations.get(ns))
            else:
                self.put_kv_namespace(ns, t)

    def _resolve_type(self, t, tag_hash):
        """Replace a spec's `{"rec": tag}` (and nested ones) with the declared hash."""
        if isinstance(t, dict) and len(t) == 1:
            ((ctor, arg),) = t.items()
            if ctor == "rec":
                return {"rec": tag_hash[arg]}
            return {ctor: self._resolve_type(arg, tag_hash)}
        return t

    def _seed_tables(self, table_spec, types_spec, tag_hash):
        """Create nominal row declarations and persist immutable normalized SQL schemas."""
        declared = [entry["tag"] for entry in types_spec]
        normalized = normalize_tables("tables", table_spec, declared, set(declared))
        for table in normalized:
            self.register_atoms(
                [table["name"], *(ref["name"] for ref in table["refs"])]
            )
            fields = [self._resolve_type(column["type"], tag_hash) for column in table["columns"]]
            node = {"k": "rec", "tag": table["name"], "fields": fields}
            decl = ir.node_hash(node)
            current = self.type_pointer(table["name"])
            if current is not None and current != decl:
                raise GraphError(
                    table["name"], "E_BAD_SPEC", "a table schema cannot change implicitly"
                )
            if self.get(decl) is None:
                self.put(decl, node, "decl")
            self._seed_type_pointer(table["name"], decl)
            self.put_field_names(decl, [column["name"] for column in table["columns"]])
            resolved = {
                **table,
                "decl": decl,
                "columns": [
                    {**column, "type": resolved_type}
                    for column, resolved_type in zip(table["columns"], fields, strict=True)
                ],
            }
            resolved["columns_by_name"] = {
                column["name"]: column["type"] for column in resolved["columns"]
            }
            body = _dumps(resolved)
            row = self._conn.execute(
                "SELECT schema_json FROM sql_tables WHERE name = ?", (table["name"],)
            ).fetchone()
            if row is not None and row[0] != body:
                raise GraphError(
                    table["name"], "E_BAD_SPEC", "table is already declared differently"
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO sql_tables(name, schema_json) VALUES(?, ?)",
                (table["name"], body),
            )

    def table_schemas(self):
        return {
            name: json.loads(body)
            for name, body in self._conn.execute(
                "SELECT name, schema_json FROM sql_tables ORDER BY name"
            )
        }

    def table_schema(self, name):
        row = self._conn.execute(
            "SELECT schema_json FROM sql_tables WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def put_provider_grants(self, providers):
        normalized = normalize_providers("providers", providers)
        for name, operations in normalized.items():
            body = _dumps(operations)
            row = self._conn.execute(
                "SELECT operations_json FROM provider_grants WHERE name = ?", (name,)
            ).fetchone()
            if row is not None and row[0] != body:
                raise GraphError(
                    name, "E_BAD_SPEC", "provider grant is already declared differently"
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO provider_grants(name, operations_json) VALUES(?, ?)",
                (name, body),
            )

    def provider_grants(self):
        return {
            name: json.loads(body)
            for name, body in self._conn.execute(
                "SELECT name, operations_json FROM provider_grants ORDER BY name"
            )
        }

    def get_spec(self, name):
        """The stored spec by name; E_UNKNOWN_SPEC if it is not registered."""
        row = self._conn.execute("SELECT body FROM specs WHERE name = ?", (name,)).fetchone()
        if row is None:
            available = self.specs()
            detail = (
                "spec name is required" if name is None else f"unknown spec {name!r}"
            )
            detail += f"; available: {available}"
            if len(available) == 1:
                detail += f"; exactly one spec is registered, use name={available[0]!r}"
            raise GraphError(name or "spec", "E_UNKNOWN_SPEC", detail)
        return json.loads(row[0])

    def specs(self):
        """Registered spec names, sorted."""
        return [row[0] for row in self._conn.execute("SELECT name FROM specs ORDER BY name")]

    # -- KV actor (ADR-0008) -------------------------------------------------

    def kv_namespaces(self):
        """Declared namespaces as {ns: type_json}; the spec seeds them, never the agent."""
        return {
            ns: json.loads(t)
            for ns, t in self._conn.execute(
                "SELECT ns, type_json FROM kv_namespaces ORDER BY ns"
            )
        }

    def put_kv_namespace(self, ns, type_json):
        """Declare a namespace's value type. Immutable; the same type is idempotent.

        A namespace names one value type: re-declaring it with a different type is
        an explicit, CAS-protected change, never a silent overwrite (E_STALE_ROOT).
        The call is recorded in `history` (INV-21).
        """
        if not isinstance(ns, str) or ir.ATOM_RE.match(ns) is None:
            raise GraphError(ns, "E_BAD_NODE", f"namespace must match {ir.ATOM_RE.pattern}")
        ir.check_type(type_json, ns, "kv type")
        body = _dumps(type_json)
        row = self._conn.execute(
            "SELECT type_json FROM kv_namespaces WHERE ns = ?", (ns,)
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO kv_namespaces(ns, type_json) VALUES(?, ?)", (ns, body)
            )
            self._conn.execute(
                "INSERT INTO kv_type_history(ns, seq, type_json, migration_fn) "
                "VALUES(?, 1, ?, NULL)",
                (ns, body),
            )
        elif row[0] != body:
            raise GraphError(
                ns,
                "E_STALE_ROOT",
                f"namespace '{ns}' is already declared with a different type; use apply_cas",
            )
        self.log("put_kv_namespace", {"ns": ns, "type": type_json}, {"ok": True})
        return ns

    def kv_get_raw(self, ns, key):
        """(type_bytes, value_bytes) of one stored value, or None."""
        row = self._conn.execute(
            "SELECT type_bytes, value FROM kv WHERE ns = ? AND key = ?", (ns, key)
        ).fetchone()
        return None if row is None else (bytes(row[0]), bytes(row[1]))

    def kv_scan_raw(self, ns, prefix):
        """[(key, type_bytes, value_bytes)] with `prefix`, ordered by key in UTF-8 bytes."""
        rows = self._conn.execute(
            "SELECT key, type_bytes, value FROM kv WHERE ns = ? AND substr(key, 1, ?) = ?",
            (ns, len(prefix), prefix),
        ).fetchall()
        return sorted(
            ((k, bytes(tb), bytes(v)) for k, tb, v in rows),
            key=lambda row: row[0].encode("utf-8"),
        )

    def kv_commit(self, ops):
        """Apply [("put", ns, key, type_bytes, value) | ("del", ns, key)] in one transaction.

        All or nothing: any failure rolls every op back. On success the commit is
        recorded in `history` as a summary (INV-21).
        """
        batch = [_normalize_kv_op(op) for op in ops]
        ns_seen = sorted({op[1] for op in batch})
        puts = sum(1 for op in batch if op[0] == "put")
        dels = len(batch) - puts
        self._conn.execute("BEGIN")
        try:
            for op in batch:
                if op[0] == "put":
                    _, ns, key, type_bytes, value = op
                    self._conn.execute(
                        "INSERT INTO kv(ns, key, type_bytes, value) VALUES(?, ?, ?, ?) "
                        "ON CONFLICT(ns, key) DO UPDATE SET "
                        "type_bytes = excluded.type_bytes, value = excluded.value",
                        (ns, key, type_bytes, value),
                    )
                else:
                    _, ns, key = op
                    self._conn.execute("DELETE FROM kv WHERE ns = ? AND key = ?", (ns, key))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self.log(
            "kv_commit",
            {"ns": ns_seen, "puts": puts, "dels": dels},
            {"ok": True},
        )
        return {"puts": puts, "dels": dels}

    def kv_dump(self, ns=None):
        """Debug view of stored values: {ns: {key: {"type": hex, "value": hex}}}."""
        if ns is None:
            rows = self._conn.execute(
                "SELECT ns, key, type_bytes, value FROM kv ORDER BY ns, CAST(key AS BLOB)"
            )
        else:
            rows = self._conn.execute(
                "SELECT ns, key, type_bytes, value FROM kv WHERE ns = ? "
                "ORDER BY CAST(key AS BLOB)",
                (ns,),
            )
        out = {}
        for ns_name, key, type_bytes, value in rows:
            out.setdefault(ns_name, {})[key] = {
                "type": bytes(type_bytes).hex(),
                "value": bytes(value).hex(),
            }
        return out

    def kv_type_history(self, ns):
        """Declared type versions of `ns`, oldest first: [{seq, type, migration_fn}].

        `seq` 1 is the seeding; each later version names the function that migrates
        the previous version's values to it (ADR-0009).
        """
        return [
            {"seq": seq, "type": json.loads(type_json), "migration_fn": migration_fn}
            for seq, type_json, migration_fn in self._conn.execute(
                "SELECT seq, type_json, migration_fn FROM kv_type_history "
                "WHERE ns = ? ORDER BY seq",
                (ns,),
            )
        ]

    def set_kv_namespace_type(self, ns, type_json, migration_fn=None):
        """Evolve a declared namespace to a new value type, appending a version.

        `kv_namespaces()` keeps returning the current (latest) type. A namespace
        that still holds rows changes type only with a `migration_fn` (ADR-0009):
        without one it is rejected with E_SCHEMA_MIGRATION_REQUIRED. An empty
        namespace changes type freely. The call is recorded in `history`.
        """
        if not isinstance(ns, str) or ir.ATOM_RE.match(ns) is None:
            raise GraphError(ns, "E_BAD_NODE", f"namespace must match {ir.ATOM_RE.pattern}")
        ir.check_type(type_json, ns, "kv type")
        row = self._conn.execute(
            "SELECT type_json FROM kv_namespaces WHERE ns = ?", (ns,)
        ).fetchone()
        if row is None:
            raise GraphError(ns, "E_UNKNOWN_NAMESPACE", f"unknown KV namespace '{ns}'")
        if row[0] == _dumps(type_json):
            return ns
        old = json.loads(row[0])
        if migration_fn is None and self._kv_has_rows(ns):
            raise GraphError(
                ns,
                "E_SCHEMA_MIGRATION_REQUIRED",
                f"namespace '{ns}' has values; declare the migration "
                f"fn({typecheck.render_type(old, self)}) -> "
                f"{typecheck.render_type(type_json, self)}",
            )
        if migration_fn is not None and (not isinstance(migration_fn, str) or not migration_fn):
            raise GraphError(ns, "E_BAD_NODE", "migration_fn must be a non-empty string")
        seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM kv_type_history WHERE ns = ?", (ns,)
        ).fetchone()[0]
        body = _dumps(type_json)
        self._conn.execute(
            "INSERT INTO kv_type_history(ns, seq, type_json, migration_fn) VALUES(?, ?, ?, ?)",
            (ns, seq, body, migration_fn),
        )
        self._conn.execute(
            "UPDATE kv_namespaces SET type_json = ? WHERE ns = ?", (body, ns)
        )
        self.log(
            "set_kv_namespace_type",
            {"ns": ns, "seq": seq, "type": type_json, "migration_fn": migration_fn},
            {"ok": True},
        )
        return ns

    def _kv_has_rows(self, ns):
        row = self._conn.execute("SELECT 1 FROM kv WHERE ns = ? LIMIT 1", (ns,)).fetchone()
        return row is not None

    # -- effect bounds (ADR-0009) --------------------------------------------

    def put_effect_bounds(self, bounds):
        """Seed effect caps `{name: [effect, ...]}` from a spec. Immutable per name.

        Re-declaring a name with the same cap is idempotent; with a different cap it
        is rejected with E_BAD_SPEC. Caps are canonicalized (sorted, deduplicated).
        The call is recorded in `history`.
        """
        if not isinstance(bounds, dict):
            raise GraphError("spec", "E_BAD_SPEC", "effects must be an object")
        normalized = {}
        for name, effects in bounds.items():
            if not isinstance(name, str) or not name:
                raise GraphError("spec", "E_BAD_SPEC", f"invalid effect bound name {name!r}")
            if not isinstance(effects, list):
                raise GraphError("spec", "E_BAD_SPEC", f"effect bound {name!r} must be a list")
            for effect in effects:
                if not isinstance(effect, str) or not is_effect(effect):
                    raise GraphError(
                        "spec", "E_BAD_SPEC", f"invalid effect {effect!r} in bound {name!r}"
                    )
            normalized[name] = sorted(set(effects))
        for name, canonical in normalized.items():
            row = self._conn.execute(
                "SELECT effects_json FROM effect_bounds WHERE name = ?", (name,)
            ).fetchone()
            if row is not None and row[0] != _dumps(canonical):
                raise GraphError(
                    name,
                    "E_BAD_SPEC",
                    f"effect bound for '{name}' is already declared differently",
                )
        for name, canonical in normalized.items():
            self._conn.execute(
                "INSERT OR IGNORE INTO effect_bounds(name, effects_json) VALUES(?, ?)",
                (name, _dumps(canonical)),
            )
        if normalized:
            self.log("put_effect_bounds", {"names": sorted(normalized)}, {"ok": True})

    def effect_bound(self, name):
        """The declared effect cap for `name`, sorted, or None when it has none."""
        row = self._conn.execute(
            "SELECT effects_json FROM effect_bounds WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    # -- history -------------------------------------------------------------

    def log(self, kind, payload: dict, feedback: dict) -> int:
        cur = self._conn.execute(
            "INSERT INTO history(ts, kind, payload, feedback) VALUES(?, ?, ?, ?)",
            (time.time(), kind, _dumps(payload), _dumps(feedback)),
        )
        return cur.lastrowid

    def history(self, limit=100):
        """Most recent entries first. `limit=None` returns the whole history."""
        query = "SELECT id, ts, kind, payload, feedback FROM history ORDER BY id DESC"
        if limit is None:
            rows = self._conn.execute(query)
        else:
            rows = self._conn.execute(query + " LIMIT ?", (int(limit),))
        return [
            {
                "id": row[0],
                "ts": row[1],
                "kind": row[2],
                "payload": json.loads(row[3]),
                "feedback": json.loads(row[4]),
            }
            for row in rows
        ]

    def close(self):
        self._conn.close()
