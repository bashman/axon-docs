"""Executable spec: the human-authored cases the phase-1 gate counts.

A spec is a JSON object with a `name`, the `atoms` it seeds, an optional `types`
list of nominal record declarations, an optional `kv` list of typed namespaces,
and a list of `cases`. Each case names a function, passes ADR-0005 JSON boundary
values and expects either a result value (`{"result": X}`) or an error code
(`{"error": "E_CODE"}`); `origin` marks whether the human or the agent wrote it.
The spec belongs to the human: the agent only reads a registered spec and runs
it by name (ADR-0002 §7).

A case is either short (`fn`/`args`/`expect`, plus an optional `clock`) or a
sequence of `steps`; both forms may carry an initial `state` and a final
`state_after` (ADR-0008). Each step runs on its own clock value.

`validate_spec` rejects a malformed spec with E_BAD_SPEC. A spec may also cap the
effects of its named roots (`"effects"`) and declare schema migrations for its KV
namespaces (`"migrations"`, ADR-0009). `run_cases` executes every case through
`run(ref, args[, clock=ms])` (a `Service.run`) and returns a deterministic summary;
no case can abort the others, and comparison is strict (True is not 1). With a state
runner (`reset`/`snapshot`) each case starts from its own state and `state_after` is
compared at the end; with a `store`, a step that no longer fits the graph fails with
E_SPEC_TYPE instead of running.
"""

import re

from . import ir, typecheck, values
from .ir import GraphError

ORIGINS = ("human", "agent")
HTTP_METHODS = ("DELETE", "GET", "PATCH", "POST", "PUT")
HTTP_SCOPES = ("read", "write", "admin")

# Effect names a spec cap accepts (ADR-0009): `kv.read@ns | kv.write@ns |
# clock@host | print@stdout`, with `ns` an atom.
_EFFECT_ATOM = r"[A-Za-z][A-Za-z0-9_]{0,63}"
EFFECT_RE = re.compile(
    rf"^(?:kv\.(?:read|write)@{_EFFECT_ATOM}|sql\.(?:read|write)@{_EFFECT_ATOM}"
    rf"|clock@host|print@stdout)$"
)

_UNSET = object()


def describe(spec) -> str:
    """Render the declarations of a validated spec as compact, stable text."""
    validate_spec(spec)
    lines = [f"spec {spec['name']}"]

    types = spec.get("types", [])
    lines.append("records:")
    for entry in types:
        fields = ", ".join(
            f"{field['name']}: {_type_text(field['type'])}" for field in entry["fields"]
        )
        lines.append(f"- {entry['tag']} {{{fields}}}")
    if not types:
        lines.append("- none")

    atoms = spec.get("atoms", [])
    lines.append(f"atoms: {', '.join(atoms) if atoms else 'none'}")

    namespaces = spec.get("kv", [])
    lines.append("kv namespaces:")
    lines.extend(f"- {entry['ns']}: {_type_text(entry['type'])}" for entry in namespaces)
    if not namespaces:
        lines.append("- none")

    tables = normalize_tables(
        spec["name"],
        spec.get("tables", []),
        [entry["tag"] for entry in types],
        {entry["tag"] for entry in types},
    )
    lines.append("sql tables:")
    for table in tables:
        columns = ", ".join(
            f"{column['name']}: {_type_text(column['type'])}" for column in table["columns"]
        )
        primary = table["primary_key"]
        primary_text = ",".join(primary) if isinstance(primary, list) else primary
        unique = ", ".join(
            f"{constraint['name']}({','.join(constraint['columns'])})"
            for constraint in table["unique"]
        )
        refs = ", ".join(
            f"{ref['name']}({ref['column']}->{ref['table']}.{ref['ref_column']})"
            for ref in table["refs"]
        )
        lines.append(
            f"- {table['name']} {{{columns}}}; pk={primary_text}; "
            f"unique={unique or 'none'}; refs={refs or 'none'}"
        )
    if not tables:
        lines.append("- none")

    if tables:
        lines.append(
            "sql forms (a row is the record named after the table; no predicate = all rows):"
        )
        names = ", ".join(table["name"] for table in tables)
        lines.append(f"- list all: (cap sql select <table>) for {names}")
        lines.append("- by pk: (cap sql get|update|delete <table> <pk>)")
        lines.append("- insert: (cap sql insert <table> (mk <table> field=value ...))")
        lines.append("- auto id: (cap sql insert_auto <table> <non-id fields in table order>)")
        lines.append("- filter: (cap sql select <table> <sql_predicate>); omit it to read all")

    routes = spec.get("routes", [])
    rate = spec.get("rate")
    lines.append("http routes (request=request, response=response):")
    for route in routes:
        route_rate = "off"
        if route["rate"] and rate:
            route_rate = f"{rate['limit']}/{rate['window_ms']}ms per {rate['key']}"
        lines.append(
            f"- {route['method']} {route['path']} -> {route['handler']}; "
            f"auth={_auth_text(route['auth'])}; rate={route_rate}"
        )
    if not routes:
        lines.append("- none")

    roots = _expected_roots(spec)
    route_handlers = {route["handler"] for route in routes}
    lines.append("expected roots:")
    http_roots = [root for root in roots if root in route_handlers]
    other_roots = [root for root in roots if root not in route_handlers]
    if http_roots:
        lines.append(f"- fn(request)->response: {', '.join(http_roots)}")
    if other_roots:
        lines.append(f"- fn(?): {', '.join(other_roots)}")
    if not roots:
        lines.append("- none")
    return "\n".join(lines)


def _type_text(type_) -> str:
    if isinstance(type_, str):
        return type_
    constructor, argument = next(iter(type_.items()))
    if constructor == "rec":
        return argument
    if constructor == "result":
        return f"result<{_type_text(argument)},error>"
    return f"{constructor}<{_type_text(argument)}>"


def _auth_text(auth) -> str:
    if auth == "none" or auth is None:
        return "none"
    if isinstance(auth, dict):
        return f"token({auth['token']})"
    return auth


def _expected_roots(spec) -> list[str]:
    names = set(spec.get("effects", {}))
    names.update(route["handler"] for route in spec.get("routes", []))
    for case in spec.get("cases", []):
        calls = case.get("steps", []) if "steps" in case else [case]
        names.update(call["fn"] for call in calls)
    return sorted(names)


def is_effect(name) -> bool:
    """True when `name` is a well-formed effect (`kv.read@notas`, `clock@host`, ...)."""
    return isinstance(name, str) and EFFECT_RE.match(name) is not None


def validate_spec(spec) -> None:
    """Check a spec's shape; a malformed spec raises E_BAD_SPEC at its name."""
    if not isinstance(spec, dict):
        raise GraphError("spec", "E_BAD_SPEC", "spec must be an object")
    where = spec.get("name")
    if not isinstance(where, str) or not where:
        raise GraphError("spec", "E_BAD_SPEC", "spec.name must be a non-empty string")
    atoms = spec.get("atoms")
    if not isinstance(atoms, list):
        raise GraphError(where, "E_BAD_SPEC", "spec.atoms must be a list")
    for atom in atoms:
        if not isinstance(atom, str) or ir.ATOM_RE.match(atom) is None:
            raise GraphError(where, "E_BAD_SPEC", f"invalid atom name {atom!r}")
    types = spec.get("types", [])
    if not isinstance(types, list):
        raise GraphError(where, "E_BAD_SPEC", "spec.types must be a list")
    declared: list[str] = []
    seen_tags: set[str] = set()
    for entry in types:
        _validate_type_decl(where, entry, declared, seen_tags)
    namespaces = _validate_kv(where, spec.get("kv", []), declared)
    tables = normalize_tables(where, spec.get("tables", []), declared, seen_tags)
    normalize_providers(where, spec.get("providers", {}))
    routes = normalize_routes(where, spec.get("routes", []))
    normalize_auth(where, spec.get("auth", {}), routes)
    normalize_rate(where, spec.get("rate"), routes)
    _validate_http_types(where, types, routes)
    _validate_effects(where, spec.get("effects", {}))
    _validate_migrations(where, spec.get("migrations", []), namespaces)
    cases = spec.get("cases")
    if not isinstance(cases, list):
        raise GraphError(where, "E_BAD_SPEC", "spec.cases must be a list")
    seen = set()
    for case in cases:
        _validate_case(where, case, seen, namespaces, {table["name"] for table in tables})


def normalize_routes(where, routes) -> list[dict]:
    """Validate HTTP routes and return their canonical host-facing form."""
    if not isinstance(routes, list):
        raise GraphError(where, "E_BAD_SPEC", "spec.routes must be a list")
    out, seen = [], set()
    for route in routes:
        if not isinstance(route, dict) or set(route) != {
            "method", "path", "handler", "auth", "rate"
        }:
            raise GraphError(
                where,
                "E_BAD_SPEC",
                "each route needs exactly `method`, `path`, `handler`, `auth` and `rate`",
            )
        method = route["method"]
        path = route["path"]
        handler = route["handler"]
        if method not in HTTP_METHODS:
            raise GraphError(where, "E_BAD_SPEC", f"invalid route method {method!r}")
        params = _route_params(where, path)
        if not isinstance(handler, str) or ir.ATOM_RE.fullmatch(handler) is None:
            raise GraphError(where, "E_BAD_SPEC", f"invalid route handler {handler!r}")
        auth = _normalize_route_auth(where, route["auth"])
        if route["rate"] not in (False, True):
            raise GraphError(where, "E_BAD_SPEC", "route.rate must be a boolean")
        shape = (method, tuple("{}" if part.startswith("{") else part for part in path.split("/")))
        if shape in seen:
            raise GraphError(where, "E_BAD_SPEC", f"duplicate or ambiguous route {method} {path}")
        seen.add(shape)
        out.append({**route, "method": method, "params": params, "auth": auth})
    return out


def _route_params(where, path) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path:
        raise GraphError(where, "E_BAD_SPEC", f"invalid route path {path!r}")
    params, seen = [], set()
    for segment in path.split("/")[1:]:
        if not segment:
            raise GraphError(where, "E_BAD_SPEC", f"invalid route path {path!r}")
        if segment.startswith("{") or segment.endswith("}"):
            if not (segment.startswith("{") and segment.endswith("}")):
                raise GraphError(where, "E_BAD_SPEC", f"invalid route parameter in {path!r}")
            name = segment[1:-1]
            if ir.ATOM_RE.fullmatch(name or "") is None or name in seen:
                raise GraphError(where, "E_BAD_SPEC", f"invalid route parameter {name!r}")
            seen.add(name)
            params.append(name)
    return params


def _normalize_route_auth(where, auth) -> str | None:
    if auth == "none":
        return None
    if isinstance(auth, str) and auth.startswith("token(") and auth.endswith(")"):
        scope = auth[6:-1]
    elif isinstance(auth, dict) and set(auth) == {"token"}:
        scope = auth["token"]
    else:
        raise GraphError(where, "E_BAD_SPEC", "route.auth must be 'none' or token(scope)")
    if scope not in HTTP_SCOPES:
        raise GraphError(where, "E_BAD_SPEC", f"invalid auth scope {scope!r}")
    return scope


def normalize_auth(where, auth, routes) -> dict[str, dict]:
    """Validate the immutable bearer-token table owned by the human spec."""
    if not isinstance(auth, dict):
        raise GraphError(where, "E_BAD_SPEC", "spec.auth must be an object")
    out = {}
    for token, session in auth.items():
        if not isinstance(token, str) or not token:
            raise GraphError(where, "E_BAD_SPEC", "auth tokens must be non-empty strings")
        if not isinstance(session, dict) or set(session) != {"subject", "scope"}:
            raise GraphError(where, "E_BAD_SPEC", f"auth token {token!r} needs subject and scope")
        subject, scope = session["subject"], session["scope"]
        if not isinstance(subject, str) or not subject or scope not in HTTP_SCOPES:
            raise GraphError(where, "E_BAD_SPEC", f"invalid auth session for token {token!r}")
        out[token] = {"subject": subject, "scope": scope}
    if any(route["auth"] is not None for route in routes) and not out:
        raise GraphError(where, "E_BAD_SPEC", "authenticated routes require spec.auth tokens")
    return out


def normalize_rate(where, rate, routes) -> dict | None:
    """Validate the single sliding-window policy shared by rate-limited routes."""
    if rate is None:
        if any(route["rate"] for route in routes):
            raise GraphError(where, "E_BAD_SPEC", "rate-limited routes require spec.rate")
        return None
    if not isinstance(rate, dict) or set(rate) != {"limit", "window_ms", "key"}:
        raise GraphError(where, "E_BAD_SPEC", "spec.rate needs limit, window_ms and key")
    limit, window = rate["limit"], rate["window_ms"]
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
        or not isinstance(window, int)
        or isinstance(window, bool)
        or window <= 0
        or rate["key"] != "subject"
    ):
        raise GraphError(
            where, "E_BAD_SPEC", "rate needs positive integers and key must be 'subject'"
        )
    return dict(rate)


def _validate_http_types(where, types, routes) -> None:
    if not routes:
        return
    by_tag = {entry["tag"]: entry for entry in types}
    request = by_tag.get("request")
    response = by_tag.get("response")
    if request is None or response is None:
        raise GraphError(where, "E_BAD_SPEC", "routes require nominal request and response types")
    request_fields = {field["name"]: field["type"] for field in request["fields"]}
    response_fields = {field["name"]: field["type"] for field in response["fields"]}
    if list(request_fields) != ["method", "path", "params", "query", "body", "subject"]:
        raise GraphError(
            where,
            "E_BAD_SPEC",
            "request fields must be method, path, params, query, body, subject in that order",
        )
    if request_fields["method"] != "string" or request_fields["path"] != "string":
        raise GraphError(where, "E_BAD_SPEC", "request method and path must be strings")
    if request_fields["subject"] != "string" or any(
        not isinstance(request_fields[name], dict) or set(request_fields[name]) != {"rec"}
        for name in ("params", "query", "body")
    ):
        raise GraphError(
            where, "E_BAD_SPEC", "request params/query/body must be records and subject a string"
        )
    if list(response_fields) != ["status", "body", "retry_after_ms"]:
        raise GraphError(
            where,
            "E_BAD_SPEC",
            "response fields must be status, body, retry_after_ms in that order",
        )
    if (
        response_fields["status"] != "i64"
        or not isinstance(response_fields["body"], dict)
        or set(response_fields["body"]) != {"rec"}
        or response_fields["retry_after_ms"] != {"option": "i64"}
    ):
        raise GraphError(
            where, "E_BAD_SPEC", "response needs i64 status, record body and option<i64> retry"
        )


def _validate_kv(where, kv, declared) -> set:
    """Validate the namespace declarations; returns their names."""
    if not isinstance(kv, list):
        raise GraphError(where, "E_BAD_SPEC", "spec.kv must be a list")
    names = set()
    for entry in kv:
        if not isinstance(entry, dict) or set(entry) not in (
            {"ns", "type"},
            {"ns", "type", "commit"},
        ):
            raise GraphError(where, "E_BAD_SPEC", "each namespace needs `ns` and `type`")
        ns = entry["ns"]
        if not isinstance(ns, str) or ir.ATOM_RE.match(ns) is None:
            raise GraphError(where, "E_BAD_SPEC", f"invalid namespace {ns!r}")
        if ns in names:
            raise GraphError(where, "E_BAD_SPEC", f"duplicate namespace {ns!r}")
        if "commit" in entry and entry["commit"] != "on_success":
            raise GraphError(
                where, "E_BAD_SPEC", "namespace `commit` may only be 'on_success'"
            )
        _validate_spec_type(where, entry["type"], declared, 0)
        names.add(ns)
    return names


def normalize_providers(where, providers) -> dict[str, list[str]]:
    """Validate and canonicalize provider grants as ``{provider: [operation, ...]}``."""
    from .providers import CONTRACTS

    if isinstance(providers, list):
        providers = {
            name: sorted(CONTRACTS[name].operations) if name in CONTRACTS else []
            for name in providers
        }
    if not isinstance(providers, dict):
        raise GraphError(where, "E_BAD_SPEC", "spec.providers must be an object or list")
    out = {}
    for name, operations in providers.items():
        contract = CONTRACTS.get(name)
        if contract is None:
            raise GraphError(where, "E_BAD_SPEC", f"unknown provider {name!r}")
        if operations == "all":
            operations = list(contract.operations)
        if not isinstance(operations, list) or any(not isinstance(op, str) for op in operations):
            raise GraphError(where, "E_BAD_SPEC", f"provider {name!r} operations must be a list")
        unknown = sorted(set(operations) - set(contract.operations))
        if unknown:
            raise GraphError(where, "E_BAD_SPEC", f"provider {name!r} has unknown ops {unknown}")
        out[name] = sorted(set(operations))
    return out


def normalize_tables(where, tables, declared, seen_tags) -> list[dict]:
    """Validate relational schemas and return a stable host-facing representation."""
    if not isinstance(tables, list):
        raise GraphError(where, "E_BAD_SPEC", "spec.tables must be a list")
    out, names = [], set()
    for raw in tables:
        if not isinstance(raw, dict):
            raise GraphError(where, "E_BAD_SPEC", "each table must be an object")
        allowed = {"name", "columns", "primary_key", "primary", "refs", "unique"}
        if set(raw) - allowed or "name" not in raw or "columns" not in raw:
            raise GraphError(
                where, "E_BAD_SPEC", "each table needs `name`, `columns` and `primary_key`"
            )
        name = raw["name"]
        if not isinstance(name, str) or ir.ATOM_RE.fullmatch(name) is None:
            raise GraphError(where, "E_BAD_SPEC", f"invalid table name {name!r}")
        if name in names or name in seen_tags:
            raise GraphError(where, "E_BAD_SPEC", f"duplicate table/type name {name!r}")
        names.add(name)
        columns = _normalize_columns(where, name, raw["columns"], declared)
        column_names = {column["name"] for column in columns}
        primary = raw.get("primary_key", raw.get("primary"))
        primary_columns = [primary] if isinstance(primary, str) else primary
        if (
            not isinstance(primary_columns, list)
            or not primary_columns
            or len(set(primary_columns)) != len(primary_columns)
            or any(column not in column_names for column in primary_columns)
        ):
            raise GraphError(
                where, "E_BAD_SPEC", f"table {name!r}: invalid primary_key {primary!r}"
            )
        unique = _normalize_unique(where, name, raw.get("unique", []), column_names)
        refs = _normalize_refs(where, name, raw.get("refs", []), column_names)
        out.append(
            {
                "name": name,
                "columns": columns,
                "primary_key": primary if isinstance(primary, str) else list(primary),
                "primary_name": f"{name}_pkey",
                "unique": unique,
                "refs": refs,
            }
        )
    known = {table["name"]: table for table in out}
    for table in out:
        for ref in table["refs"]:
            target = known.get(ref["table"])
            if target is None or ref["ref_column"] not in {
                column["name"] for column in target["columns"]
            }:
                raise GraphError(
                    where,
                    "E_BAD_SPEC",
                    f"table {table['name']!r}: ref {ref['name']!r} has an unknown target",
                )
    return out


def _normalize_columns(where, table, columns, declared):
    if isinstance(columns, dict):
        columns = [{"name": name, "type": type_} for name, type_ in columns.items()]
    if not isinstance(columns, list) or not columns:
        raise GraphError(where, "E_BAD_SPEC", f"table {table!r}: columns must be non-empty")
    out, seen = [], set()
    for column in columns:
        if not isinstance(column, dict) or set(column) != {"name", "type"}:
            raise GraphError(where, "E_BAD_SPEC", f"table {table!r}: invalid column")
        name = column["name"]
        if not isinstance(name, str) or ir.ATOM_RE.fullmatch(name) is None or name in seen:
            raise GraphError(where, "E_BAD_SPEC", f"table {table!r}: invalid column {name!r}")
        _validate_spec_type(where, column["type"], declared, 0)
        seen.add(name)
        out.append({"name": name, "type": column["type"]})
    return out


def _normalize_unique(where, table, unique, columns):
    if not isinstance(unique, list):
        raise GraphError(where, "E_BAD_SPEC", f"table {table!r}: unique must be a list")
    out = []
    for item in unique:
        if isinstance(item, str):
            item = {"name": f"{table}_{item}_key", "columns": [item]}
        if not isinstance(item, dict) or set(item) != {"name", "columns"}:
            raise GraphError(where, "E_BAD_SPEC", f"table {table!r}: invalid unique constraint")
        cols = item["columns"]
        if not isinstance(cols, list) or not cols or any(col not in columns for col in cols):
            raise GraphError(where, "E_BAD_SPEC", f"table {table!r}: invalid unique columns")
        out.append({"name": item["name"], "columns": cols})
    return out


def _normalize_refs(where, table, refs, columns):
    if isinstance(refs, dict):
        refs = [{"column": column, "target": target} for column, target in refs.items()]
    if not isinstance(refs, list):
        raise GraphError(where, "E_BAD_SPEC", f"table {table!r}: refs must be a list")
    out = []
    for item in refs:
        if not isinstance(item, dict):
            raise GraphError(where, "E_BAD_SPEC", f"table {table!r}: invalid ref")
        column = item.get("column") or (item.get("columns") or [None])[0]
        target = item.get("target")
        target_table = item.get("table")
        target_column = item.get("ref_column") or (item.get("ref_columns") or [None])[0]
        if isinstance(target, str) and "." in target:
            target_table, target_column = target.split(".", 1)
        name = item.get("name") or f"{table}_{column}_fkey"
        if column not in columns or not all(
            isinstance(value, str) and value for value in (name, target_table, target_column)
        ):
            raise GraphError(where, "E_BAD_SPEC", f"table {table!r}: invalid ref")
        out.append(
            {"name": name, "column": column, "table": target_table, "ref_column": target_column}
        )
    return out


def _validate_effects(where, effects) -> None:
    """Validate `{"effects": {name: [effect, ...]}}` (ADR-0009)."""
    if not isinstance(effects, dict):
        raise GraphError(where, "E_BAD_SPEC", "spec.effects must be an object")
    for name, caps in effects.items():
        if not isinstance(name, str) or not name:
            raise GraphError(where, "E_BAD_SPEC", f"invalid effect bound name {name!r}")
        if not isinstance(caps, list):
            raise GraphError(where, "E_BAD_SPEC", f"effect bound {name!r} must be a list")
        for effect in caps:
            if not is_effect(effect):
                raise GraphError(
                    where, "E_BAD_SPEC", f"invalid effect {effect!r} in bound {name!r}"
                )


def _validate_migrations(where, migrations, namespaces) -> None:
    """Validate `{"migrations": [{"ns", "fn"}]}` (ADR-0009).

    Each migration names a namespace this spec declares in its own `kv` (the one
    whose type is changing) and the pure function that migrates its values.
    """
    if not isinstance(migrations, list):
        raise GraphError(where, "E_BAD_SPEC", "spec.migrations must be a list")
    seen = set()
    for entry in migrations:
        if not isinstance(entry, dict) or set(entry) != {"ns", "fn"}:
            raise GraphError(where, "E_BAD_SPEC", "each migration needs `ns` and `fn`")
        ns = entry["ns"]
        if not isinstance(ns, str) or ir.ATOM_RE.match(ns) is None:
            raise GraphError(where, "E_BAD_SPEC", f"invalid migration namespace {ns!r}")
        if ns not in namespaces:
            raise GraphError(
                where, "E_BAD_SPEC", f"migration namespace {ns!r} is not declared in `kv`"
            )
        if ns in seen:
            raise GraphError(where, "E_BAD_SPEC", f"duplicate migration for {ns!r}")
        seen.add(ns)
        if not isinstance(entry["fn"], str) or not entry["fn"]:
            raise GraphError(where, "E_BAD_SPEC", "migration `fn` must be a non-empty string")


def _validate_type_decl(where, entry, declared, seen_tags) -> None:
    """Validate one `{"tag", "fields"}` declaration, in declaration order."""
    if (
        not isinstance(entry, dict)
        or "tag" not in entry
        or "fields" not in entry
        or set(entry) != {"tag", "fields"}
    ):
        raise GraphError(where, "E_BAD_SPEC", "each type needs `tag` and `fields`")
    tag = entry["tag"]
    if not isinstance(tag, str) or ir.ATOM_RE.match(tag) is None:
        raise GraphError(where, "E_BAD_SPEC", f"invalid type tag {tag!r}")
    if tag in seen_tags:
        raise GraphError(where, "E_BAD_SPEC", f"duplicate type tag {tag!r}")
    fields = entry["fields"]
    if not isinstance(fields, list) or not 1 <= len(fields) <= ir.MAX_ARITY:
        raise GraphError(
            where, "E_BAD_SPEC", f"type {tag!r}: fields must be a list of 1..{ir.MAX_ARITY}"
        )
    names = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) != {"name", "type"}:
            raise GraphError(where, "E_BAD_SPEC", f"type {tag!r}: each field needs name and type")
        name = field["name"]
        if not isinstance(name, str) or not name:
            raise GraphError(where, "E_BAD_SPEC", f"type {tag!r}: field name must be non-empty")
        if name in names:
            raise GraphError(where, "E_BAD_SPEC", f"type {tag!r}: duplicate field name {name!r}")
        names.add(name)
        _validate_spec_type(where, field["type"], declared, 0)
    seen_tags.add(tag)
    declared.append(tag)


def _validate_spec_type(where, t, declared, depth) -> None:
    """A spec field type: a primitive, option/result/list of one, or `{"rec": earlier_tag}`."""
    if depth > ir.MAX_TYPE_DEPTH:
        raise GraphError(where, "E_BAD_SPEC", f"type nesting exceeds {ir.MAX_TYPE_DEPTH}")
    if isinstance(t, str) and t in ir.TYPE_CODE:
        return
    if isinstance(t, dict) and len(t) == 1:
        ((ctor, arg),) = t.items()
        if ctor in ("option", "result", "list"):
            _validate_spec_type(where, arg, declared, depth + 1)
            return
        if ctor == "rec":
            if not isinstance(arg, str) or arg not in declared:
                raise GraphError(
                    where, "E_BAD_SPEC", f"{{'rec': {arg!r}}} must name a type declared earlier"
                )
            return
    raise GraphError(where, "E_BAD_SPEC", f"invalid field type {t!r}")


def _validate_case(where, case, seen, namespaces, tables=None) -> None:
    if not isinstance(case, dict):
        raise GraphError(where, "E_BAD_SPEC", "case must be an object")
    cid = case.get("id")
    if not isinstance(cid, str) or not cid:
        raise GraphError(where, "E_BAD_SPEC", "case.id must be a non-empty string")
    if cid in seen:
        raise GraphError(where, "E_BAD_SPEC", f"duplicate case id {cid!r}")
    seen.add(cid)
    if case.get("origin") not in ORIGINS:
        raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: origin must be 'human' or 'agent'")
    has_fn, has_steps = "fn" in case, "steps" in case
    if has_fn and has_steps:
        raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: use `fn` or `steps`, not both")
    if not has_fn and not has_steps:
        raise GraphError(
            where, "E_BAD_SPEC", f"case {cid!r}: needs `fn`/`args`/`expect` or `steps`"
        )
    if has_fn:
        _validate_call(where, f"case {cid!r}", case)
        _validate_expect(where, cid, case.get("expect"))
    else:
        _validate_steps(where, cid, case["steps"])
    if "clock" in case and not _is_int(case["clock"]):
        raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: clock must be milliseconds")
    for key in ("state", "state_after"):
        if key in case:
            _validate_state(where, cid, key, case[key], namespaces)
    for key in ("table_state", "tables_after"):
        if key in case:
            _validate_table_state(where, cid, key, case[key], tables or set())


def _validate_table_state(where, cid, key, state, tables):
    if not isinstance(state, dict):
        raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: {key} must be an object")
    for table, rows in state.items():
        if table not in tables:
            raise GraphError(
                where, "E_BAD_SPEC", f"case {cid!r}: {key} uses undeclared table {table!r}"
            )
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: {key}.{table} must be a row list")


def _validate_call(where, label, call) -> None:
    if not isinstance(call.get("fn"), str) or not call["fn"]:
        raise GraphError(where, "E_BAD_SPEC", f"{label}: fn must be a non-empty string")
    if not isinstance(call.get("args"), list):
        raise GraphError(where, "E_BAD_SPEC", f"{label}: args must be a list")


def _validate_steps(where, cid, steps) -> None:
    if not isinstance(steps, list) or not steps:
        raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: steps must be a non-empty list")
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: steps[{i}] must be an object")
        _validate_call(where, f"case {cid!r} steps[{i}]", step)
        _validate_expect(where, cid, step.get("expect"))
        if "clock" in step and not _is_int(step["clock"]):
            raise GraphError(
                where, "E_BAD_SPEC", f"case {cid!r}: steps[{i}].clock must be milliseconds"
            )


def _validate_expect(where, cid, expect) -> None:
    if not isinstance(expect, dict) or set(expect) not in ({"result"}, {"error"}):
        raise GraphError(
            where,
            "E_BAD_SPEC",
            f"case {cid!r}: expect must be {{'result': X}} or {{'error': 'E_CODE'}}",
        )
    if "error" in expect and (not isinstance(expect["error"], str) or not expect["error"]):
        raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: expect.error must be an error code")


def _validate_state(where, cid, key, state, namespaces) -> None:
    if not isinstance(state, dict):
        raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: {key} must be an object")
    for ns, entries in state.items():
        if not isinstance(ns, str) or ir.ATOM_RE.match(ns) is None:
            raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: {key} has an invalid namespace")
        if ns not in namespaces:
            raise GraphError(
                where, "E_BAD_SPEC", f"case {cid!r}: {key} uses undeclared namespace {ns!r}"
            )
        if not isinstance(entries, dict):
            raise GraphError(where, "E_BAD_SPEC", f"case {cid!r}: {key}.{ns} must be an object")
        for k in entries:
            if not isinstance(k, str):
                raise GraphError(
                    where, "E_BAD_SPEC", f"case {cid!r}: {key}.{ns} keys must be strings"
                )


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def spec_type_errors(store, spec) -> list:
    """Steps of `spec` that no longer fit the graph, as [(case_id, index, detail)].

    ADR-0009: the function must exist and have the arity and parameter types the
    step's `args` need; an `expect.result` must fit the return type; and the case's
    `state`/`state_after` must fit their namespaces' types. This is a "the spec went
    stale" signal, not a logic failure.
    """
    validate_spec(spec)
    namespaces = store.kv_namespaces()
    out = []
    for case in spec["cases"]:
        for index, step in enumerate(_case_steps(case)):
            detail = _step_type_error(store, namespaces, case, step)
            if detail is not None:
                out.append((case["id"], index, detail))
    return out


def _step_type_error(store, namespaces, case, step):
    """Why `step` no longer fits the graph, or None when it does."""
    ref = step["fn"]
    try:
        target = store.resolve(ref)
    except GraphError:
        return f"fn {ref!r} is not defined (the spec calls it with args {step['args']!r})"
    node = store.get(target)
    if node is None or node.get("k") != "fn":
        return f"{ref!r} is not a function"
    params, ret = node["params"], node["ret"]
    args = step["args"]
    if len(args) != len(params):
        return f"{ref!r} takes {len(params)} args, got {len(args)}"
    for i, (arg, want) in enumerate(zip(args, params, strict=True)):
        reason = _not_encodable(store, want, arg)
        if reason:
            return f"args[{i}] does not fit {typecheck.render_type(want, store)}: {reason}"
    expect = step["expect"]
    if "result" in expect:
        success = _success_type(ret)
        reason = _not_encodable(store, success, expect["result"])
        if reason:
            return f"result does not fit {typecheck.render_type(success, store)}: {reason}"
    for key in ("state", "state_after"):
        if key in case:
            detail = _state_type_error(store, namespaces, key, case[key])
            if detail is not None:
                return detail
    for key in ("table_state", "tables_after"):
        if key in case:
            detail = _table_state_type_error(store, key, case[key])
            if detail is not None:
                return detail
    return None


def _state_type_error(store, namespaces, key, state):
    """Why a case's `state`/`state_after` no longer fits its namespaces, or None."""
    for ns, entries in state.items():
        want = namespaces.get(ns)
        if want is None:
            return f"{key}.{ns} is not a declared namespace"
        for name, value in entries.items():
            reason = _not_encodable(store, want, value)
            if reason:
                return (
                    f"{key}.{ns}[{name!r}] does not fit "
                    f"{typecheck.render_type(want, store)}: {reason}"
                )
    return None


def _table_state_type_error(store, key, state):
    for table, rows in state.items():
        schema = store.table_schema(table)
        if schema is None:
            return f"{key}.{table} is not a declared table"
        want = {"rec": schema["decl"]}
        for index, row in enumerate(rows):
            reason = _not_encodable(store, want, row)
            if reason:
                return f"{key}.{table}[{index}] does not fit its row type: {reason}"
    return None


def _not_encodable(store, want, value):
    """The reason a boundary JSON `value` does not fit `want`, or None when it does.

    Delegates to `values.is_encodable` (ADR-0009). That helper is optional: if it
    is not present the check is skipped rather than crashing the runner.
    """
    is_encodable = getattr(values, "is_encodable", None)
    if is_encodable is None:
        return None
    return is_encodable(store.get, want, value)


def run_cases(run, spec: dict, *, state=None, store=None) -> dict:
    """Run every case of a spec through `run` and summarize the outcome.

    `run` returns `{"ok": True, "result": X}` or `{"ok": False, "error": {...}}`
    (`Service.run`); a step that defines a `clock` is called as `run(ref, args,
    clock=ms)`. A case passes when every step's `expect` matches the observed
    outcome strictly and, when a state runner is given, the final `snapshot()`
    matches `state_after`. The summary counts human and agent cases separately,
    and every case is reported regardless of the others failing.

    With `store`, each step is first checked against the graph (ADR-0009): a step
    that no longer fits its function signature fails with `E_SPEC_TYPE` (and its
    detail) instead of being executed.
    """
    validate_spec(spec)
    stale = {}
    if store is not None:
        for cid, index, detail in spec_type_errors(store, spec):
            stale.setdefault((cid, index), detail)
    cases = []
    passed = failed = human_passed = human_total = 0
    for case in spec["cases"]:
        if state is not None:
            state.reset(case.get("state") or {})
            if hasattr(state, "reset_tables"):
                state.reset_tables(case.get("table_state") or {})
        steps = []
        ok = True
        for index, step in enumerate(_case_steps(case)):
            expected = step["expect"]
            detail = stale.get((case["id"], index))
            if detail is None:
                actual = _outcome(run, step["fn"], step["args"], step.get("clock", _UNSET))
                if _unwraps_result(store, step["fn"]):
                    actual = _unwrap(actual)
            else:
                actual = {"error": "E_SPEC_TYPE", "detail": detail}
            step_ok = _same(expected, actual)
            ok = ok and step_ok
            steps.append(
                {
                    "fn": step["fn"],
                    "args": step["args"],
                    "expected": expected,
                    "actual": actual,
                    "status": "pass" if step_ok else "fail",
                }
            )
        entry = {
            "id": case["id"],
            "origin": case["origin"],
            "status": "pass" if ok else "fail",
            "steps": steps,
        }
        if len(steps) == 1:
            entry["expected"] = steps[0]["expected"]
            entry["actual"] = steps[0]["actual"]
        if state is not None and "state_after" in case:
            state_expected = case["state_after"]
            state_actual = state.snapshot()
            if not _same(_non_empty(state_expected), _non_empty(state_actual)):
                ok = False
                entry["state_expected"] = state_expected
                entry["state_actual"] = state_actual
            entry["status"] = "pass" if ok else "fail"
        if state is not None and "tables_after" in case:
            tables_expected = case["tables_after"]
            tables_actual = state.snapshot_tables()
            if not _same(_non_empty(tables_expected), _non_empty(tables_actual)):
                ok = False
                entry["tables_expected"] = tables_expected
                entry["tables_actual"] = tables_actual
            entry["status"] = "pass" if ok else "fail"
        if ok:
            passed += 1
        else:
            failed += 1
        if case["origin"] == "human":
            human_total += 1
            if ok:
                human_passed += 1
        cases.append(entry)
    return {
        "spec": spec["name"],
        "passed": passed,
        "failed": failed,
        "human_passed": human_passed,
        "human_total": human_total,
        "cases": cases,
    }


def _success_type(ret):
    """The type a spec's `expect.result` is checked against.

    A root returning `result<T, error>` is observed through its frontier: `ok(v)` is
    the result `v` (of type T) and `err(e)` is the error `e.code`.
    """
    if isinstance(ret, dict) and "result" in ret:
        return ret["result"]
    return ret


def _unwraps_result(store, ref) -> bool:
    """True when the function `ref` returns `result<T, error>`."""
    if store is None:
        return False
    try:
        node = store.get(store.resolve(ref))
    except GraphError:
        return False
    ret = node.get("ret") if node else None
    return isinstance(ret, dict) and "result" in ret


def _unwrap(outcome: dict) -> dict:
    """Turn a tagged `result` value into the spec's `{"result": v}` / `{"error": code}`."""
    value = outcome.get("result")
    if not isinstance(value, dict):
        return outcome
    if value.get("tag") == "ok":
        return {"result": value.get("v")}
    if value.get("tag") == "err":
        error = value.get("v")
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, dict):
            code = code.get("atom")
        return {"error": code}
    return outcome


def _non_empty(state) -> dict:
    """A KV state without its empty namespaces: `{"ns": {}}` and `{}` are the same state."""
    return {ns: rows for ns, rows in state.items() if rows}


def _case_steps(case) -> list:
    """The case as a list of {fn, args, expect, [clock]} steps (short or `steps` form)."""
    if "steps" in case:
        return list(case["steps"])
    step = {"fn": case["fn"], "args": case["args"], "expect": case["expect"]}
    if "clock" in case:
        step["clock"] = case["clock"]
    return [step]


def _outcome(run, ref, args, clock=_UNSET) -> dict:
    """Normalize a `run` result to `{"result": X}` or `{"error": code}`."""
    try:
        if clock is _UNSET:
            result = run(ref, args)
        else:
            result = run(ref, args, clock=clock)
    except GraphError as error:
        return {"error": error.info["code"]}
    except Exception:  # a broken runner must not abort the whole spec
        return {"error": "E_TRAP"}
    if not isinstance(result, dict):
        return {"error": "E_TRAP"}
    if result.get("ok") is True:
        return {"result": result.get("result")}
    if result.get("ok") is False:
        error = result.get("error")
        return {"error": error.get("code") if isinstance(error, dict) else None}
    return {"error": "E_TRAP"}


def _same(a, b) -> bool:
    """Strict structural equality: a `bool` is never equal to an `int`."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return False
    return type(a) is type(b) and a == b
