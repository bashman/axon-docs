"""Propagation of a node replacement to everything that depends on it.

`call.fn` binds the callee by hash, so a function written before a replacement
still points at the old id: moving the pointer alone would silently keep running
the old code. `plan_replacement` computes the cascade.

Given an existing hash `old` and a stored hash `new` of the same type, it climbs
the `dependents` (parents via `edges`) transitively. Every ancestor is rebuilt
with each replaced child swapped for its new id (single-child fields and lists
are handled through `ir.CHILD_FIELDS`), stored under its new hash and typed with
`typecheck.check`. Because ids are Merkle roots, a parent may only be rebuilt
after every affected child is final: the walk repeats until no replacement
changes, so a diamond (two routes to the same ancestor) is rewritten once.

Reconstruction is bottom-up: children before parents, then parents before
grandparents, up to the named `fn`s. If any rebuilt node fails to typecheck the
whole plan is rejected with that error. Nodes already stored stay (they are
inert), but no pointer is moved: this function never applies the CAS, so the
caller can do `store.apply_cas(plan["moves"])` atomically.
"""

from . import ir, typecheck
from .ir import GraphError


def plan_replacement(store, old, new):
    """Cascade that replaces `old` by `new`, as `{"replaced": {...}, "moves": [...]}`.

    `replaced` maps every old id to its new id (including `old -> new`). `moves`
    holds one CAS change per name/root that pointed at a replaced id, ready for
    `store.apply_cas`. Raising rejects the whole plan and moves nothing.
    """
    if old == new:
        return {"replaced": {}, "moves": []}
    old_type = store.type_of(old)
    if old_type is None:
        raise GraphError(old, "E_UNKNOWN_REF", f"no node with id {old}")
    new_type = store.type_of(new)
    if new_type is None:
        raise GraphError(new, "E_UNKNOWN_REF", f"no node with id {new}")
    ancestors = _ancestors(store, old)
    # with no dependents, changing the signature only moves pointers (nothing to re-check)
    if old_type != new_type and len(ancestors) > 1:
        raise GraphError(
            old,
            "E_TYPE_MISMATCH",
            f"cannot replace {old} of type {old_type} with {new} of type {new_type}",
        )

    replaced = {old: new}
    dirty = [old]
    while dirty:
        parents = set()
        for child in dirty:
            for parent in store.dependents(child):
                if parent in ancestors:
                    parents.add(parent)
        next_dirty = set()
        for parent in parents:
            rebuilt = _substitute(store.get(parent), replaced)
            new_hash = ir.node_hash(rebuilt)
            if replaced.get(parent) == new_hash:
                continue  # already final; nothing above it needs revisiting
            replaced[parent] = new_hash
            store.put(new_hash, rebuilt, typecheck.check(store, rebuilt))
            next_dirty.add(parent)
        dirty = next_dirty

    return {"replaced": replaced, "moves": _moves(store, replaced)}


def _ancestors(store, h):
    """`h` and every node that references it, transitively, in the original graph.

    Rebuilding a parent can store fresh nodes whose edges point at children that
    are not final yet, so those intermediates would look like extra dependents.
    Only the original ancestors carry pointers and must be rewritten, so the walk
    is confined to this set.
    """
    seen = {h}
    frontier = [h]
    while frontier:
        nxt = []
        for node in frontier:
            for parent in store.dependents(node):
                if parent not in seen:
                    seen.add(parent)
                    nxt.append(parent)
        frontier = nxt
    return seen


def _substitute(node, replaced):
    """`node` with every child id present in `replaced` swapped for its new id."""
    singles, lists = ir.CHILD_FIELDS[node["k"]]
    out = dict(node)
    for field in singles:
        out[field] = replaced.get(node[field], node[field])
    for field in lists:
        out[field] = [replaced.get(c, c) for c in node[field]]
    return out


def _moves(store, replaced):
    """One CAS change per name/root pointing at a replaced id, deterministically ordered."""
    moves = []
    for old, new in replaced.items():
        for pointer in store.pointers_of(old):
            moves.append({"pointer": pointer, "expected": old, "new": new})
    moves.sort(key=_move_key)
    return moves


def _move_key(move):
    pointer = move["pointer"]
    return (
        pointer["kind"],
        pointer.get("namespace", ""),
        pointer["name"],
        move["expected"],
        move["new"],
    )
