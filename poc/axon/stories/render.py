"""Readable review of a `historias.md` (section 8 C3): plain prose for the person.

Two views of the same document:

- `render(doc)` is for the person who answered the interview: no routes, no JSON, no status
  codes and no technical defaults. Each case reads like «si piden … , …» and the end lists the
  `SUPUESTO:` rows of the story plus the fixed defaults in plain words. With `only="H-02"` it
  prints just that story's cases: `story add` uses it so the person reads only the new slice;
- `render_technical(doc)` is the view for whoever wires or reviews the API: routes, bodies,
  status codes and the compiler's defaults.
"""

from __future__ import annotations

import json
import re

from .compile import _route_path, _split_query, _things
from .parse import Doc, state_text

_STATUS_TEXT = {
    200: "responde 200",
    201: "responde 201",
    204: "responde 204 sin cuerpo",
    400: "responde 400 dato mal formado",
    401: "responde 401 no autorizado",
    403: "responde 403 prohibido",
    404: "responde 404 no encontrado",
    409: "responde 409 conflicto",
    422: "responde 422 dato inválido",
    429: "responde 429 demasiadas peticiones",
    500: "responde 500 error interno",
}

# The same statuses in the person's words (no numbers).
_PLAIN_OUTCOME = {
    200: "se muestra",
    201: "se crea",
    202: "se acepta",
    204: "se hace, sin más que decir",
    400: "se rechaza: lo enviado no se entiende",
    401: "se rechaza: hay que identificarse",
    403: "se rechaza: no hay permiso",
    404: "no existe",
    409: "se rechaza: choca con algo que ya existe",
    422: "se rechaza: falta algo o está mal escrito (dato inválido)",
    429: "se rechaza: se ha pedido demasiadas veces seguidas",
    500: "falla por dentro",
}

_FIXED_PLAIN = {
    "Todas las peticiones son públicas (no hay permisos): auth «none».":
        "Todos los que usan el sistema pueden hacer todo: nadie necesita identificarse.",
    "No hay límite de peticiones en ninguna ruta: rate «false».":
        "No hay límite de veces seguidas que se puede pedir lo mismo.",
    "El id de cada cosa es automático y numérico.":
        "Cada cosa recibe un número automático para distinguirla.",
    "Ninguna cosa se relaciona con otra.":
        "Ninguna cosa está ligada a otra: por ejemplo, un préstamo no guarda "
        "qué libro ni qué socio.",
    "No se exige unicidad de ningún campo.": "Puede haber dos cosas con el mismo nombre o título.",
    "Borrar es definitivo.": "Lo que se borra no se puede recuperar.",
}

_STANDARD_ERRORS = {"validation_error", "not_found", "conflict", "forbidden", "unauthorized"}

# How a single-story review relates to approval (they decide which defaults are shown).
NEW = "new"  # a slice being added (`story add`), with its final state
BEING_APPROVED = "being_approved"  # the candidate of `story approve` (not approved yet)
REOPENED = "reopened"  # an approved story reviewed again

SUPUESTOS_HEADER = "SUPUESTOS (léelos con la persona uno a uno y que confirme o cambie cada uno):"
DEFAULTS_ONCE = "Los supuestos por defecto del archivo ya se aprobaron con una historia anterior"

_LEVEL_PLAIN = {"read": "lector", "write": "editor", "admin": "administrador"}
_LEVEL_ONE_ORDER = {"read": 0, "write": 1, "admin": 2}  # the same `>=` as check.py
_LEVEL_ONE = {
    "read": "una persona lectora",
    "write": "una persona editora",
    "admin": "una persona administradora",
}
_MASKED = re.compile(r'^"([^"]+)"$')

_METHODS = {"POST": "crear", "GET": "consultar", "PUT": "cambiar", "PATCH": "cambiar",
            "DELETE": "borrar"}


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _given_text(case) -> str:
    if not case.given:
        return "sin datos"
    parts = []
    for given in case.given:
        fields = ", ".join(f"{name}={_dumps(value)}" for name, value in given.fields.items())
        parts.append(f"la cosa {given.thing} {given.id}" + (f" con {fields}" if fields else ""))
    return "; ".join(parts)


def _request_text(case) -> str:
    path, query = _split_query(case.request.path, case.line)
    route, _ = _route_path(path, case.line)
    text = f"{case.request.method} {route}"
    if query:
        text += " con " + ", ".join(f"{key}={_dumps(value)}" for key, value, _ in query)
    if case.request.body is not None:
        text += " y cuerpo " + _dumps(case.request.body)
    if case.request.header is not None:
        text += f" con cabecera {case.request.header[0]}"
    return text


def _plain_value(value) -> str:
    if isinstance(value, str):
        return f"«{value}»"
    return str(value)


def _plain_name(name: str) -> str:
    """`producto_id` reads «producto»: the review never shows data-model suffixes."""
    if name.endswith("_id") and len(name) > 3:
        name = name[:-3]
    return name.replace("_", " ")


def _plain_fields(fields: dict) -> str:
    return ", ".join(f"{_plain_name(name)} {_plain_value(value)}" for name, value in fields.items())


def _mask(token: str) -> str:
    """A credential in the technical view: four characters and `…`, or only `…` when short."""
    return token[:4] + "…" if len(token) > 8 else "…"


def _sessions(doc: Doc) -> dict[str, tuple[str, str]]:
    """`{token: (sujeto, alcance)}` of the whole document (the first declaration wins)."""
    out: dict[str, tuple[str, str]] = {}
    for story in doc.stories:
        for token, subject, scope in story.sessions:
            out.setdefault(token, (subject, scope))
    return out


def _actor_plain(doc: Doc, case) -> str:
    """«como Raúl,» / «sin identificarse,»: the person, never the token."""
    if case.actor is None:
        return "sin identificarse," if getattr(case, "actor_spelled", False) else ""
    known = _sessions(doc).get(case.actor)
    return f"como {known[0]}," if known else "como una persona sin sesión declarada,"


def _access_plain(doc: Doc, story) -> str:
    """«Acceso: solo una persona administradora puede hacer esto; Raúl tiene ese permiso.»

    The hierarchy decides, as in `check`: a higher level has the permission too.
    """
    if story.requires is None:
        return ""
    who = _LEVEL_ONE[story.requires]
    needed = _LEVEL_ONE_ORDER[story.requires]
    reaching = [subject for subject, scope in _sessions(doc).values()
                if _LEVEL_ONE_ORDER[scope] >= needed]
    text = f"Acceso: solo {who} puede hacer esto"
    if reaching:
        text += "; " + ", ".join(dict.fromkeys(reaching)) + " tiene ese permiso"
    return text + "."


def _plain_given(case) -> str:
    if not case.given:
        return ""
    parts = []
    for given in case.given:
        text = f"existe {given.thing} {given.id}"
        if given.fields:
            text += f" con {_plain_fields(given.fields)}"
        parts.append(text)
    return " y ".join(parts)


def _known_one(doc: Doc, noun: str) -> str:
    """The declared thing the route's noun names (plural, singular or «socio»→«socios»)."""
    names = list(_things(doc))
    for candidate in (noun, noun[:-1] if noun.endswith("s") else noun, noun + "s"):
        if candidate in names:
            return candidate
    return noun


def _plain_request(case, doc: Doc) -> str:
    """The request in the person's words: creating, looking up, changing or deleting."""
    path, query = _split_query(case.request.path, case.line)
    route, params = _route_path(path, case.line)
    verb = _METHODS.get(case.request.method, "usar")
    noun = route.strip("/").split("/")[0].replace("-", " ").replace("_", " ")
    # shortcut: singular by dropping a final "s" and no article, so gender never shows; a known
    # thing is named as declared, otherwise the route's noun is used.
    one = case.creates or _known_one(doc, noun)
    body = case.request.body or {}
    if case.request.method in ("POST", "PUT", "PATCH") and body:
        text = f"{verb} {one} con {_plain_fields(body)}"
    elif params or query or body:
        text = f"{verb} {one}"
        if body:
            text += f" con {_plain_fields(body)}"
        if params:
            text += f" «{_last_segment(path)}»"
        if query:
            text += " " + ", ".join(f"donde {key} es {_plain_value(value)}"
                                    for key, value, _ in query)
    else:
        text = f"{verb} {noun}"
    if case.request.header is not None:
        text += f" diciendo en la cabecera {case.request.header[0]}: {case.request.header[1]}"
    return text


def _last_segment(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    return parts[-1] if parts else ""


def _by_rule(text: str, case, rule_text: dict[str, str]) -> str:
    """The approved rule says what happens better than the generic outcome."""
    rule = rule_text.get(case.rule)
    if not rule:
        return text
    # lowercase the initial only for a plain word, never for an acronym like HTTP
    return rule if rule[1:2].isupper() else rule[:1].lower() + rule[1:]


def _plain_response(case, rule_text: dict[str, str]) -> str:
    outcome = _PLAIN_OUTCOME.get(case.response.status, "responde")
    body = case.response.body or {}
    listed = _plain_items(body)
    if listed and case.response.status < 300:
        return _by_rule(listed, case, rule_text)
    error = body.get("error")
    if outcome == "se crea" and body:
        fields = {key: value for key, value in body.items() if key != "id"}
        if fields:
            outcome += f" con {_plain_fields(fields)}"
    elif outcome.startswith("se rechaza") and error and error not in _STANDARD_ERRORS:
        outcome += ": " + str(error).replace("_", " ")
    elif outcome == "no existe":
        outcome = "avisa de que no existe"
    return _by_rule(outcome, case, rule_text)


def assumptions(
    doc: Doc, *, story: str | None = None, review: str = REOPENED
) -> list[str]:
    """`SUPUESTO:` rows for `story` (or the whole document), then the defaults applied unsaid.

    With `story`, `review` says how it relates to approval (see `_defaults_for`): a file default
    whose plain text is already one of the story's own SUPUESTO rows is not repeated.
    """
    own = _story_supuestos(doc, story)
    rows = _fixed_rows(doc)
    if story is not None:
        existing = {row.strip().lower().rstrip(".") for row in own}
        # shortcut: exact match (lowercased, without a final dot) only — no fuzzy word matching
        rows = [row for row in rows if _FIXED_PLAIN.get(row, row).strip().lower().rstrip(".")
                not in existing]
        return _defaults_for(doc, story, rows, review=review) + own
    return own + rows


def _defaults_for(
    doc: Doc, story: str, rows: list[str], *, review: str = REOPENED
) -> list[str]:
    """The file defaults for a single-story review, by `review` (see `NEW` … `REOPENED`).

    Approval decides, not position or the target's own state: `other_approved` looks at every
    story except the target, wherever it sits in the file.
    """
    other_approved = any(
        entry.id != story and entry.state == "aprobada" for entry in doc.stories
    )
    if review == REOPENED:
        return []  # they were read when this story was approved: never again
    if other_approved:
        return [DEFAULTS_ONCE] if rows else []
    # NEW and BEING_APPROVED: this story is (or becomes) the first approval, so read them now.
    return rows


def _fixed_rows(doc: Doc) -> list[str]:
    rows = [
        "Todas las peticiones son públicas (no hay permisos): auth «none».",
        "No hay límite de peticiones en ninguna ruta: rate «false».",
        "El id de cada cosa es automático y numérico.",
        "Ninguna cosa se relaciona con otra.",
        "No se exige unicidad de ningún campo.",
        "Borrar es definitivo.",
    ]
    methods = {case.request.method for story in doc.stories for case in story.cases}
    if "DELETE" not in methods:
        rows = [row for row in rows if not row.startswith("Borrar")]
    if any(entry[3] for fields in _things(doc).values() for entry in fields):
        rows = [row for row in rows if not row.startswith("Ninguna cosa se relaciona")]
    if any(entry[-1] for fields in _things(doc).values() for entry in fields):
        rows = [row for row in rows if not row.startswith("No se exige unicidad")]
    return rows


def _tables_of(doc: Doc) -> list[tuple[str, list[str]]]:
    """`[(cosa, [campos únicos])]` of the `(tabla)` things, in declaration order."""
    out: list[tuple[str, list[str]]] = []
    for story in doc.stories:
        for thing in story.things:
            if not thing.table or thing.name in [name for name, _ in out]:
                continue
            fields = _things(doc).get(thing.name, [])
            out.append((thing.name, [entry[0] for entry in fields if entry[-1]]))
    return out


def _stored_plain(doc: Doc) -> list[str]:
    """What is kept between requests, in the person's words: what, and what cannot repeat."""
    lines = []
    for name, unique in _tables_of(doc):
        # no plural guessing in Spanish: «Se guarda cada libro; no se repite: titulo»
        text = f"Se guarda cada {_plain_name(name)}"
        if unique:
            text += "; no se repite: " + ", ".join(_plain_name(field) for field in unique)
        lines.append(text + ".")
    return lines


def _left_out(doc: Doc) -> list[str]:
    """«Se queda fuera»: the stories' `FUERA:` lines, then the document's «Fuera de alcance»."""
    found = []
    for story in doc.stories:
        for _, text in story.free_text:
            match = re.search(r"FUERA:\s*(.+?)\s*$", text)
            if match:
                found.append(match.group(1))
    return found + [text for _, text in doc.left_out]


def _plain_items(body) -> str:
    """A list response in words: «una lista con 2 cosas: …» or «una lista vacía»."""
    items = body.get("items") if isinstance(body, dict) else None
    if items is None:
        return ""
    if not items:
        return "se muestra una lista vacía"
    rows = "; ".join(_plain_fields(element) for element in items)
    count = "1 cosa" if len(items) == 1 else f"{len(items)} cosas"
    return f"se muestra la lista ({count}): {rows}"


def _story_supuestos(doc: Doc, only: str | None = None) -> list[str]:
    """The texts of the `SUPUESTO:` lines the payload wrote into the prose."""
    found = []
    stories = [story for story in doc.stories if only is None or story.id == only]
    for story in stories:
        for _, text in story.free_text:
            for match in re.finditer(r"SUPUESTO:\s*(.+?)\s*$", text):
                found.append(match.group(1))
    return found


def _things_plain(doc: Doc) -> list[str]:
    lines = []
    for name, fields in _things(doc).items():
        rows = []
        for field, kind, required, target, unique in fields:
            label = "texto" if kind == "string" else "número"
            row = f"{_plain_name(field)} ({label}"
            if required:
                row += ", obligatorio"
            if unique:
                row += ", único"
            if target:
                row += f", apunta a {_plain_name(target)}"
            rows.append(row + ")")
        lines.append(f"Cada {name} tiene: " + (", ".join(rows) if rows else "sin datos"))
    return lines


def _pick(doc: Doc, only: str | None) -> list:
    """The stories to show; `only` names one that must exist."""
    if only is None:
        return list(doc.stories)
    chosen = [story for story in doc.stories if story.id == only]
    if not chosen:
        raise ValueError(f"no hay historia «{only}» en el documento")
    return chosen


def state_plain(story, *, being_approved: bool = False) -> str:
    """The state line of the review, before the cases: only what the person must know.

    `Aprobada` is the normal case and stays silent. A `propuesta`/`pospuesta` shows its state;
    `being_approved` is the `story approve` candidate, which would be approved right away.
    """
    if being_approved:
        return "Se aprobaría al confirmar."
    if story.state == "pospuesta":
        reason = f": {story.reason}" if story.reason else ""
        return f"Pospuesta{reason}."
    if story.state == "propuesta":
        return "Propuesta: aún no está aprobada."
    if story.state is None:
        return "Sin estado: no se compila hasta marcarla como aprobada."
    return ""


def render(doc: Doc, *, only: str | None = None, review: str = REOPENED) -> str:
    """The review for the person: prose per case, what is created, what is left out, supuestos.

    `only="H-02"` prints just that story's cases (the things, «Se queda fuera» and SUPUESTOS stay
    document-wide); `story add` uses it so the person reads only the slice being added. With
    `only`, `review` says whether the story is new (`NEW`), the `story approve` candidate
    (`BEING_APPROVED`) or an approved story opened again (`REOPENED`, the default).
    """
    rule_text = {rule.id: rule.text for rule in doc.rules}
    lines: list[str] = []
    for story in _pick(doc, only):
        lines.append(f"{story.id} {story.title}")
        state = state_plain(story, being_approved=review == BEING_APPROVED)
        if state:
            lines.append(state)
        access = _access_plain(doc, story)
        if access:
            lines.append(access)
        for case in story.cases:
            kind = "si sale bien" if case.kind == "feliz" else "si algo va mal"
            prefix = f"- {case.id} [{kind}]"
            actor = _actor_plain(doc, case)
            if actor:
                prefix += f" {actor}"
            given = _plain_given(case)
            if given:
                prefix += f" {given};"
            text = _plain_request(case, doc)
            result = _plain_response(case, rule_text)
            lines.append(f"{prefix} si piden {text}, {result}.")
        lines.append("")
    things = _things_plain(doc)
    if things:
        lines.extend(things)
        lines.append("")
    stored = _stored_plain(doc)
    if stored:
        lines.extend(stored)
        lines.append("")
    left_out = _left_out(doc)
    if left_out:
        lines.append("Se queda fuera (lo pediste, pero el sistema aún no lo guarda):")
        lines.extend(f"- {row}" for row in left_out)
        lines.append("")
    lines.append(SUPUESTOS_HEADER)
    for row in assumptions(doc, story=only, review=review):
        lines.append(f"- {_FIXED_PLAIN.get(row, row)}")
    return "\n".join(lines).rstrip() + "\n"


def render_technical(doc: Doc) -> str:
    """The technical review: routes, JSON bodies, status codes and the compiler's defaults."""
    lines: list[str] = []
    for story in doc.stories:
        lines.append(f"{story.id} {story.title}")
        if story.state is not None and story.state != "aprobada":
            lines.append(f"Estado: {state_text(story)}")
        for case in story.cases:
            kind = "si sale bien" if case.kind == "feliz" else "si algo va mal"
            status = _STATUS_TEXT.get(case.response.status, f"responde {case.response.status}")
            body = f" {_dumps(case.response.body)}" if case.response.body else ""
            rule = f" (regla {case.rule})" if case.rule else ""
            actor = ""
            known = _sessions(doc).get(case.actor) if case.actor else None
            masked = _mask(case.actor) if case.actor else ""
            if known is not None and case.actor:
                subject, scope = known
                actor = f" como {subject} ({scope}, token {masked})"
            elif case.actor is not None:
                actor = f" como token {masked} (sin sesión)"
            lines.append(
                f"- {case.id} [{kind}]{actor} si {_given_text(case)} y piden "
                f"{_request_text(case)}, {status}{body}{rule}"
            )
        lines.append("")
    things = _things(doc)
    for name in things:
        fields = ", ".join(
        field + (f"→{target}" if target else "") for field, _, _, target, *_ in things[name]
    ) or "(sin campos)"
        lines.append(f"Cosas: {name} → {fields}")
    for name, _unique in _tables_of(doc):
        columns = ["id (i64)"]
        for field, kind, required, _target, unique in _things(doc).get(name, []):
            label = "texto" if kind == "string" else "i64"
            text = f"{field} ({label}" + (", obligatorio" if required else "") + ")"
            if unique:
                text = text[:-1] + ", único)"
            columns.append(text)
        lines.append(f"Tabla: {name} → " + ", ".join(columns))
    if things:
        lines.append("")
    left_out = _left_out(doc)
    if left_out:
        lines.append("Se queda fuera:")
        lines.extend(f"- {row}" for row in left_out)
        lines.append("")
    lines.append(SUPUESTOS_HEADER)
    for row in assumptions(doc):
        lines.append(f"- {row}")
    return "\n".join(lines).rstrip() + "\n"
