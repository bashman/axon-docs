"""Deterministic in-memory implementation of the ``sql`` provider."""

from __future__ import annotations

import copy
from typing import Any

from . import values
from .ir import GraphError
from .providers import SQL_CONTRACT

MAX_ROWS = 65_536


def _constraint(name: str):
    return {
        "tag": "err",
        "v": {
            "code": {"atom": "E_CONSTRAINT"},
            "detail": {"tag": "some", "v": name},
        },
    }


def _ok(value):
    return {"tag": "ok", "v": copy.deepcopy(value)}


class MemorySql:
    """Table state shared by root invocations; mutations happen through ``SqlTx``."""

    contract = SQL_CONTRACT

    def __init__(self, schemas: dict[str, dict], get_node):
        self.schemas = copy.deepcopy(schemas)
        self.get_node = get_node
        self._rows = {name: {} for name in schemas}

    def begin(self):
        return SqlTx(self)

    def load(self, state: dict[str, list[dict]]):
        if not isinstance(state, dict):
            raise GraphError("tables", "E_SPEC_TYPE", "table state must be an object")
        self._rows = {name: {} for name in self.schemas}
        tx = self.begin()
        for table, rows in state.items():
            if table not in self.schemas or not isinstance(rows, list):
                raise GraphError(table, "E_SPEC_TYPE", "invalid table state")
            for row in rows:
                result = tx.insert(table, row)
                if result["tag"] == "err":
                    raise GraphError(
                        table,
                        "E_SPEC_TYPE",
                        f"initial table state violates {result['v']['detail']['v']}",
                    )
        tx.commit()

    def snapshot(self):
        out = {}
        for name, schema in sorted(self.schemas.items()):
            rows = sorted(
                self._rows[name].values(),
                key=lambda row: _sort_key(_primary_value(schema, row)),
            )
            if rows:
                out[name] = copy.deepcopy(rows)
        return out


class SqlTx:
    """Copy-on-write SQL transaction with read-your-writes semantics."""

    def __init__(self, backend: MemorySql):
        self.backend = backend
        self.rows = copy.deepcopy(backend._rows)
        self.closed = False

    def commit(self):
        self._open()
        self.backend._rows = self.rows
        self.closed = True

    def rollback(self):
        self.closed = True

    def call(self, op: str, args: list[Any]):
        if not args:
            raise GraphError("cap", "E_ARITY", f"sql.{op} needs a table")
        table = _atom(args[0])
        rest = args[1:]
        if op == "get":
            return self.get(table, *rest)
        if op == "insert":
            return self.insert(table, *rest)
        if op == "insert_auto":
            return self.insert_auto(table, *rest)
        if op == "update":
            return self.update(table, *rest)
        if op == "delete":
            return self.delete(table, *rest)
        if op == "select":
            return self.select(table, *rest)
        if op == "join":
            joined = self.join(table, _atom(rest[0]), *rest[1:])
            return [item["left"] for item in joined]
        raise GraphError("cap", "E_UNKNOWN_CAPABILITY", f"unknown operation sql.{op}")

    def get(self, table: str, key):
        schema = self._schema(table)
        row = self.rows[table].get(_lookup_key(schema, key))
        return {"tag": "none"} if row is None else {"tag": "some", "v": copy.deepcopy(row)}

    def insert(self, table: str, row: dict):
        schema = self._schema(table)
        violation = self._row_violation(table, row)
        if violation:
            return _constraint(violation)
        key = _key(_primary_value(schema, row))
        if key in self.rows[table]:
            return _constraint(schema["primary_name"])
        self.rows[table][key] = copy.deepcopy(row)
        return _ok(row)

    def insert_auto(self, table: str, *values):
        schema = self._schema(table)
        columns = [column["name"] for column in schema["columns"] if column["name"] != "id"]
        given = dict(zip(columns, values, strict=True))
        given["id"] = max((item["id"] for item in self.rows[table].values()), default=0) + 1
        # Records are read by position: keep the schema's column order, not the insertion order.
        row = {column["name"]: given[column["name"]] for column in schema["columns"]}
        return self.insert(table, row)

    def update(self, table: str, key, row: dict):
        schema = self._schema(table)
        old_key = _lookup_key(schema, key)
        if old_key not in self.rows[table]:
            return _constraint(schema["primary_name"])
        if not isinstance(row, dict) or _key(_primary_value(schema, row)) != old_key:
            return _constraint(schema["primary_name"])
        previous = self.rows[table].pop(old_key)
        violation = self._row_violation(table, row)
        if violation:
            self.rows[table][old_key] = previous
            return _constraint(violation)
        self.rows[table][old_key] = copy.deepcopy(row)
        return _ok(row)

    def delete(self, table: str, key):
        schema = self._schema(table)
        stored_key = _lookup_key(schema, key)
        row = self.rows[table].get(stored_key)
        if row is None:
            return _constraint(schema["primary_name"])
        for child_name, child_schema in self.backend.schemas.items():
            for ref in child_schema["refs"]:
                if ref["table"] != table:
                    continue
                target = row[ref["ref_column"]]
                if any(child[ref["column"]] == target for child in self.rows[child_name].values()):
                    return _constraint(ref["name"])
        del self.rows[table][stored_key]
        return _ok(row)

    def select(
        self,
        table: str,
        where: dict | None = None,
        order: Any = None,
        limit: int = MAX_ROWS,
        offset: int = 0,
    ):
        schema = self._schema(table)
        if not _page(limit, offset):
            raise GraphError(table, "E_CONSTRAINT", "pagination")
        rows = [row for row in self.rows[table].values() if _matches(schema, row, where)]
        column, descending = _order(order, schema["primary_key"])
        if column not in schema["columns_by_name"]:
            raise GraphError(table, "E_CONSTRAINT", f"order:{column}")
        rows.sort(
            key=lambda row: (_sort_key(row[column]), _sort_key(_primary_value(schema, row)))
        )
        if descending:
            rows.reverse()
        return copy.deepcopy(rows[offset : offset + limit])

    def join(self, table: str, ref_name: str, where=None, order=None, limit=MAX_ROWS, offset=0):
        schema = self._schema(table)
        ref = next((item for item in schema["refs"] if item["name"] == ref_name), None)
        if ref is None:
            raise GraphError(table, "E_CONSTRAINT", ref_name)
        left = self.select(table, where, order, MAX_ROWS, 0)
        target_rows = self.rows[ref["table"]].values()
        index = {row[ref["ref_column"]]: row for row in target_rows}
        joined = [
            {"left": row, "right": copy.deepcopy(index[row[ref["column"]]])}
            for row in left
            if row[ref["column"]] in index
        ]
        if not _page(limit, offset):
            raise GraphError(table, "E_CONSTRAINT", "pagination")
        return copy.deepcopy(joined[offset : offset + limit])

    def _schema(self, table):
        self._open()
        schema = self.backend.schemas.get(table)
        if schema is None:
            raise GraphError(table, "E_UNKNOWN_TABLE", f"unknown table '{table}'")
        return schema

    def _row_violation(self, table, row):
        schema = self._schema(table)
        row_type = {"rec": schema["decl"]}
        reason = values.is_encodable(self.backend.get_node, row_type, row)
        if reason:
            raise GraphError(table, "E_TYPE_MISMATCH", reason)
        for unique in schema["unique"]:
            candidate = tuple(row[column] for column in unique["columns"])
            if any(
                tuple(other[column] for column in unique["columns"]) == candidate
                for other in self.rows[table].values()
            ):
                return unique["name"]
        for ref in schema["refs"]:
            value = row[ref["column"]]
            if not any(
                target[ref["ref_column"]] == value
                for target in self.rows[ref["table"]].values()
            ):
                return ref["name"]
        return None

    def _open(self):
        if self.closed:
            raise RuntimeError("SQL transaction is closed")


def _atom(value):
    if isinstance(value, dict) and set(value) == {"atom"}:
        return value["atom"]
    if isinstance(value, str):
        return value
    raise GraphError("cap", "E_TYPE_MISMATCH", "expected an atom")


def _key(value):
    if isinstance(value, dict):
        return tuple(sorted((key, _key(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_key(item) for item in value)
    return value


def _primary_value(schema, row):
    primary = schema["primary_key"]
    if isinstance(primary, list):
        return {column: row.get(column) for column in primary}
    return row.get(primary)


def _lookup_key(schema, value):
    primary = schema["primary_key"]
    if isinstance(primary, list):
        if not isinstance(value, dict):
            return _key(value)
        return _key({column: value.get(column) for column in primary})
    return _key(value)


def _sort_key(value):
    if isinstance(value, dict) and set(value) == {"atom"}:
        value = value["atom"]
    return (type(value).__name__, value)


def _page(limit, offset):
    integers = all(
        isinstance(value, int) and not isinstance(value, bool) for value in (limit, offset)
    )
    return integers and 0 <= limit <= MAX_ROWS and offset >= 0


def _order(order, default):
    if isinstance(default, list):
        default = default[0]
    if order is None or order == {"atom": "none"}:
        return default, False
    if isinstance(order, dict) and set(order) == {"atom"}:
        order = order["atom"]
    if isinstance(order, str):
        return (order[1:], True) if order.startswith("-") else (order, False)
    if isinstance(order, dict) and set(order) >= {"column"}:
        return order["column"], order.get("direction") == {"atom": "desc"} or order.get(
            "direction"
        ) == "desc"
    raise GraphError("select", "E_CONSTRAINT", "order")


def _matches(schema, row, predicate):
    if predicate is None or predicate == {"atom": "all"}:
        return True
    if not isinstance(predicate, dict):
        raise GraphError(schema["name"], "E_CONSTRAINT", "where")
    if "op" in predicate:
        op = _atom(predicate["op"])
        if op in {"and", "or"}:
            children = predicate.get("args", predicate.get("children"))
            return _logic(schema, row, op, children)
        return _compare(schema, row, op, predicate.get("column"), predicate.get("value"))
    if len(predicate) != 1:
        raise GraphError(schema["name"], "E_CONSTRAINT", "where")
    op, body = next(iter(predicate.items()))
    if op in {"and", "or"}:
        return _logic(schema, row, op, body)
    if isinstance(body, dict):
        return _compare(schema, row, op, body.get("column"), body.get("value"))
    if isinstance(body, list) and len(body) == 2:
        return _compare(schema, row, op, body[0], body[1])
    raise GraphError(schema["name"], "E_CONSTRAINT", "where")


def _logic(schema, row, op, children):
    if not isinstance(children, list) or not children:
        raise GraphError(schema["name"], "E_CONSTRAINT", "where")
    results = (_matches(schema, row, child) for child in children)
    return all(results) if op == "and" else any(results)


def _compare(schema, row, op, column, value):
    column = _atom(column) if isinstance(column, dict) else column
    if column not in schema["columns_by_name"] or op not in {"eq", "lt"}:
        raise GraphError(schema["name"], "E_CONSTRAINT", f"where:{column}")
    return row[column] == value if op == "eq" else row[column] < value
