"""Compile `historias.md` to an AXON spec (docs/requisitos-historias.md v3, section 5.1).

Rules 1-8 of §5.1 are applied in order; anything they do not decide stops with a Spanish
`StoryError` at the offending line. The result is checked with `validate_spec`, and a
rejection is reported at the story's first line with the validator's message.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from ..ir import GraphError
from ..spec import validate_spec
from .parse import STATUSES, Doc, Given, Story, StoryError

_PARAM = re.compile(r"\{([^}]+)\}")


def _declared(line: int, message: str) -> StoryError:
    return StoryError(line, f"no decidido por §5.1: {message}")


# --- rules 4 and 5: route, query and body ----------------------------------------------------


def _split_query(raw: str, line: int) -> tuple[str, list[tuple[str, str, int]]]:
    """Rule 4: `/hello?name=Ada` -> path `/hello` and query field `name` = "Ada"."""
    if "?" not in raw:
        return raw, []
    path, query = raw.split("?", 1)
    params = []
    for pair in query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        if not key:
            raise _declared(line, f"parámetro de consulta sin nombre en «{raw}»")
        params.append((key, unquote(value.replace("+", " ")), line))
    return path, params


def _route_path(raw: str, line: int) -> tuple[str, list[str]]:
    """Rule 4: a numeric segment (`/authors/1`) is declared as `{id}`."""
    if not raw.startswith("/") or raw == "/":
        raise StoryError(line, f"ruta inválida «{raw}»")
    segments = raw.split("/")[1:]
    parts, params = [], []
    for index, segment in enumerate(segments):
        if re.fullmatch(r"\d+", segment):
            name = "id" if index + 1 == len(segments) else f"id{index + 1}"
            params.append(name)
            parts.append("{" + name + "}")
        elif _PARAM.fullmatch(segment):
            name = _PARAM.fullmatch(segment).group(1)
            params.append(name)
            parts.append(segment)
        elif segment:
            parts.append(segment)
        else:
            raise StoryError(line, f"ruta inválida «{raw}»")
    return "/" + "/".join(parts), params


# --- rules 5 and 6: field types --------------------------------------------------------------


def _value_type(value):
    """`str` -> string, a whole number -> i64, a boolean -> bool; anything else is not §5.1."""
    if isinstance(value, bool):
        return "bool"  # §5.1 has no boolean type; the caller stops on it
    if isinstance(value, float):
        return "float"  # §5.1 has no decimal type; the caller stops on it
    if isinstance(value, int):
        return "i64"
    if isinstance(value, str):
        return "string"
    return None


def _field_table(observations, kind: str, *, bodies) -> list[tuple[str, dict]]:
    """Merge `(campo, valor, línea)` into ordered fields (rules 5 and 6).

    `bodies` is the list of response bodies (or None when there is no per-body rule): a key
    missing from at least one of them is `option<…>`, a key in all of them keeps its type.
    """
    order: list[str] = []
    types: dict[str, str] = {}
    present: dict[str, int] = {}
    for name, value, line in observations:
        found = _value_type(value)
        if found is None:
            raise _declared(line, f"el campo «{name}» de {kind} no es texto ni número entero")
        if found == "bool":
            raise _declared(line, f"el campo «{name}» de {kind} es booleano y §5.1 no define bool")
        if found == "float":
            raise _declared(
                line, f"el campo «{name}» de {kind} es decimal y §5.1 no define decimales"
            )
        if name in types and types[name] != found:
            raise _declared(line, f"el campo «{name}» de {kind} aparece como texto y como número")
        types.setdefault(name, found)
        if name not in order:
            order.append(name)
    if bodies is None:
        return [(name, {"option": types[name]}) for name in order]
    for body in bodies:
        for name in body:
            present[name] = present.get(name, 0) + 1
    out = []
    for name in order:
        if present.get(name, 0) < len(bodies):
            out.append((name, {"option": types[name]}))
        else:
            out.append((name, types[name]))
    return out


def _check_given(given: Given, declared, case_id: str, story_id: str) -> None:
    """Rules 5: a `Dado` object must fit its `Cosas` declaration, required fields included."""
    if declared is None:
        raise _declared(
            given.line,
            f"el caso «{case_id}» de la historia «{story_id}» usa la cosa «{given.thing}» "
            "en «Dado», pero no está declarada en «Cosas» de ninguna historia",
        )
    known = {entry[0]: entry[1] for entry in declared}
    for name, value in given.fields.items():
        if name not in known:
            raise StoryError(given.line, f"la cosa «{given.thing}» no tiene el campo «{name}»")
        found = _value_type(value)
        if found is None:
            raise _declared(
                given.line, f"el campo «{name}» de «{given.thing}» no es texto ni número entero"
            )
        if found not in ("string", "i64"):
            raise _declared(
                given.line, f"el campo «{name}» de «{given.thing}» es {found} y §5.1 no lo define"
            )
        if found != known[name]:
            raise StoryError(
                given.line, f"el campo «{name}» de «{given.thing}» debe ser {known[name]}"
            )
    for name, _, required, *_ in declared:
        if required and name not in given.fields:
            raise _declared(
                given.line, f"falta el campo obligatorio «{name}» de «{given.thing}» en «Dado»"
            )


def _created_body(case, declared) -> list[tuple[str, dict]]:
    """Validate one creation body and return its declaration-derived request fields."""
    if declared is None:
        raise StoryError(
            case.line, f"la cosa «{case.creates}» de «Crea» no está declarada en «Cosas»"
        )
    body = case.request.body or {}
    known = {entry[0]: entry[1] for entry in declared}
    for name, value in body.items():
        if name not in known:
            raise StoryError(case.line, f"la cosa «{case.creates}» no tiene el campo «{name}»")
        found = _value_type(value)
        if found != known[name]:
            raise StoryError(
                case.line, f"el campo «{name}» de «{case.creates}» debe ser {known[name]}"
            )
    if 200 <= case.response.status < 300:
        for name, _, required, *_ in declared:
            if required and name not in body:
                raise StoryError(
                    case.line,
                    f"falta el campo obligatorio «{name}» de «{case.creates}» en «Cuando»",
                )
    return [
        (name, kind if required else {"option": kind})
        for name, kind, required, *_ in declared
    ]


def _is_option(kind) -> bool:
    return isinstance(kind, dict) and set(kind) == {"option"}


def _body_fields(created, observed, *, every_route_has_body: bool) -> list[tuple[str, dict]]:
    """The request body record: direct types only when every route carries a body.

    `request_body` is one record shared by every route, and the HTTP host coerces it for each
    request. A required field would turn every bodyless request (`GET` with `{}`) into a 422
    before the handler runs, so a spec with any `GET`/`DELETE` route declares every body field
    as `option<T>`: the handler itself answers the 422 of the story's `[error]` case.
    """
    fields = _merge_body_fields(created, observed)
    if every_route_has_body:
        return fields
    return [(name, kind if _is_option(kind) else {"option": kind}) for name, kind in fields]


def _merge_body_fields(created, observed) -> list[tuple[str, dict]]:
    """Keep declared creation fields authoritative, adding unrelated observed fields as options."""
    out: list[tuple[str, dict]] = []
    types: dict[str, dict | str] = {}
    for name, kind, line in created:
        if name in types and types[name] != kind:
            raise _declared(line, f"el campo «{name}» de cuerpo tiene declaraciones incompatibles")
        if name not in types:
            out.append((name, kind))
            types[name] = kind
    for name, kind in observed:
        plain = kind.get("option") if isinstance(kind, dict) else kind
        declared = types.get(name)
        declared_plain = declared.get("option") if isinstance(declared, dict) else declared
        if declared is not None and declared_plain != plain:
            raise _declared(1, f"el campo «{name}» de cuerpo aparece con tipos incompatibles")
        if declared is None:
            out.append((name, kind))
            types[name] = kind
    return out


# --- rule 7: handler names -------------------------------------------------------------------


def _handler_name(path: str) -> str:
    name = _PARAM.sub(lambda match: "by_" + match.group(1), path.strip("/")).replace("/", "_")
    name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    if not name:
        raise StoryError(1, f"no se puede derivar el handler de «{path}»")
    if name[0].isdigit():
        name = f"r{name}"
    return name


# --- the compiler ----------------------------------------------------------------------------


# Section 5.1 rule 3, "convención de marcadores": a record needs at least one field
# (`validate_spec`), so a category with no observed field gets a fixed, arbitrary name.
# Nobody reads it; the A1.3 behavioural test proves the marker does not change the API.
MARKERS = {
    "params": {"name": "id", "type": {"option": "i64"}},
    "query": {"name": "name", "type": {"option": "string"}},
    "body": {"name": "name", "type": {"option": "string"}},
}


def _with_placeholder(kind: str | None, fields: list[tuple[str, dict]], marker=None) -> list[dict]:
    """The observed fields, or `marker` (the marker of `kind`) when the category observed none.

    `response_body` is not a §5.1 category: rule 6 declares its keys, and 11.1 always has one,
    so it passes its own marker (the query one) only to keep the record non-empty.
    """
    if not fields:
        return [dict(marker or MARKERS[kind])]
    return [{"name": n, "type": t} for n, t in fields]


def _types(params, query_fields, body_fields, response_fields, item_fields=None) -> list[dict]:
    """Rule 3: the five records in the order `tools/t7/axon_spec.json` uses.

    §5.1 fixes the `request`/`response` shapes but not the declaration order; the reference
    order is params, query, body, request, response_body, response.
    """
    return [
        {"tag": "request_params", "fields": [{"name": n, "type": {"option": "i64"}} for n in params]
         or [dict(MARKERS["params"])]},
        {"tag": "request_query", "fields": _with_placeholder("query", query_fields)},
        {"tag": "request_body", "fields": _with_placeholder("body", body_fields)},
        {
            "tag": "request",
            "fields": [
                {"name": "method", "type": "string"},
                {"name": "path", "type": "string"},
                {"name": "params", "type": {"rec": "request_params"}},
                {"name": "query", "type": {"rec": "request_query"}},
                {"name": "body", "type": {"rec": "request_body"}},
                {"name": "subject", "type": "string"},
            ],
        },
        # A list of records needs its element record first: `validate_spec` only lets a
        # record name an earlier one.
        *(
            [{"tag": "response_item", "fields": [{"name": n, "type": t} for n, t in item_fields]}]
            if item_fields
            else []
        ),
        {
            "tag": "response_body",
            "fields": _with_placeholder(
                None, response_body_fields(response_fields, item_fields), MARKERS["query"]
            ),
        },
        {
            "tag": "response",
            "fields": [
                {"name": "status", "type": "i64"},
                {"name": "body", "type": {"rec": "response_body"}},
                {"name": "retry_after_ms", "type": {"option": "i64"}},
            ],
        },
    ]


# --- response lists: the `{items: [...]}` envelope (§5.1 rule 6) ------------------------------


def _response_line(case) -> int:
    """The line of `Entonces` (a case may span several lines)."""
    return case.response.line or case.line


def _item_observation(body: dict, line: int, kind: str):
    """Split one response body into its scalar keys and the `items` list observation.

    Returns `(others, items)` where `others` are `(campo, valor, línea)` pairs and `items` is
    None when the body has no `items`, or the list of elements, each a list of
    `(campo, valor, línea)` pairs.
    """
    others, items = [], None
    for name, value in body.items():
        if name != "items":
            others.append((name, value, line))
            continue
        if not isinstance(value, list):
            raise _declared(line, f"«items» de {kind} debe ser una lista [ … ]")
        items = []
        for element in value:
            if isinstance(element, list):
                raise _declared(line, "no puede haber una lista dentro de otra lista en «items»")
            if not isinstance(element, dict):
                raise _declared(line, "cada elemento de «items» debe ser un objeto { … }")
            if not element:
                raise _declared(line, "cada elemento de «items» necesita al menos un campo")
            keys = []
            for key, item in element.items():
                if isinstance(item, (list, dict)):
                    what = "una lista" if isinstance(item, list) else "un objeto"
                    raise _declared(line, f"el campo «{key}» de «items» no puede ser {what}")
                keys.append((key, item, line))
            items.append(keys)
    return others, items


def _merge_item_fields(item_bags) -> list[tuple[str, dict]]:
    """Merge the keys of the list's elements; a key absent from any element is optional."""
    order: list[str] = []
    types: dict[str, str] = {}
    present: dict[str, int] = {}
    for body in item_bags:
        for name, value, at in body:
            found = _value_type(value)
            if found is None:
                raise _declared(at, f"el campo «{name}» de «items» no es texto ni número entero")
            if found in ("bool", "float"):
                raise _declared(at, f"el campo «{name}» de «items» es {found} y §5.1 no lo define")
            if name in types and types[name] != found:
                raise _declared(
                    at, f"el campo «{name}» de «items» aparece como texto y como número"
                )
            types.setdefault(name, found)
            if name not in order:
                order.append(name)
            present[name] = present.get(name, 0) + 1
    out = []
    for name in order:
        if present[name] < len(item_bags):
            out.append((name, {"option": types[name]}))
        else:
            out.append((name, types[name]))
    return out


def response_body_fields(response_fields, item_fields):
    """The response keys, with `items: option<list<rec<response_item>>>` first when present."""
    fields = list(response_fields)
    if item_fields is not None:
        fields.insert(0, ("items", {"option": {"list": {"rec": "response_item"}}}))
    return fields


def _things(doc: Doc) -> dict[str, list[tuple[str, str, bool, str | None, bool]]]:
    """`{cosa: [(campo, tipo, obligatorio, apunta a, único)]}` in declaration order."""
    fields: dict[str, list[tuple[str, str, bool, str | None, bool]]] = {}
    for story in doc.stories:
        for thing in story.things:
            merged = fields.setdefault(thing.name, [])
            known = {entry[0]: (entry[1], entry[2]) for entry in merged}
            for field in thing.fields:
                if field[0] not in known:
                    merged.append(field)
    return fields


def _query_fields(observations) -> list[tuple[str, dict]]:
    """Query fields: a typed key keeps its declared type, an untyped one is inferred as before."""
    order: list[str] = []
    typed: dict[str, str] = {}
    plain = []
    for key, value, line in observations:
        if isinstance(value, list):  # `_typed_query` already decided the type
            typed[key] = value[0]
        else:
            plain.append((key, value, line))
        if key not in order:
            order.append(key)
    inferred = {name: kind for name, kind in _field_table(plain, "consulta", bodies=None)}
    out = []
    for key in order:
        if key in typed:
            out.append((key, {"option": typed[key]}))
        else:
            out.append((key, inferred[key]))
    return out


def _tables(doc: Doc) -> list[dict]:
    """`spec.tables` for the `(tabla)` things, in first-declaration order (section B).

    `id` is always the first column (`i64`, primary key); a required field keeps its type and an
    optional one becomes `option<T>`; `único` adds simple unique constraints. No table, no key.
    """
    fields = _things(doc)
    order: list[str] = []
    for story in doc.stories:
        for thing in story.things:
            if thing.table and thing.name not in order:
                order.append(thing.name)
    out: list[dict] = []
    for name in order:
        fields_of = fields.get(name, [])
        columns = [{"name": "id", "type": "i64"}]
        unique = []
        refs = []
        for field, kind, required, target, is_unique in fields_of:
            columns.append({"name": field, "type": kind if required else {"option": kind}})
            if is_unique:
                unique.append(field)
            if target is not None:
                refs.append({"column": field, "target": f"{target}.id"})
        out.append(
            {
                "name": name,
                "columns": columns,
                "primary_key": "id",
                "unique": unique,
                "refs": refs,
            }
        )
    return out


def _story_table(things) -> str | None:
    """The story's single `(tabla)` thing, or None; more than one is ambiguous with a query."""
    tables = []
    for thing in things:
        if thing.table and thing.name not in tables:
            tables.append(thing.name)
    return tables[0] if len(tables) == 1 else None


def _typed_query(query, table_of: str | None, things, doc: Doc, story) -> list:
    """`[(clave, [tipo declarado], línea)]` for a story with exactly one table; raw otherwise.

    A story without `(tabla)` keeps the untyped HTTP inference byte for byte: its values keep
    their own type.
    """
    if not query:
        return query
    if len({thing.name for thing in story.things if thing.table}) > 1:
        raise StoryError(
            query[0][2],
            f"la historia «{story.id}» declara varias cosas «(tabla)» y filtra por consulta: "
            "no se sabe a cuál se refiere; separa esa ruta en otra historia",
        )
    if table_of is None:
        return query
    columns = {"id": "i64"}
    columns.update({field: kind for field, kind, _r, _t, _u in things.get(table_of, [])})
    out = []
    for key, value, line in query:
        if key not in columns:
            known = ", ".join(columns)
            raise StoryError(
                line,
                f"la clave de consulta «{key}» no es una columna de «{table_of}» "
                f"(son: {known})",
            )
        if columns[key] == "i64":
            if not re.fullmatch(r"[+-]?\d+", value) or not (
                -2**63 <= int(value) < 2**63
            ):
                raise StoryError(
                    line,
                    f"la clave de consulta «{key}» es un número entero; llegó «{value}»",
                )
        out.append((key, [columns[key]], line))
    return out


def approved_doc(doc: Doc) -> Doc:
    """The document with only its approved stories: what `compile_doc` may look at.

    Section 3 step 7: a `propuesta` or `pospuesta` story is not compiled, and it cannot lend a
    thing, a route or a session to an approved one: the filter comes before any inference. A
    story with cases but no `Estado:` is a mistake, not a draft (F2), so it still stops.
    """
    for story in doc.stories:
        if story.state is None and story.cases:
            raise StoryError(
                story.line,
                f"la historia «{story.id}» no indica su «Estado:»; sólo se compila lo aprobado "
                "(Estado: aprobada)",
            )
    return Doc(
        stories=[story for story in doc.stories if story.state == "aprobada" and story.cases],
        rules=list(doc.rules),
        lines=list(doc.lines),
        left_out=list(doc.left_out),
    )


def compile_doc(doc: Doc, *, name: str = "proyecto") -> dict:
    """Compile the approved stories of the document, in order, to one spec (rules 1-8 of §5.1)."""
    if not doc.stories:
        raise StoryError(1, "el documento no tiene historias")
    has_approved = any(story.state == "aprobada" for story in doc.stories)
    if not has_approved and all(story.state in STATUSES for story in doc.stories):
        # Drafts only: no empty spec pretending success. It matters more than anything else.
        raise StoryError(
            doc.stories[0].line,
            "el documento no tiene historias aprobadas; sólo se compila lo aprobado "
            "(Estado: aprobada)",
        )
    doc = approved_doc(doc)  # F2: a story with cases and no `Estado:` still stops here
    if not has_approved:
        raise StoryError(
            doc.stories[0].line if doc.stories else 1,
            "el documento no tiene historias aprobadas; sólo se compila lo aprobado "
            "(Estado: aprobada)",
        )
    if not doc.stories:  # approvals without cases contribute nothing
        raise StoryError(1, "ninguna historia aprobada tiene casos")
    things = _things(doc)
    query_obs, body_obs, response_obs, created_body = [], [], [], []
    routes: list[dict] = []
    params: list[str] = []
    response_bodies: list[dict] = []
    flat_bodies: list[dict] = []
    item_bodies: list[list] = []
    response_lists: int | None = None  # line of the first `items` key seen
    seen: dict[tuple[str, str], int] = {}
    seen_by: dict[tuple[str, str], Story] = {}  # the story that declared the route first
    route_auth: dict[tuple[str, str], str] = {}
    session_table: dict[str, dict] = {}
    for story in doc.stories:
        table_of = _story_table(story.things)
        auth = f"token({story.requires})" if story.requires else "none"
        for token, subject, scope in story.sessions:
            previous = session_table.get(token)
            if previous is not None and (previous["subject"], previous["scope"]) != (
                subject,
                scope,
            ):
                raise StoryError(
                    story.sessions_line,
                    f"el token de «{subject}» ya se usa en la línea {previous['line']} con otro "
                    "nombre o nivel; un token es siempre la misma persona y el mismo nivel",
                )
            session_table.setdefault(
                token, {"subject": subject, "scope": scope, "line": story.sessions_line}
            )
        for case in story.cases:
            path, query = _split_query(case.request.path, case.line)
            route, names = _route_path(path, case.line)
            params += [name for name in names if name not in params]
            # Two cases may share a route: they are two examples of the same request, but two
            # stories that disagree on who may call it change one of the two contracts.
            key = (case.request.method, route)
            if key not in seen:
                seen[key] = case.line
                seen_by[key] = story
                route_auth[key] = auth
                routes.append(
                    {
                        "method": case.request.method,
                        "path": route,
                        "handler": _handler_name(route),
                        "auth": auth,  # rule 8, per story
                        "rate": False,
                    }
                )
            else:
                routes_auth = route_auth[key]
                if routes_auth != auth:
                    raise StoryError(
                        case.line,
                        f"la ruta {case.request.method} {route} ya la usa la historia "
                        f"«{seen_by[key].id}» (línea {seen[key]}) con otro acceso; todas las "
                        "historias que la comparten deben pedir el mismo permiso",
                    )
            query_obs += _typed_query(query, table_of, things, doc, story)
            if case.creates is None:
                body_obs += [(k, v, case.line) for k, v in (case.request.body or {}).items()]
            else:
                created_body += [
                    (name, kind, case.line)
                    for name, kind in _created_body(case, things.get(case.creates))
                ]
            if case.response.body is not None:
                response_bodies.append(case.response.body)
                others, items = _item_observation(
                    case.response.body, _response_line(case), "respuesta"
                )
                response_obs += others
                plain_body = {name: value for name, value, _ in others}
                flat_bodies.append(plain_body)
                if items is not None:
                    if response_lists is None:
                        response_lists = case.line
                    item_bodies.extend(items)  # one bag of (campo, valor, línea) per element
            for given in case.given:
                _check_given(given, things.get(given.thing), case.id, story.id)
    # Rule 7: `<ruta sin barras, en minúsculas>`; when the name collides, every colliding route
    # gets the lowercase method added (`/books` GET+POST -> `books_get`, `books_post`).
    counts: dict[str, int] = {}
    for route in routes:
        counts[route["handler"]] = counts.get(route["handler"], 0) + 1
    for route in routes:
        if counts[route["handler"]] > 1:
            route["handler"] = f"{route['handler']}_{route['method'].lower()}"
    tables = _tables(doc)
    item_fields = None
    if response_lists and not item_bodies:
        raise _declared(
            response_lists,
            "«items» aparece solo como lista vacía: hace falta un caso con un elemento "
            "para saber qué lleva cada uno",
        )
    if item_bodies:
        item_fields = _merge_item_fields(item_bodies)
    types = _types(
        params,
        _query_fields(query_obs),
        _body_fields(
            created_body,
            _field_table(body_obs, "cuerpo", bodies=None),
            # A request without a body (`GET`, `DELETE`, or an empty `{}`) carries no body field.
            every_route_has_body=all(
                case.creates is not None or bool(case.request.body)
                for story in doc.stories
                for case in story.cases
            ),
        ),
        _field_table(response_obs, "respuesta", bodies=flat_bodies),
        item_fields,
    )
    spec = {
        "name": name,  # rule 1: the project name; equivalence ignores it
        "types": types,
        "atoms": [],  # rule 2
        "routes": routes,
        "providers": {},
        "effects": {},
        "cases": [],
    }
    if tables:
        spec["tables"] = tables
        spec["providers"] = {"sql": "all"}  # handlers reach the tables only through `cap sql`
    if session_table:
        spec["auth"] = {
            token: {"subject": entry["subject"], "scope": entry["scope"]}
            for token, entry in session_table.items()
        }
    try:
        validate_spec(spec)
    except GraphError as error:
        raise StoryError(doc.stories[0].line, f"la spec generada no es válida: {error}") from error
    return spec


def compile_text(text: str, *, name: str = "proyecto") -> dict:
    from .check import check_text

    errors, doc = check_text(text)
    if doc is None or errors:
        raise errors[0] if errors else StoryError(1, "documento vacío")
    return compile_doc(doc, name=name)
