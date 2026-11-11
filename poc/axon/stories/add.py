"""`axon story add`: the deterministic append boundary of the conversational interview.

The conversation lives in the skill (`.claude/skills/axon-story/SKILL.md`); the model proposes
the content of one story as a small JSON payload and this module serializes it into the closed
grammar of `docs/requisitos-historias.md`, then validates the whole document with the existing
`check_text` and `compile_text`. Parsing, semantic checks and default derivation are not
re-implemented here.

## Payload (UTF-8 JSON, one object per added story)

Required keys (no others are accepted; every error names the offending JSON path):

```
title       string                          "Alta de libros"
role        string | null                   "Una persona bibliotecaria"; null -> "Alguien"
capability  string                          "dar de alta un libro"
purpose     string                          "tenerlo en el catálogo"
state       "propuesta" | "aprobada"        only "aprobada" compiles (section 3 step 7);
            | "pospuesta"                   "pospuesta (<razón>)" is not expressible here
things      list of thing                   [] -> the story declares no `Cosas:`
cases       non-empty list of case
rules       list of rule                    may be empty only if no case cites a rule
assumptions list of string (optional)       every answer the person did not give ("no sé");
                                            written as `SUPUESTO: <texto>.` and shown in the review
left_out    list of string (optional)       what the person asked for but the grammar cannot
                                            hold; shown under "Se queda fuera", never silent
```

```
thing = {"name": string,
         "table": bool (default false),           # `(tabla)`: the thing is stored
         "fields": non-empty list of {"name": string,
                                      "type": "texto" | "numero" | "entero",
                                      "required": bool (default false),
                                      "unique": bool (default false)}}  # needs "table": true

case  = {"kind": "feliz" | "error",
         "creates": string naming the declared thing created by the request (optional),
         "given": list of {"thing": string, "id": integer | string,
                           "fields": object of string -> string|integer (default {})},
         "request": {"method": "GET"|"POST"|"PUT"|"PATCH"|"DELETE",
                     "path": string starting with "/",   # may carry `?a=1`
                     "body": object of string -> string|integer (optional),
                     "header": {"name": string, "value": string} (optional)},
         "response": {"status": 100..599 integer,
                      "body": object of string -> string|integer (optional)},
         "rule": integer index into `rules` (optional; an `[error]` case cites a rule with `error`,
         a `feliz` case one without)}

rule  = {"text": string, "error": lowercase snake_case code (omit it for a behaviour rule,
         cited only by a `feliz` case)}
```

`given` is the starting state (section 5 `Dado`), never the request; values are concrete.

## Layout

The block follows section 4 verbatim: the title, the role phrase, `Estado:`, the `Cosas:` list,
`Casos:` and the rules after a blank line. A case is one `- C-NN …` item; when it does not fit in
`MAX_LINE` columns it breaks before `Cuando` / `Entonces` / `Regla:` and the continuation lines
are indented two spaces (the parser reads a continuation line that does not start with `- `).
A `Entonces <estado> [cuerpo]  Regla: BR-NN` piece is never broken (the parser needs its state).

## Result

`PreparedAdd(candidate, ids, assumptions, render)`: the whole file after the append, the IDs
allocated (`{"story": "H-03", "cases": ["C-05", ...], "rules": ["BR-03"]}`), the SUPUESTO rows
of the resulting document and its review text: `render(doc, only=<new story id>)`, i.e. only the
slice just added (the fixed defaults appear only with the file's first story).

`AddError(source, line, message)` reports a failure in `existing` (the file before the append,
with its own line), `candidate` (the document after it), `compile` (the HTTP spec) or `write`.

`append_story` writes a temporary file in the same directory and `os.replace`s it only after
`prepare_add` succeeded, so a failure leaves the original file byte-for-byte unchanged.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from .check import check_text
from .compile import compile_doc
from .parse import LEVEL_NAMES, Doc, StoryError
from .render import BEING_APPROVED, NEW, REOPENED, assumptions, render

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
STATES = ("propuesta", "aprobada", "pospuesta")
KINDS = ("feliz", "error")
FIELD_TYPES = {"texto": "texto", "numero": "numero", "entero": "entero"}


_TYPES = {"dict": "un objeto", "list": "una lista", "str": "un texto", "int": "un número"}


def _kind(value) -> str:
    return _TYPES.get(type(value).__name__, "otra cosa")


class PayloadError(ValueError):
    """A JSON payload outside the shape above; `path` is the offending field ('' = the root)."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        where = f"{path}: " if path else ""
        super().__init__(f"{where}{message}")


class AddError(ValueError):
    """`axon story add` could not add the story; `source` says where the problem was found.

    `source` is `existing` (the file as it was), `candidate` (the file plus the new story) or
    `write`. For `candidate`, `line` is a line of the *new* document; the CLI labels it.
    """

    def __init__(self, source: str, message: str, line: int | None = None):
        self.source = source
        self.line = line
        self.message = message
        super().__init__(message)


@dataclass
class PreparedAdd:
    candidate: str
    ids: dict[str, object]
    assumptions: list[str] = field(default_factory=list)
    render: str = ""


# --- IDs -------------------------------------------------------------------------------------


def next_id(prefix: Literal["H", "C", "BR"], used: Iterable[str]) -> str:
    """`<prefix>-NN` after the greatest existing suffix plus one; gaps are never reused."""
    marker = f"{prefix}-"
    numbers = []
    for name in used:
        text = str(name)
        if text.startswith(marker) and text[len(marker) :].isdigit():
            numbers.append(int(text[len(marker) :]))
    return f"{prefix}-{max(numbers, default=0) + 1:02d}"


# --- payload shape ---------------------------------------------------------------------------


def _check_object(value, path: str, keys: set[str], required: set[str]) -> Mapping:
    if not isinstance(value, dict):
        raise PayloadError(path, f"se esperaba un objeto {{…}}, llegó {_kind(value)}")
    unknown = sorted(set(value) - keys)
    if unknown:
        raise PayloadError(f"{path}.{unknown[0]}".lstrip("."), f"campo desconocido «{unknown[0]}»")
    for key in sorted(required):
        if key not in value:
            raise PayloadError(f"{path}.{key}".lstrip("."), f"falta el campo «{key}»")
    return value


def _string(value, path: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise PayloadError(path, f"se esperaba un texto, llegó {value!r}")
    if not value.strip():
        raise PayloadError(path, "se esperaba un texto no vacío")
    if "\n" in value or "\r" in value:
        raise PayloadError(path, "se esperaba una sola línea")
    return value.strip()


def _choice(value, path: str, choices: Sequence[str]) -> str:
    if value not in choices:
        raise PayloadError(path, f"se esperaba uno de {', '.join(choices)}; llegó {value!r}")
    return value


def _list(value, path: str, *, non_empty: bool = False) -> list:
    if not isinstance(value, list):
        raise PayloadError(path, f"se esperaba una lista, llegó {_kind(value)}")
    if non_empty and not value:
        raise PayloadError(path, "se esperaba una lista no vacía")
    return value


def _scalar_fields(value, path: str, *, allow_items: bool = False) -> dict:
    """`{campo: "texto" | 12}`: the values a `Dado`, body or response may carry.

    With `allow_items`, a response body may also carry `"items": [ {…}, … ]` (section 5.1 rule 6):
    every element is an object of scalars and is kept as written.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PayloadError(path, f"se esperaba un objeto {{…}}, llegó {_kind(value)}")
    out = {}
    for key, item in value.items():
        if allow_items and key == "items":
            if not isinstance(item, list):
                raise PayloadError(f"{path}.items", "se esperaba una lista de objetos [ … ]")
            out[key] = [
                _item_element(element, f"{path}.items[{index}]")
                for index, element in enumerate(item)
            ]
            continue
        if isinstance(item, (str, int)) and not isinstance(item, bool):
            out[key] = item
            continue
        # §5.1 has no boolean or decimal type: a boolean is refused here; a finite decimal
        # is passed on and the compiler reports it at the candidate's line.
        if isinstance(item, float) and item == item and abs(item) != float("inf"):
            out[key] = item
            continue
        raise PayloadError(
            f"{path}.{key}", f"se esperaba un texto o un número entero; llegó {item!r}"
        )
    return out


def _item_element(value, path: str) -> dict:
    """One element of a response list: an object whose values are scalars (no nesting)."""
    if not isinstance(value, dict):
        raise PayloadError(path, f"se esperaba un objeto {{ … }}; llegó {_kind(value)}")
    out = {}
    for key, item in value.items():
        if isinstance(item, (list, dict)):
            what = "una lista" if isinstance(item, list) else "un objeto"
            raise PayloadError(f"{path}.{key}", f"no puede ser {what}: solo texto o número entero")
        out[key] = _scalar_fields({key: item}, path)[key]
    return out


def _code(value, path: str) -> str:
    if not isinstance(value, str) or not value.isidentifier() or not value.islower():
        raise PayloadError(path, f"se esperaba un código en minúsculas (a_b); llegó {value!r}")
    return value


def _validated(data: Mapping) -> dict:
    """Check the payload and return a normalized copy (defaults filled)."""
    data = _check_object(
        data,
        "",
        {
            "title",
            "role",
            "capability",
            "purpose",
            "state",
            "reason",
            "access",
            "things",
            "cases",
            "rules",
            "assumptions",
            "left_out",
        },
        {"title", "capability", "purpose", "state", "cases", "rules"},
    )
    out = {
        "title": _string(data["title"], "title"),
        "role": _string(data.get("role"), "role", optional=True),
        "capability": _string(data["capability"], "capability"),
        "purpose": _string(data["purpose"], "purpose"),
        "state": _choice(data["state"], "state", STATES),
    }
    out["reason"] = _reason(data, out["state"])
    out["access"] = _access(data.get("access"))
    things = []
    for index, thing in enumerate(_list(data.get("things", []), "things")):
        where = f"things[{index}]"
        thing = _check_object(thing, where, {"name", "table", "fields"}, {"name", "fields"})
        table = thing.get("table", False)
        if not isinstance(table, bool):
            raise PayloadError(f"{where}.table", f"se esperaba true o false; llegó {table!r}")
        fields = []
        for position, entry in enumerate(_list(thing["fields"], f"{where}.fields", non_empty=True)):
            at = f"{where}.fields[{position}]"
            entry = _check_object(
                entry,
                at,
                {"name", "type", "required", "unique", "points_to"},
                {"name", "type"},
            )
            required = entry.get("required", False)
            if not isinstance(required, bool):
                raise PayloadError(
                    f"{at}.required", f"se esperaba true o false; llegó {required!r}"
                )
            unique = entry.get("unique", False)
            if not isinstance(unique, bool):
                raise PayloadError(f"{at}.unique", f"se esperaba true o false; llegó {unique!r}")
            if unique and not table:
                raise PayloadError(
                    f"{at}.unique",
                    "un campo «unique» necesita que la cosa sea una tabla («table»: true)",
                )
            fields.append(
                {
                    "name": _string(entry["name"], f"{at}.name"),
                    "type": _choice(entry["type"], f"{at}.type", tuple(FIELD_TYPES)),
                    "required": required,
                    "unique": unique,
                    "points_to": _string(entry.get("points_to"), f"{at}.points_to", optional=True),
                }
            )
        things.append(
            {"name": _string(thing["name"], f"{where}.name"), "table": table, "fields": fields}
        )
    out["things"] = things

    rules = []
    for index, rule in enumerate(_list(data["rules"], "rules")):
        where = f"rules[{index}]"
        rule = _check_object(rule, where, {"text", "error"}, {"text"})
        rules.append(
            {
                "text": _string(rule["text"], f"{where}.text").rstrip("."),
                "error": _code(rule["error"], f"{where}.error") if "error" in rule else None,
            }
        )
    out["rules"] = rules
    for key, prefix in (("assumptions", "SUPUESTO: "), ("left_out", "FUERA: ")):
        rows = []
        for index, row in enumerate(_list(data.get(key, []), key)):
            text = _string(row, f"{key}[{index}]")
            if text.startswith(prefix) or text.startswith("PENDIENTE:"):
                raise PayloadError(
                    f"{key}[{index}]", f"no empieces el texto con «{prefix.strip()}»"
                )
            rows.append(text)
        out[key] = rows

    cases = []
    for index, case in enumerate(_list(data["cases"], "cases", non_empty=True)):
        where = f"cases[{index}]"
        case = _check_object(
            case,
            where,
            {"kind", "actor", "given", "request", "response", "creates", "rule"},
            {"kind", "given", "request", "response"},
        )
        kind = _choice(case["kind"], f"{where}.kind", KINDS)
        actor = case.get("actor")
        if actor is not None:
            actor = _string(actor, f"{where}.actor")
            tokens = out["access"]["sessions"] if out["access"] else {}
            if actor not in tokens:
                raise PayloadError(
                    f"{where}.actor",
                    f"la sesión «{actor}» no está en «access.sessions»; sin identificarse va null",
                )
        elif out["access"] is not None and kind == "feliz":
            raise PayloadError(
                f"{where}.actor",
                "un caso feliz de una historia protegida necesita «actor» (la sesión que actúa)",
            )
        given = []
        for position, entry in enumerate(_list(case["given"], f"{where}.given")):
            at = f"{where}.given[{position}]"
            entry = _check_object(entry, at, {"thing", "id", "fields"}, {"thing", "id"})
            ident = entry["id"]
            if not isinstance(ident, (int, str)) or isinstance(ident, bool) or str(ident) == "":
                raise PayloadError(f"{at}.id", f"se esperaba un número o un texto; llegó {ident!r}")
            given.append(
                {
                    "thing": _string(entry["thing"], f"{at}.thing"),
                    "id": ident,
                    "fields": _scalar_fields(entry.get("fields"), f"{at}.fields"),
                }
            )
        request = _check_object(
            case["request"],
            f"{where}.request",
            {"method", "path", "body", "header"},
            {"method", "path"},
        )
        method = _choice(request["method"], f"{where}.request.method", METHODS)
        path = _string(request["path"], f"{where}.request.path")
        if not path.startswith("/") or " " in path:
            raise PayloadError(
                f"{where}.request.path",
                f"la ruta empieza por «/» y no lleva espacios; llegó {path!r}",
            )
        header = request.get("header")
        if header is not None:
            at = f"{where}.request.header"
            header = _check_object(header, at, {"name", "value"}, {"name", "value"})
            header = {
                "name": _string(header["name"], f"{at}.name"),
                "value": _string(header["value"], f"{at}.value"),
            }
        response = _check_object(
            case["response"], f"{where}.response", {"status", "body"}, {"status"}
        )
        status = response["status"]
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise PayloadError(
                f"{where}.response.status",
                f"se esperaba un número de estado (100..599); llegó {status!r}",
            )
        rule = case.get("rule")
        if rule is not None:
            if not isinstance(rule, int) or isinstance(rule, bool):
                raise PayloadError(
                    f"{where}.rule", f"se esperaba el índice de una regla; llegó {rule!r}"
                )
            if not 0 <= rule < len(rules):
                bound = f"0..{len(rules) - 1}" if rules else "there are no rules"
                raise PayloadError(f"{where}.rule", f"la regla {rule} no existe ({bound})")
            # Name the rule index as well as the case path: the payload carries `rules[rule]`,
            # and the document only shows the allocated «BR-NN», so a bare «sin error» does not
            # say which rule to change (a11g's non-reproducible failure leaned on that message).
            if (kind == "error") != (rules[rule]["error"] is not None):
                if kind == "error":
                    wanted = (
                        f"un caso [error] cita la regla {rule} (rules[{rule}]), que no tiene "
                        "«error»; añade «error» a esa regla o cita una que lo tenga"
                    )
                else:
                    wanted = (
                        f"un caso feliz cita la regla {rule} (rules[{rule}]), que tiene «error»; "
                        "quita «error» de esa regla o cita una que no lo tenga"
                    )
                raise PayloadError(f"{where}.rule", wanted)
        cases.append(
            {
                "kind": kind,
                "actor": actor,
                "protected": out["access"] is not None,
                "creates": _string(case.get("creates"), f"{where}.creates", optional=True),
                "given": given,
                "request": {
                    "method": method,
                    "path": path,
                    "body": _scalar_fields(request.get("body"), f"{where}.request.body") or None,
                    "header": header,
                },
                "response": {
                    "status": status,
                    "body": _scalar_fields(
                        response.get("body"), f"{where}.response.body", allow_items=True
                    )
                    or None,
                },
                "rule": rule,
            }
        )
    out["cases"] = cases
    return out


# --- the state line --------------------------------------------------------------------------


def _reason(data: Mapping, state: str) -> str | None:
    """`reason`: required (and non-empty) for `pospuesta`, refused for the other states."""
    reason = data.get("reason")
    if state != "pospuesta":
        if reason is not None:
            raise PayloadError(
                "reason",
                f"«reason» solo vale para «pospuesta»; con «{state}» no lleva razón",
            )
        return None
    if reason is None:
        raise PayloadError("reason", "«pospuesta» necesita «reason»: la razón de la pausa")
    return _string(reason, "reason")  # one non-empty line: the grammar decides the rest


def _level(value, path: str) -> str:
    """The payload names the scopes as the runtime does (`read|write|admin`)."""
    if value not in LEVEL_NAMES:
        raise PayloadError(path, f"se esperaba uno de {', '.join(LEVEL_NAMES)}; llegó {value!r}")
    return value


def _access(value) -> dict | None:
    """`access: {requires, sessions}` (section 1); omitted for a public story."""
    if value is None:
        return None
    at = "access"
    value = _check_object(value, at, {"requires", "sessions"}, {"requires", "sessions"})
    sessions = value["sessions"]
    if not isinstance(sessions, dict):
        raise PayloadError(f"{at}.sessions", "se esperaba un objeto no vacío de sesiones")
    if not sessions:
        raise PayloadError(f"{at}.sessions", "se esperaba un objeto no vacío")
    out = {}
    for token, session in sessions.items():
        where = f"{at}.sessions.{token}"
        if not token.strip():
            raise PayloadError(f"{at}.sessions", "cada sesión necesita un token no vacío")
        session = _check_object(session, where, {"subject", "scope"}, {"subject", "scope"})
        out[token] = {
            "subject": _string(session["subject"], f"{where}.subject"),
            "scope": _level(session["scope"], f"{where}.scope"),
        }
    return {"requires": _level(value["requires"], f"{at}.requires"), "sessions": out}


def _state_text(story: Mapping) -> str:
    """`pospuesta` carries its reason in parentheses, as the parser reads it."""
    if story["state"] == "pospuesta":
        return f"pospuesta ({story['reason']})"
    return story["state"]


# --- Markdown --------------------------------------------------------------------------------

MAX_LINE = 100  # ruff's line limit for the repository; the file stays greppable


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _fit(parts: list[str], limit: int = MAX_LINE) -> list[str]:
    """Pack `parts` into source lines of at most `limit` columns.

    `parts` are the exact pieces of the grammar, including the spaces that separate them, so
    `"".join(_fit(parts))` is their concatenation. A part longer than `limit` starts a new line
    and overflows it (a value is never split); continuation lines are indented two spaces.
    """
    lines: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) > limit:
            lines.append(current.rstrip())
            current = "  " + part.lstrip(" ")
            continue
        current += part
    lines.append(current.rstrip())
    return lines


def _case_lines(case: dict) -> tuple[list[str], int | None]:
    """The exact pieces of one `- C-NN …` item; the caller substitutes the ids."""
    # `Como:` only exists in a protected story; `Como: nadie` spells the 401 case explicitly.
    if case["actor"] is not None:
        marker = f"Como: {_json(case['actor'])} "
    elif case["kind"] == "error" and case["protected"]:
        marker = "Como: nadie "
    else:
        marker = ""
    if marker:
        parts = [f"- {{case_id}} [{case['kind']}]  " + marker + " Dado "]
    else:
        parts = [f"- {{case_id}} [{case['kind']}]  Dado "]
    if not case["given"]:
        parts.append("nada")
    else:
        entries = []
        for entry in case["given"]:
            text = f"{entry['thing']} {entry['id']}"
            if entry["fields"]:
                text += " " + _json(entry["fields"])
            entries.append(text)
        parts.append("; ".join(entries))
    request = case["request"]
    parts.append(f"  Cuando {request['method']} {request['path']}")
    if request["body"] is not None:
        parts.append(" " + _json(request["body"]))
    if request["header"] is not None:
        parts.append(f" con {request['header']['name']}: {request['header']['value']}")
    # `Entonces <estado> [{cuerpo}]  Regla: BR-NN` never breaks inside: a blank state is a
    # parse error, so this piece is kept whole and may overflow the column limit.
    tail = f"  Entonces {case['response']['status']}"
    if case["response"]["body"] is not None:
        tail += " " + _json(case["response"]["body"])
    if case["creates"] is not None:
        tail += f"  Crea: {case['creates']}"
    if case["rule"] is not None:
        tail += f"  Regla: {{rule_ids[{case['rule']}]}}"
    parts.append(tail)
    return _fit(parts), case["rule"]


def _indent(lines: list[str]) -> list[str]:
    return [lines[0]] + [f"  {line}" for line in lines[1:]]


def story_block(
    data: Mapping[str, object], *, story_id: str, case_ids: Sequence[str], rule_ids: Sequence[str]
) -> str:
    """The Markdown block (sections 4, 5 and 7) for one story, without trailing blank lines."""
    story = _validated(data)
    if len(case_ids) != len(story["cases"]) or len(rule_ids) != len(story["rules"]):
        raise ValueError("story_block needs one id per case and per rule")
    role = story["role"] or "Alguien"
    lines = [
        f"## {story_id} {story['title']}",
        f"{role} quiere {story['capability']} para {story['purpose']}.",
        f"Estado: {_state_text(story)}",
    ]
    if story["access"]:
        lines.append(f"Solo: {LEVEL_NAMES[story['access']['requires']]}")
        lines.append("Sesiones:")
        for token, session in story["access"]["sessions"].items():
            lines.append(
                f"- {_json(token)}: {session['subject']} ({LEVEL_NAMES[session['scope']]})"
            )
    # `SUPUESTO:`/`FUERA:`: prose lines the parser keeps as free text and the review reads.
    for row in story["assumptions"]:
        lines.append(f"SUPUESTO: {row.rstrip('.')}.")
    for row in story["left_out"]:
        lines.append(f"FUERA: {row.rstrip('.')}.")
    if story["things"]:
        lines.append("Cosas:")
        for thing in story["things"]:
            fields = []
            for entry in thing["fields"]:
                text = f"{entry['name']} ({entry['type']}"
                if entry["required"]:
                    text += ", obligatorio"
                if entry["unique"]:
                    text += ", único"
                if entry["points_to"]:
                    text += f", apunta a {entry['points_to']}"
                fields.append(text + ")")
            marker = " (tabla)" if thing["table"] else ""
            pieces = [
                part + ("," if position < len(fields) - 1 else "")
                for position, part in enumerate(fields)
            ]
            lines += _indent(_fit([f"- {thing['name']}{marker}: " + " ".join(pieces)]))
    lines.append("Casos:")
    cited: dict[int, list[str]] = {index: [] for index in range(len(story["rules"]))}
    for index, case in enumerate(story["cases"]):
        item_lines, rule = _case_lines(case)
        text = [line.replace("{case_id}", case_ids[index]) for line in item_lines]
        if rule is not None:
            text = [line.replace(f"{{rule_ids[{rule}]}}", rule_ids[rule]) for line in text]
            cited[rule].append(case_ids[index])
        lines += _indent(text)
    if story["rules"]:
        lines.append("")
        for index, rule in enumerate(story["rules"]):
            lines += _fit(
                [f"{rule_ids[index]} ", rule["text"] + ". "]
                + ([f"Error: {rule['error']}. "] if rule["error"] else [])
                + ["Casos: "]
                + [
                    case + ("." if position == len(cited[index]) - 1 else ", ")
                    for position, case in enumerate(cited[index])
                ]
            )
    return "\n".join(lines) + "\n"


# --- prepare and append ----------------------------------------------------------------------


LEFT_OUT_HEADING = "## Fuera de alcance"


@contextlib.contextmanager
def _locked(path: Path):
    """Hold an exclusive advisory lock across a read-validate-replace cycle on `path`.

    shortcut: POSIX `flock` only, so concurrent writers on one host are safe; upgrade to a portable
    lock if the file ever lives on a network share or Windows.
    """
    lock = path.parent / f".{path.name}.lock"
    with open(lock, "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def add_left_out(path: Path, text: str, *, dry_run: bool = False) -> None:
    """Append `FUERA: <text>.` to the «Fuera de alcance» section, creating it if needed.

    The whole file is validated first and written with the same temp-file + `os.replace` as
    `append_story`, so a failure leaves the file byte-for-byte unchanged.
    """
    path = Path(path)
    text = text.strip().rstrip(".").strip()
    if not text or "\n" in text:
        raise AddError("write", "«--left-out» necesita un texto de una línea")
    with _locked(path):
        _add_left_out_locked(path, text, dry_run)


def _add_left_out_locked(path: Path, text: str, dry_run: bool) -> None:
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as error:
        raise AddError("write", f"no se puede leer «{path}»: {error}") from error
    errors, _ = check_text(existing)
    if errors:
        raise _story_error("existing", errors[0])
    lines = existing.splitlines()
    heading = next(
        (
            index
            for index in reversed(range(len(lines)))
            if lines[index].strip().lower() == LEFT_OUT_HEADING.lower()
        ),
        None,
    )
    if heading is None:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        candidate = (existing + "\n" if existing.strip() else "") + (
            f"{LEFT_OUT_HEADING}\nFUERA: {text}.\n"
        )
    else:
        last = heading
        while last + 1 < len(lines) and lines[last + 1].strip().upper().startswith("FUERA:"):
            last += 1
        lines.insert(last + 1, f"FUERA: {text}.")
        candidate = "\n".join(lines) + "\n"
    errors, _ = check_text(candidate)
    if errors:
        raise _story_error("candidate", errors[0])
    if not dry_run:
        _write_atomically(path, candidate)


def _identifiers(doc: Doc) -> tuple[set[str], set[str], set[str]]:
    stories = {story.id for story in doc.stories}
    cases = {case.id for story in doc.stories for case in story.cases}
    rules = {rule.id for rule in doc.rules}
    return stories, cases, rules


def _story_error(source: str, error: StoryError) -> AddError:
    return AddError(source, str(error), error.line)


def _new_story_error(error: StoryError, existing: str) -> AddError:
    """A failure of the candidate, labelled as coming from the story being added."""
    line = error.line
    if line is not None and line > len(existing.splitlines()):
        head = "la historia nueva (línea del fichero con lo que se va a añadir)"
        return AddError("candidate", f"{head}: {error}", line)
    return AddError("candidate", f"tras añadir la historia: {error}", line)


def prepare_add(existing: str, data: Mapping[str, object], *, name: str) -> PreparedAdd:
    """Validate `existing`, append one story block in memory, and validate the result.

    Returns the candidate text, the allocated IDs, the SUPUESTO rows and the review. Nothing is
    written; `append_story` is the only writer.
    """
    errors, doc = check_text(existing)
    if doc is None or errors:
        raise _story_error("existing", errors[0])
    story_used, case_used, rule_used = _identifiers(doc)
    story_id = next_id("H", story_used)
    cases = data.get("cases") if isinstance(data, Mapping) else None
    rules = data.get("rules") if isinstance(data, Mapping) else None
    case_count = len(cases) if isinstance(cases, list) else 0
    rule_count = len(rules) if isinstance(rules, list) else 0
    case_ids = []
    for _ in range(case_count):
        case_ids.append(next_id("C", case_used))
        case_used.add(case_ids[-1])
    rule_ids = []
    for _ in range(rule_count):
        rule_ids.append(next_id("BR", rule_used))
        rule_used.add(rule_ids[-1])
    block = story_block(data, story_id=story_id, case_ids=case_ids, rule_ids=rule_ids)
    candidate = existing
    if candidate and not candidate.endswith("\n"):
        candidate += "\n"
    if candidate.strip():
        candidate += "\n"
    candidate += block
    errors, new_doc = check_text(candidate)
    if new_doc is None or errors:
        raise _new_story_error(errors[0], existing)
    if new_doc.stories[-1].state == "aprobada":
        target = new_doc
    else:
        # A proposal is stored but stays out of the spec; it must still pass the same checks and
        # compile as if the person approved it now, so nothing is deferred (design §2).
        target = _as_approved(new_doc, new_doc.stories[-1].id)
        errors, prospective = check_text(_text_as_approved(target, story_id))
        own = _in_span(target, errors, story_id) if prospective is not None else errors
        if own:
            raise _new_story_error(own[0], existing)
    try:
        compile_doc(target, name=name)
    except StoryError as error:
        raise _new_story_error(error, existing) from error
    return PreparedAdd(
        candidate=candidate,
        ids={"story": story_id, "cases": case_ids, "rules": rule_ids},
        assumptions=assumptions(new_doc),
        render=render(new_doc, only=story_id, review=NEW),
    )


def _text_as_approved(doc: Doc, story_id: str) -> str:
    """The source of `doc` with the `Estado:` line of `story_id` set to `aprobada`."""
    story = next(entry for entry in doc.stories if entry.id == story_id)
    from .english import approve_status

    lines = list(doc.lines)
    lines[story.state_line - 1] = approve_status(lines[story.state_line - 1])
    return "\n".join(lines) + ("\n" if doc.lines else "")


def _in_span(doc: Doc, errors: list[StoryError], story_id: str) -> list[StoryError]:
    """The errors found inside the story `story_id` (its own lines only)."""
    story = next(entry for entry in doc.stories if entry.id == story_id)
    lines = range(story.line, story.last_line + 1)
    return [error for error in errors if error.line in lines]


def _as_approved(doc: Doc, story_id: str) -> Doc:
    """A copy of `doc` with `story_id` approved (dataclasses are replaced, `doc` is untouched)."""
    stories = [
        replace(story, state="aprobada", reason=None) if story.id == story_id else story
        for story in doc.stories
    ]
    return Doc(
        stories=stories, rules=list(doc.rules), lines=list(doc.lines), left_out=list(doc.left_out)
    )


@dataclass
class Approval:
    """`approve_story`: the review of the story (the candidate) and what happened.

    `changed` is False when nothing was written: a dry run, or a story already approved.
    """

    story: str
    render: str
    changed: bool
    dry_run: bool
    message: str


def approve_story(path: Path, story_id: str, *, name: str, dry_run: bool = False) -> Approval:
    """Approve one `propuesta`/`pospuesta` story by rewriting only its `Estado:` line.

    Runs under the same lock as `append_story`; the candidate is checked and compiled before
    the atomic replace, so an unknown id, a story without state or a compile failure writes
    nothing. A story already approved is left untouched (idempotent).
    """
    path = Path(path)
    with _locked(path):
        return _approve_locked(path, story_id, name=name, dry_run=dry_run)


def _approve_locked(path: Path, story_id: str, *, name: str, dry_run: bool) -> Approval:
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as error:
        raise AddError("write", f"no se puede leer «{path}»: {error}") from error
    errors, doc = check_text(existing)
    if doc is None or errors:
        raise _story_error("existing", errors[0])
    story = next((entry for entry in doc.stories if entry.id == story_id), None)
    if story is None:
        raise AddError("existing", f"el documento no tiene la historia «{story_id}»")
    if story.state == "aprobada":
        return Approval(
            story_id,
            render(doc, only=story_id, review=REOPENED),
            False,
            dry_run,
            f"{story_id} ya está aprobada",
        )
    if story.state is None:
        raise AddError(
            "existing",
            f"la historia «{story_id}» no indica su «Estado:»; no se puede aprobar",
            story.line,
        )
    span = range(story.line - 1, min(story.last_line, len(doc.lines)))
    marks = [
        number
        for number in span
        if doc.lines[number].strip().lower().startswith(("estado:", "status:"))
    ]
    if len(marks) != 1:
        raise AddError(
            "existing",
            f"la historia «{story_id}» debe tener una sola línea «Estado:» en su parte "
            f"(tiene {len(marks)})",
            story.line,
        )
    from .english import approve_status

    lines = list(doc.lines)
    lines[marks[0]] = approve_status(lines[marks[0]])
    candidate = "\n".join(lines) + ("\n" if existing.endswith("\n") else "")
    errors, new_doc = check_text(candidate)
    if new_doc is None or errors:
        raise _story_error("candidate", errors[0])
    try:
        compile_doc(new_doc, name=name)
    except StoryError as error:
        raise _story_error("candidate", error) from error
    review = render(new_doc, only=story_id, review=BEING_APPROVED)
    if not dry_run:
        _write_atomically(path, candidate)
    verb = "se aprobaría" if dry_run else "aprobada"
    return Approval(story_id, review, not dry_run, dry_run, f"{verb} {story_id}")


def append_story(path: Path, data: Mapping[str, object], *, name: str) -> PreparedAdd:
    """`prepare_add` over the file at `path`, then a same-directory temp file + `os.replace`."""
    path = Path(path)
    with _locked(path):
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as error:
            raise AddError("write", f"no se puede leer «{path}»: {error}") from error
        prepared = prepare_add(existing, data, name=name)
        _write_atomically(path, prepared.candidate)
    return prepared


def _write_atomically(path: Path, content: str) -> None:
    """Write `content` to a temp file in `path`'s directory, then `os.replace` it."""
    directory = path.parent if str(path.parent) else Path(".")
    handle = None
    temporary = None
    try:
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            handle = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, StoryError) as error:
        raise AddError("write", f"no se puede escribir «{path}»: {error}") from error
    finally:
        if handle is not None:
            os.close(handle)
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


__all__ = [
    "AddError",
    "Approval",
    "approve_story",
    "PayloadError",
    "PreparedAdd",
    "add_left_out",
    "append_story",
    "assumptions",
    "next_id",
    "prepare_add",
    "story_block",
]
