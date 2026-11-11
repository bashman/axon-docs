"""Export the approved stories of `historias.md` as an HTTP-cases document.

The document is the one `axon/http_cases.py` replays: `{"name": …, "cases": [ … ]}`, one
case per story case, in file order. It is derived from the same `Dado`/`Cuando`/`Entonces`
rows `compile_doc` reads, so a handler that makes these cases pass makes the approved
stories pass.

Shape of one case (the keys `_run_case` and `HttpHost.reset` read, the same ones
`tools/t6/cases.py::_case` writes):

- `id`: the story case id (`C-05`); `step`: always 1;
- `sessions`: the `Sesiones:` of the stories, `{token: {subject, scope}}`;
- `seed`: rows of the `(tabla)` things, keyed and fielded as `spec.tables` names them;
- `requests`: one `{method, path, headers, body, time}`; `Como: "token"` adds
  `Authorization: Bearer <token>`;
- `responses`: one `{status[, body]}` (the replay matches the body partially).
"""

from __future__ import annotations

from .check import check_text
from .compile import approved_doc, compile_doc
from .parse import Doc, StoryError


def _session_table(doc: Doc) -> dict[str, dict]:
    """`Sesiones:` of the stories as `{token: {subject, scope}}`, with compile's conflict rule."""
    table: dict[str, dict] = {}
    lines: dict[str, int] = {}
    for story in doc.stories:
        for token, subject, scope in story.sessions:
            previous = table.get(token)
            if previous is not None and (previous["subject"], previous["scope"]) != (
                subject,
                scope,
            ):
                raise StoryError(
                    story.sessions_line,
                    f"el token de «{subject}» ya se usa en la línea {lines[token]} con otro "
                    "nombre o nivel; un token es siempre la misma persona y el mismo nivel",
                )
            table.setdefault(token, {"subject": subject, "scope": scope})
            lines.setdefault(token, story.sessions_line)
    return table


def _row_id(given) -> int:
    text = str(given.id)
    if not text.isdigit():
        raise StoryError(
            given.line, f"el identificador de «{given.thing}» debe ser un número entero"
        )
    return int(text)


def _seed(case, table_of: dict[str, list[dict]]) -> dict:
    """The `Dado` rows as `{tabla: [fila]}`; a thing that is not a table cannot be seeded."""
    seed: dict[str, list[dict]] = {}
    for given in case.given:
        columns = table_of.get(given.thing)
        if columns is None:
            raise StoryError(
                given.line,
                f"el caso «{case.id}» da «{given.thing}» en «Dado», pero no se guarda como "
                "tabla; marca la cosa con «(tabla)» o quita ese «Dado»: no se puede sembrar",
            )
        row = {"id": _row_id(given)}
        for name, value in given.fields.items():
            if name not in {column["name"] for column in columns}:
                raise StoryError(given.line, f"la cosa «{given.thing}» no tiene el campo «{name}»")
            if name == "id":
                raise StoryError(
                    given.line,
                    f"la cosa «{given.thing}» recibe su número automático; no siembres «id»",
                )
            # `apunta a` keeps the i64 type: the value already is the row of the target table.
            row[name] = value
        seed.setdefault(given.thing, []).append(row)
    return seed


def _case(case, sessions: dict[str, dict], table_of: dict[str, list[dict]]) -> dict:
    """One story case as one HTTP case; anything it cannot represent stops at its line."""
    headers: dict[str, str] = {}
    if case.request.header is not None:
        headers[case.request.header[0]] = case.request.header[1]
    if case.actor is not None:
        if case.actor not in sessions:
            raise StoryError(
                case.actor_line,
                f"el caso «{case.id}» usa la sesión «{case.actor}», que no está en «Sesiones:»",
            )
        headers["Authorization"] = f"Bearer {case.actor}"
    request = {
        "method": case.request.method,
        "path": case.request.path,
        "headers": headers,
        "body": case.request.body or {},
        "time": 0,
    }
    response: dict = {"status": case.response.status}
    if case.response.body is not None:
        response["body"] = case.response.body
    return {
        "id": case.id,
        "step": 1,
        "sessions": sessions,
        "seed": _seed(case, table_of),
        "requests": [request],
        "responses": [response],
    }


def cases_doc(doc: Doc, *, name: str = "proyecto") -> dict:
    """The cases document of the approved stories of `doc`, in file order."""
    # Compile first: it applies the same approved filter, the same validation (route conflicts,
    # `Dado` against `Cosas`) and its `tables` name every seeded row.
    spec = compile_doc(doc, name=name)
    approved = approved_doc(doc)
    table_of = {table["name"]: table["columns"] for table in spec.get("tables", [])}
    sessions = _session_table(approved)
    cases = [
        _case(case, sessions, table_of) for story in approved.stories for case in story.cases
    ]
    return {"name": name, "cases": cases}


def cases_text(text: str, *, name: str = "proyecto") -> dict:
    """`story cases` from the source text: the same `check` as `compile`, then the document."""
    errors, doc = check_text(text)
    if doc is None or errors:
        raise errors[0] if errors else StoryError(1, "documento vacío")
    return cases_doc(doc, name=name)
