"""Typed capability-provider contracts.

Contracts are trusted host metadata.  They validate ``cap`` calls during graph
definition, derive their result type and effect, and leave execution to a
provider transaction owned by the root invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .ir import GraphError

Type = str | dict[str, Any]


@dataclass(frozen=True)
class Operation:
    name: str
    effect: str
    arities: tuple[int, ...]


class Provider(Protocol):
    """A deterministic provider whose transaction follows the root transaction."""

    contract: Contract

    def begin(self): ...


class Contract:
    """Closed operation set for one provider."""

    def __init__(self, name: str, operations: tuple[Operation, ...]):
        self.name = name
        self.operations = {operation.name: operation for operation in operations}

    def signature(self, store, op: str, arg_nodes: list[dict], arg_types: list[Type], where):
        operation = self.operations.get(op)
        if operation is None:
            raise GraphError(where, "E_UNKNOWN_CAPABILITY", f"unknown operation {self.name}.{op}")
        grants = store.provider_grants().get(self.name)
        if grants is None or op not in grants:
            raise GraphError(where, "E_CAP_DENIED", f"capability {self.name}.{op} is not granted")
        if op != "insert_auto" and len(arg_types) not in operation.arities:
            allowed = ", ".join(str(value) for value in operation.arities)
            raise GraphError(
                where,
                "E_ARITY",
                f"{self.name}.{op} expects {allowed} arguments, got {len(arg_types)}",
            )
        table = _static_atom(arg_nodes[0], where, f"{self.name}.{op}.args[0]")
        schema = store.table_schema(table)
        if schema is None:
            raise GraphError(where, "E_UNKNOWN_TABLE", f"unknown table '{table}'")
        row_type = {"rec": schema["decl"]}
        primary = schema["primary_key"]
        pk_type = row_type if isinstance(primary, list) else schema["columns_by_name"][primary]

        expected: list[Type | None]
        if op == "get":
            expected, result = ["atom", pk_type], {"option": row_type}
        elif op == "insert":
            expected, result = ["atom", row_type], {"result": row_type}
        elif op == "insert_auto":
            if primary != "id" or schema["columns_by_name"]["id"] != "i64":
                raise GraphError(
                    where, "E_CONSTRAINT", "insert_auto requires an i64 id primary key"
                )
            expected = [
                "atom",
                *(column["type"] for column in schema["columns"] if column["name"] != "id"),
            ]
            if len(arg_types) != len(expected):
                raise GraphError(
                    where,
                    "E_ARITY",
                    f"sql.insert_auto expects {len(expected)} arguments, got {len(arg_types)}",
                )
            result = {"result": row_type}
        elif op == "update":
            expected, result = ["atom", pk_type, row_type], {"result": row_type}
        elif op == "delete":
            expected, result = ["atom", pk_type], {"result": row_type}
        elif op == "select":
            # predicate is a nominal typed tree; order is a column name or atom, then page ints
            expected = ["atom", None, None, "i64", "i64"][: len(arg_types)]
            result = {"list": row_type}
        else:  # join(table, declared_fk[, predicate[, order[, limit[, offset]]]])
            expected = ["atom", "atom", None, None, "i64", "i64"][: len(arg_types)]
            result = {"list": row_type}

        for index, (actual, wanted) in enumerate(zip(arg_types, expected, strict=True)):
            if wanted is not None and actual != wanted:
                raise GraphError(
                    where,
                    "E_TYPE_MISMATCH",
                    f"{self.name}.{op}.args[{index}] has type {actual!r}, expected {wanted!r}",
                )
        if op == "join":
            ref_name = _static_atom(arg_nodes[1], where, "sql.join.args[1]")
            if ref_name not in {ref["name"] for ref in schema["refs"]}:
                raise GraphError(
                    where, "E_CONSTRAINT", f"join requires a declared foreign key, got {ref_name!r}"
                )
        return result, f"{operation.effect}@{table}"


def _static_atom(node: dict, where, role: str) -> str:
    if node.get("k") != "atom":
        raise GraphError(where, "E_TYPE_MISMATCH", f"{role} must be a static atom")
    return node["name"]


SQL_CONTRACT = Contract(
    "sql",
    (
        Operation("get", "sql.read", (2,)),
        Operation("insert", "sql.write", (2,)),
        Operation("insert_auto", "sql.write", ()),
        Operation("update", "sql.write", (3,)),
        Operation("delete", "sql.write", (2,)),
        Operation("select", "sql.read", (1, 2, 3, 4, 5)),
        Operation("join", "sql.read", (2, 3, 4, 5, 6)),
    ),
)

CONTRACTS = {SQL_CONTRACT.name: SQL_CONTRACT}


def get_contract(name: str, where=None) -> Contract:
    try:
        return CONTRACTS[name]
    except KeyError:
        raise GraphError(where, "E_UNKNOWN_PROVIDER", f"unknown provider '{name}'") from None
