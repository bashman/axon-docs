"""PostgreSQL implementation of the trusted ``sql`` provider contract."""

from __future__ import annotations

import copy
from typing import Any

from . import values
from .ir import GraphError
from .memsql import MAX_ROWS, _atom, _constraint, _ok, _order, _page, _primary_value, _sort_key
from .providers import SQL_CONTRACT


def _driver():
    try:
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL backend requires psycopg; install AXON with the 'postgres' extra"
        ) from exc
    return psycopg, sql, Jsonb


class PostgresSql:
    """PostgreSQL state shared by root invocations, with one connection per transaction."""

    contract = SQL_CONTRACT

    def __init__(self, schemas: dict[str, dict], get_node, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("--pg-dsn is required when --backend postgres is selected")
        self.schemas = copy.deepcopy(schemas)
        self.get_node = get_node
        self.dsn = dsn
        self._psycopg, self._sql, self._Jsonb = _driver()
        self._create_schema()

    def begin(self):
        return PostgresTx(self, self._psycopg.connect(self.dsn))

    def load(self, state: dict[str, list[dict]]):
        if not isinstance(state, dict):
            raise GraphError("tables", "E_SPEC_TYPE", "table state must be an object")
        if any(
            table not in self.schemas or not isinstance(rows, list)
            for table, rows in state.items()
        ):
            raise GraphError("tables", "E_SPEC_TYPE", "invalid table state")
        tx = self.begin()
        try:
            tables = [self._identifier(name) for name in self.schemas]
            if tables:
                tx.cursor.execute(
                    self._sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                        self._sql.SQL(", ").join(tables)
                    )
                )
            for table, rows in state.items():
                for row in rows:
                    result = tx.insert(table, row)
                    if result["tag"] == "err":
                        raise GraphError(
                            table,
                            "E_SPEC_TYPE",
                            f"initial table state violates {result['v']['detail']['v']}",
                        )
            tx.commit()
        except BaseException:
            tx.rollback()
            raise

    def snapshot(self):
        tx = self.begin()
        try:
            out = {}
            for name, schema in sorted(self.schemas.items()):
                rows = tx.select(name)
                rows.sort(key=lambda row: _sort_key(_primary_value(schema, row)))
                if rows:
                    out[name] = rows
            return out
        finally:
            tx.rollback()

    def _create_schema(self):
        conn = self._psycopg.connect(self.dsn, autocommit=True)
        try:
            with conn.cursor() as cursor:
                for table, schema in self.schemas.items():
                    columns = [
                        self._sql.SQL("{} {} {}").format(
                            self._identifier(column["name"]),
                            self._sql.SQL(_sql_type(column["type"])),
                            self._sql.SQL("" if _is_option(column["type"]) else "NOT NULL"),
                        )
                        for column in schema["columns"]
                    ]
                    constraints = [
                        self._sql.SQL("CONSTRAINT {} PRIMARY KEY ({})").format(
                            self._identifier(schema["primary_name"]),
                            self._identifiers(schema["primary_key"]),
                        )
                    ]
                    constraints.extend(
                        self._sql.SQL("CONSTRAINT {} UNIQUE ({})").format(
                            self._identifier(unique["name"]),
                            self._identifiers(unique["columns"]),
                        )
                        for unique in schema["unique"]
                    )
                    cursor.execute(
                        self._sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
                            self._identifier(table),
                            self._sql.SQL(", ").join([*columns, *constraints]),
                        )
                    )
                for table, schema in self.schemas.items():
                    for ref in schema["refs"]:
                        cursor.execute(
                            "SELECT 1 FROM pg_constraint "
                            "WHERE conname = %s AND conrelid = %s::regclass",
                            (ref["name"], table),
                        )
                        if cursor.fetchone() is None:
                            cursor.execute(
                                self._sql.SQL(
                                    "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) "
                                    "REFERENCES {} ({})"
                                ).format(
                                    self._identifier(table),
                                    self._identifier(ref["name"]),
                                    self._identifier(ref["column"]),
                                    self._identifier(ref["table"]),
                                    self._identifier(ref["ref_column"]),
                                )
                            )
        finally:
            conn.close()

    def _identifier(self, name: str):
        # Every identifier must originate in the normalized, human-owned table schema.
        allowed = set(self.schemas)
        for schema in self.schemas.values():
            allowed.update(schema["columns_by_name"])
            allowed.add(schema["primary_name"])
            allowed.update(item["name"] for item in schema["unique"])
            allowed.update(item["name"] for item in schema["refs"])
        if name not in allowed:
            raise GraphError("sql", "E_CONSTRAINT", f"unknown identifier {name!r}")
        return self._sql.Identifier(name)

    def _identifiers(self, names: str | list[str]):
        if isinstance(names, str):
            names = [names]
        return self._sql.SQL(", ").join(self._identifier(name) for name in names)


class PostgresTx:
    """A root-scoped PostgreSQL transaction with memsql-compatible values."""

    def __init__(self, backend: PostgresSql, connection):
        self.backend = backend
        self.connection = connection
        self.cursor = connection.cursor()
        self.closed = False

    def commit(self):
        self._open()
        try:
            self.connection.commit()
        finally:
            self._close()

    def rollback(self):
        if self.closed:
            return
        try:
            self.connection.rollback()
        finally:
            self._close()

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
        clause, params = self._primary_clause(schema, key)
        self.cursor.execute(
            self.backend._sql.SQL("SELECT {} FROM {} WHERE {}").format(
                self.backend._identifiers(_column_names(schema)),
                self.backend._identifier(table),
                clause,
            ),
            params,
        )
        row = self.cursor.fetchone()
        return {"tag": "none"} if row is None else {"tag": "some", "v": self._decode(schema, row)}

    def insert(self, table: str, row: dict):
        schema = self._schema(table)
        self._validate_row(table, schema, row)
        columns = _column_names(schema)
        query = self.backend._sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
            self.backend._identifier(table),
            self.backend._identifiers(columns),
            self.backend._sql.SQL(", ").join(self.backend._sql.Placeholder() for _ in columns),
            self.backend._identifiers(columns),
        )
        params = [self._encode(schema, name, row[name]) for name in columns]
        return self._write(schema, query, params)

    def insert_auto(self, table: str, *values):
        schema = self._schema(table)
        columns = [name for name in _column_names(schema) if name != "id"]
        self.cursor.execute(
            self.backend._sql.SQL("LOCK TABLE {} IN EXCLUSIVE MODE").format(
                self.backend._identifier(table)
            )
        )
        query = self.backend._sql.SQL(
            "INSERT INTO {} ({}, {}) SELECT COALESCE(MAX({}), 0) + 1, {} FROM {} RETURNING {}"
        ).format(
            self.backend._identifier(table),
            self.backend._identifier("id"),
            self.backend._identifiers(columns),
            self.backend._identifier("id"),
            self.backend._sql.SQL(", ").join(self.backend._sql.Placeholder() for _ in columns),
            self.backend._identifier(table),
            self.backend._identifiers(_column_names(schema)),
        )
        params = [
            self._encode(schema, name, value)
            for name, value in zip(columns, values, strict=True)
        ]
        return self._write(schema, query, params)

    def update(self, table: str, key, row: dict):
        schema = self._schema(table)
        self._validate_row(table, schema, row)
        if _primary_value(schema, row) != key:
            return _constraint(schema["primary_name"])
        columns = _column_names(schema)
        assignments = self.backend._sql.SQL(", ").join(
            self.backend._sql.SQL("{} = {}").format(
                self.backend._identifier(name), self.backend._sql.Placeholder()
            )
            for name in columns
        )
        clause, key_params = self._primary_clause(schema, key)
        query = self.backend._sql.SQL("UPDATE {} SET {} WHERE {} RETURNING {}").format(
            self.backend._identifier(table), assignments, clause, self.backend._identifiers(columns)
        )
        params = [self._encode(schema, name, row[name]) for name in columns] + key_params
        return self._write(schema, query, params, missing=schema["primary_name"])

    def delete(self, table: str, key):
        schema = self._schema(table)
        clause, params = self._primary_clause(schema, key)
        query = self.backend._sql.SQL("DELETE FROM {} WHERE {} RETURNING {}").format(
            self.backend._identifier(table),
            clause,
            self.backend._identifiers(_column_names(schema)),
        )
        return self._write(schema, query, params, missing=schema["primary_name"])

    def select(self, table: str, where=None, order=None, limit=MAX_ROWS, offset=0):
        schema = self._schema(table)
        if not _page(limit, offset):
            raise GraphError(table, "E_CONSTRAINT", "pagination")
        predicate, params = self._where(schema, where, alias=None)
        column, descending = _order(order, schema["primary_key"])
        if column not in schema["columns_by_name"]:
            raise GraphError(table, "E_CONSTRAINT", f"order:{column}")
        primary = schema["primary_key"]
        primary = [primary] if isinstance(primary, str) else primary
        direction = self.backend._sql.SQL("DESC" if descending else "ASC")
        ordering = self.backend._sql.SQL(", ").join(
            self.backend._sql.SQL("{} {}").format(self.backend._identifier(name), direction)
            for name in [column, *primary]
        )
        query = self.backend._sql.SQL(
            "SELECT {} FROM {} WHERE {} ORDER BY {} LIMIT %s OFFSET %s"
        ).format(
            self.backend._identifiers(_column_names(schema)),
            self.backend._identifier(table),
            predicate,
            ordering,
        )
        self.cursor.execute(query, [*params, limit, offset])
        return [self._decode(schema, row) for row in self.cursor.fetchall()]

    def join(self, table: str, ref_name: str, where=None, order=None, limit=MAX_ROWS, offset=0):
        schema = self._schema(table)
        ref = next((item for item in schema["refs"] if item["name"] == ref_name), None)
        if ref is None:
            raise GraphError(table, "E_CONSTRAINT", ref_name)
        if not _page(limit, offset):
            raise GraphError(table, "E_CONSTRAINT", "pagination")
        left_alias = self.backend._sql.Identifier("axon_left")
        right_alias = self.backend._sql.Identifier("axon_right")
        predicate, params = self._where(schema, where, alias=left_alias)
        column, descending = _order(order, schema["primary_key"])
        if column not in schema["columns_by_name"]:
            raise GraphError(table, "E_CONSTRAINT", f"order:{column}")
        left_columns = _column_names(schema)
        right_schema = self.backend.schemas[ref["table"]]
        selected = [
            self.backend._sql.SQL("{}.{}").format(left_alias, self.backend._identifier(name))
            for name in left_columns
        ] + [
            self.backend._sql.SQL("{}.{}").format(right_alias, self.backend._identifier(name))
            for name in _column_names(right_schema)
        ]
        primary = schema["primary_key"]
        primary = [primary] if isinstance(primary, str) else primary
        ordering = [column, *primary]
        direction = self.backend._sql.SQL("DESC" if descending else "ASC")
        order_sql = self.backend._sql.SQL(", ").join(
            self.backend._sql.SQL("{}.{} {}").format(
                left_alias, self.backend._identifier(name), direction
            )
            for name in ordering
        )
        query = self.backend._sql.SQL(
            "SELECT {} FROM {} AS {} JOIN {} AS {} ON {}.{} = {}.{} "
            "WHERE {} ORDER BY {} LIMIT %s OFFSET %s"
        ).format(
            self.backend._sql.SQL(", ").join(selected),
            self.backend._identifier(table), left_alias,
            self.backend._identifier(ref["table"]), right_alias,
            left_alias, self.backend._identifier(ref["column"]),
            right_alias, self.backend._identifier(ref["ref_column"]),
            predicate, order_sql,
        )
        self.cursor.execute(query, [*params, limit, offset])
        split = len(left_columns)
        return [
            {
                "left": self._decode(schema, row[:split]),
                "right": self._decode(right_schema, row[split:]),
            }
            for row in self.cursor.fetchall()
        ]

    def _write(self, schema, query, params, missing=None):
        self.cursor.execute("SAVEPOINT axon_operation")
        try:
            self.cursor.execute(query, params)
            row = self.cursor.fetchone()
            self.cursor.execute("RELEASE SAVEPOINT axon_operation")
        except self.backend._psycopg.IntegrityError as exc:
            name = exc.diag.constraint_name or "constraint"
            self.cursor.execute("ROLLBACK TO SAVEPOINT axon_operation")
            self.cursor.execute("RELEASE SAVEPOINT axon_operation")
            return _constraint(name)
        return _constraint(missing) if row is None else _ok(self._decode(schema, row))

    def _where(self, schema, predicate, alias):
        sql = self.backend._sql
        if predicate is None or predicate == {"atom": "all"}:
            return sql.SQL("TRUE"), []
        if not isinstance(predicate, dict):
            raise GraphError(schema["name"], "E_CONSTRAINT", "where")
        if "op" in predicate:
            op = _atom(predicate["op"])
            if op in {"and", "or"}:
                children = predicate.get("args", predicate.get("children"))
                return self._logic(schema, op, children, alias)
            return self._compare(schema, op, predicate.get("column"), predicate.get("value"), alias)
        if len(predicate) != 1:
            raise GraphError(schema["name"], "E_CONSTRAINT", "where")
        op, body = next(iter(predicate.items()))
        if op in {"and", "or"}:
            return self._logic(schema, op, body, alias)
        if isinstance(body, dict):
            return self._compare(schema, op, body.get("column"), body.get("value"), alias)
        if isinstance(body, list) and len(body) == 2:
            return self._compare(schema, op, body[0], body[1], alias)
        raise GraphError(schema["name"], "E_CONSTRAINT", "where")

    def _logic(self, schema, op, children, alias):
        if not isinstance(children, list) or not children:
            raise GraphError(schema["name"], "E_CONSTRAINT", "where")
        parts, params = [], []
        for child in children:
            part, child_params = self._where(schema, child, alias)
            parts.append(self.backend._sql.SQL("({})").format(part))
            params.extend(child_params)
        glue = self.backend._sql.SQL(" AND " if op == "and" else " OR ")
        return glue.join(parts), params

    def _compare(self, schema, op, column, value, alias):
        column = _atom(column) if isinstance(column, dict) else column
        if column not in schema["columns_by_name"] or op not in {"eq", "lt"}:
            raise GraphError(schema["name"], "E_CONSTRAINT", f"where:{column}")
        identifier = self.backend._identifier(column)
        if alias is not None:
            identifier = self.backend._sql.SQL("{}.{}").format(alias, identifier)
        return self.backend._sql.SQL("{} {} %s").format(
            identifier, self.backend._sql.SQL("=" if op == "eq" else "<")
        ), [self._encode(schema, column, value)]

    def _primary_clause(self, schema, key):
        primary = schema["primary_key"]
        if isinstance(primary, str):
            values_by_column = {primary: key}
        elif isinstance(key, dict):
            values_by_column = {column: key.get(column) for column in primary}
        else:
            values_by_column = {column: None for column in primary}
        parts = [
            self.backend._sql.SQL("{} = %s").format(self.backend._identifier(column))
            for column in values_by_column
        ]
        params = [self._encode(schema, column, value) for column, value in values_by_column.items()]
        return self.backend._sql.SQL(" AND ").join(parts), params

    def _validate_row(self, table, schema, row):
        reason = values.is_encodable(self.backend.get_node, {"rec": schema["decl"]}, row)
        if reason:
            raise GraphError(table, "E_TYPE_MISMATCH", reason)

    def _encode(self, schema, column, value):
        type_ = schema["columns_by_name"][column]
        while isinstance(type_, dict) and set(type_) == {"option"}:
            if value == {"tag": "none"}:
                return None
            value = value["v"]
            type_ = type_["option"]
        if type_ == "atom":
            return _atom(value)
        if isinstance(type_, dict) or type_ in {"error", "unit"}:
            return self.backend._Jsonb(value)
        return value

    def _decode(self, schema, row):
        return {
            name: _decode_value(schema["columns_by_name"][name], value)
            for name, value in zip(_column_names(schema), row, strict=True)
        }

    def _schema(self, table):
        self._open()
        schema = self.backend.schemas.get(table)
        if schema is None:
            raise GraphError(table, "E_UNKNOWN_TABLE", f"unknown table '{table}'")
        return schema

    def _open(self):
        if self.closed:
            raise RuntimeError("SQL transaction is closed")

    def _close(self):
        if self.closed:
            return
        self.closed = True
        self.cursor.close()
        self.connection.close()


def _is_option(type_):
    return isinstance(type_, dict) and set(type_) == {"option"}


def _column_names(schema):
    """Preserve nominal record order instead of JSON object's sorted key order."""
    return [column["name"] for column in schema["columns"]]


def _sql_type(type_):
    while _is_option(type_):
        type_ = type_["option"]
    return {"i64": "BIGINT", "bool": "BOOLEAN", "string": "TEXT", "atom": "TEXT"}.get(
        type_, "JSONB"
    )


def _decode_value(type_, value):
    if _is_option(type_):
        if value is None:
            return {"tag": "none"}
        return {"tag": "some", "v": _decode_value(type_["option"], value)}
    if type_ == "atom":
        return {"atom": value}
    return value
