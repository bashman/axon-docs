"""Parse `historias.md` (docs/requisitos-historias.md v3, sections 4, 5 and 7).

The grammar is closed, so the parser never interprets natural language and never guesses:
anything outside the forms of section 5 stops with a Spanish `StoryError` carrying the
1-based line, e.g. ``línea 12: falta «Entonces»``.

Two small pieces are reused everywhere:

- `parse_value`: a JSON-like value with unquoted keys (`{titulo: "Dune"}`), no `eval`;
- `parse_case`: `<Dado> <Cuando> <Entonces> [Crea: cosa] [Regla: BR-NN]` on one or more lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

STATUSES = {"propuesta", "aprobada", "pospuesta"}

# Section 1 of report-codex-f-design.md: the only scopes the runtime executes, in plain Spanish.
LEVELS = {"lector": "read", "editor": "write", "administrador": "admin"}
LEVEL_NAMES = {code: name for name, code in LEVELS.items()}


class StoryError(ValueError):
    """A story outside the grammar, at a known 1-based line."""

    def __init__(self, line: int, message: str):
        self.line = line
        self.message = message
        super().__init__(f"línea {line}: {message}")


# --- values -------------------------------------------------------------------------------


class _ValueParser:
    """JSON-like values with unquoted keys: objects, arrays, strings, integers, booleans, null."""

    def __init__(self, text: str, line: int):
        self.text = text
        self.pos = 0
        self.line = line

    def fail(self, message: str) -> StoryError:
        return StoryError(self.line, f"{message} (columna {self.pos + 1})")

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def value(self):
        self.skip()
        char = self.peek()
        if char == "{":
            return self.obj()
        if char == "[":
            return self.array()
        if char == '"':
            return self.string()
        sign = char in "-+" and self.text[self.pos + 1 : self.pos + 2].isdigit()
        if char.isdigit() or sign:
            return self.scalar()
        return self.keyword()

    def obj(self) -> dict:
        self.pos += 1
        out: dict = {}
        self.skip()
        if self.peek() == "}":
            self.pos += 1
            return out
        while True:
            self.skip()
            key = self.key()
            self.skip()
            if self.peek() != ":":
                raise self.fail("falta «:» en el cuerpo")
            self.pos += 1
            out[key] = self.value()
            self.skip()
            char = self.peek()
            if char == ",":
                self.pos += 1
                continue
            if char == "}":
                self.pos += 1
                return out
            raise self.fail("falta «}» al cerrar el cuerpo")

    def array(self) -> list:
        self.pos += 1
        out: list = []
        self.skip()
        if self.peek() == "]":
            self.pos += 1
            return out
        while True:
            out.append(self.value())
            self.skip()
            char = self.peek()
            if char == ",":
                self.pos += 1
                continue
            if char == "]":
                self.pos += 1
                return out
            raise self.fail("falta «]» al cerrar la lista")

    def key(self) -> str:
        char = self.peek()
        if char == '"':
            return self.string()
        start = self.pos
        while self.pos < len(self.text) and (
            self.text[self.pos].isalnum()
            or self.text[self.pos] in "_-"
            or (self.text[self.pos] == "." and self.text[self.pos + 1 : self.pos + 2].isdigit())
        ):
            self.pos += 1
        if self.pos == start:
            raise self.fail("falta el nombre del campo")
        return self.text[start : self.pos]

    def string(self) -> str:
        self.pos += 1
        out: list[str] = []
        escapes = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t"}
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == '"':
                self.pos += 1
                return "".join(out)
            if char == "\\":
                self.pos += 1
                nxt = self.peek()
                if nxt not in escapes:
                    raise self.fail(f"escape desconocido «\\{nxt}»")
                out.append(escapes[nxt])
                self.pos += 1
                continue
            out.append(char)
            self.pos += 1
        raise self.fail("string sin cerrar")

    def keyword(self):
        match = _KEYWORD.match(self.text, self.pos)
        if match is None:
            raise self.fail(f"valor desconocido «{self.text[self.pos :][:12]}»")
        self.pos = match.end()
        return {"true": True, "false": False, "null": None}[match.group(0)]

    def scalar(self):
        match = _SCALAR.match(self.text, self.pos)
        if match is None:
            raise self.fail(f"valor desconocido «{self.text[self.pos :][:12]}»")
        self.pos = match.end()
        token = match.group(0)
        return float(token) if "." in token else int(token)


_SCALAR = re.compile(r"[-+]?\d+(?:\.\d+)?+")
_KEYWORD = re.compile(r"true|false|null")


def parse_value(text: str, line: int):
    """Parse one closed value and require the whole text to be consumed."""
    parser = _ValueParser(text, line)
    value = parser.value()
    parser.skip()
    if parser.pos != len(parser.text):
        raise parser.fail(f"sobra «{parser.text[parser.pos :][:12]}»")
    return value


def split_top(text: str, separator: str) -> list[str]:
    """Split on `separator` outside strings, braces and brackets."""
    parts, depth, quote, current, index = [], 0, False, [], 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == '"':
                quote = False
            index += 1
            continue
        if char == '"':
            quote = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif depth == 0 and text.startswith(separator, index):
            parts.append("".join(current))
            current = []
            index += len(separator)
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


# --- the case grammar (section 5) ----------------------------------------------------------


@dataclass
class Given:
    thing: str
    id: str
    fields: dict
    line: int


@dataclass
class Request:
    method: str
    path: str
    body: dict | None
    header: tuple[str, str] | None
    line: int


@dataclass
class Response:
    status: int
    body: dict | None
    line: int


@dataclass
class Case:
    id: str
    kind: str
    given: list[Given]
    request: Request
    response: Response
    creates: str | None
    rule: str | None
    line: int
    actor: str | None = None  # `Como: "token"` -> token; `Como: nadie` or absent -> None
    actor_line: int = 0
    actor_spelled: bool = False  # a `Como:` marker was written (even `Como: nadie`)


_QUOTED = re.compile(r'"[^"]*"')
_VERBS = re.compile(r"(?<=\s)(Cuando|Entonces)(?=\s)")
_KIND_PREFIX = re.compile(r"^\[(feliz|error)\]\s*")
_SECTION_PREFIXES = ("## ", "# ", "Estado:", "Solo:", "Sesiones:", "Cosas:", "Casos:", "BR-")


def _split_verbs(text: str, line: int) -> tuple[str, str, str]:
    """Split `Dado … Cuando … Entonces …` into its three parts."""
    parts = _VERBS.split(text)

    def dado_of(part: str) -> str:
        # `parse_case` strips the `[feliz]`/`[error]` marker; `parse_case` callers may not.
        part = _KIND_PREFIX.sub("", part.strip())
        return part[len("Dado") :].strip()

    if len(parts) == 5 and parts[1] == "Cuando" and parts[3] == "Entonces":
        return dado_of(parts[0]), parts[2].strip(), parts[4].strip()
    if len(parts) == 3 and parts[1] == "Cuando":
        return dado_of(parts[0]), parts[2].strip(), ""
    if len(parts) == 3 and parts[1] == "Entonces":
        return dado_of(parts[0]), "", parts[2].strip()
    if len(parts) == 5:
        return dado_of(parts[0]), dado_of(parts[0]), parts[4].strip()
    if text.endswith("Entonces"):
        return dado_of(text[: -len("Entonces")]), "", ""
    if len(parts) != 5:
        raise StoryError(line, "falta «Cuando» o «Entonces»")
    raise StoryError(line, "los casos se escriben «Dado … Cuando … Entonces …» en ese orden")


def _parse_given(text: str, line: int) -> list[Given]:
    if text == "nada":
        return []
    given = []
    chunks = []
    for part in split_top(text, ";"):
        chunks.extend(split_top(part, " y "))
    for chunk in chunks:
        chunk = chunk.strip()
        match = re.fullmatch(
            r'([a-z_][a-z0-9_]*)\s+("[^"]*"|\d+)\s+(\{.*\})|([a-z_][a-z0-9_]*)\s+(\d+)',
            chunk,
        )
        if match is None:
            raise StoryError(line, f"«Dado» mal formado: «{chunk[:40]}» (separa con «;» o «y»)")
        if match.group(1) is not None:
            thing, ident, raw = match.group(1), match.group(2), match.group(3)
            if _QUOTED.fullmatch(ident):
                ident = ident[1:-1]
        else:
            thing, ident, raw = match.group(4), match.group(5), "{}"
        fields = parse_value(raw, line)
        if not isinstance(fields, dict):
            raise StoryError(line, "los datos de «Dado» deben ser un objeto {…}")
        given.append(Given(thing, ident, fields, line))
    if not given:
        raise StoryError(line, "falta «Dado»")
    return given


def _parse_cuando(text: str, line: int) -> Request:
    match = re.fullmatch(
        r"(GET|POST|PUT|PATCH|DELETE)\s+(\S+)(?:\s+(\{.*\}))?(?:\s+con\s+([^:]+):\s*(.+))?",
        text,
    )
    if match is None:
        raise StoryError(line, f"«Cuando» mal formado: «{text[:40]}»")
    method, raw_path, body_text, header, header_value = match.groups()
    # Percent-decoding comes after the grammar: a path is `\S+`, so `%2F` stays one segment.
    path = unquote(raw_path, errors="replace")
    body = parse_value(body_text, line) if body_text else None
    if body is not None and not isinstance(body, dict):
        raise StoryError(line, "el cuerpo de «Cuando» debe ser un objeto {…}")
    header_pair = None
    if header is not None:
        header_pair = (header.strip(), header_value.strip())
    if path != "/" and (path.endswith("/") or not path.startswith("/")):
        raise StoryError(line, f"ruta inválida «{path}»")
    return Request(method, path, body, header_pair, line)


def _parse_entonces(text: str, line: int) -> Response:
    match = re.fullmatch(r"(\d{3})(?:\s+(\{.*\}))?", text)
    if match is None:
        raise StoryError(line, f"«Entonces» mal formado: «{text[:40]}»")
    status = int(match.group(1))
    body = parse_value(match.group(2), line) if match.group(2) else None
    if body is not None and not isinstance(body, dict):
        raise StoryError(line, "el cuerpo de «Entonces» debe ser un objeto {…}")
    return Response(status, body, line)


_ACTOR = re.compile(r'(?:Como:\s*("(?:[^"\\]|\\.)*"|nadie)\s+)?')


def _parse_actor(match, line: int) -> str | None:
    if match is None or match.group(1) is None:
        return None
    raw = match.group(1)
    if raw == "nadie":
        return None
    value = parse_value(raw, line)
    if not isinstance(value, str) or not value:
        raise StoryError(line, "«Como:» necesita un token entre comillas o «nadie»")
    return value


def parse_case(text: str, line: int) -> Case:
    """Parse `[kind] [Como: "token" | nadie] Dado … Cuando … Entonces … [Crea:] [Regla:]`."""
    match = re.match(r"\[(feliz|error)\]\s+", text)
    if match is None:
        raise StoryError(line, "falta «[feliz]» o «[error]»")
    kind = match.group(1)
    rest = text[match.end() :]
    actor_match = _ACTOR.match(rest)
    actor = _parse_actor(actor_match, line)
    actor_spelled = bool(actor_match and actor_match.group(1))
    rest = rest[actor_match.end() :]
    rule = None
    rule_match = re.search(r"\s+Regla:\s*(BR-\d+)\s*$", rest)
    if rule_match is not None:
        rule = rule_match.group(1)
        rest = rest[: rule_match.start()]
    creates = None
    creates_match = re.search(r"\s+Crea:\s*([a-z_][a-z0-9_]*)\s*$", rest)
    if creates_match is not None:
        creates = creates_match.group(1)
        rest = rest[: creates_match.start()]
    dado, cuando, entonces = _split_verbs(rest, line)
    if not cuando:
        raise StoryError(line, "falta «Cuando»")
    if not entonces:
        raise StoryError(line, "«Entonces» mal formado: falta el estado")
    return Case(
        id="",
        kind=kind,
        given=_parse_given(dado, line),
        request=_parse_cuando(cuando, line),
        response=_parse_entonces(entonces, line),
        creates=creates,
        rule=rule,
        line=line,
        actor=actor,
        actor_line=line,
        actor_spelled=actor_spelled,
    )


# --- the story template (sections 4 and 7) -------------------------------------------------


@dataclass
class Thing:
    name: str
    # (campo, tipo, obligatorio, apunta a, único)
    fields: list[tuple[str, str, bool, str | None, bool]]
    line: int
    raw: list[str] = field(default_factory=list)
    table: bool = False  # the `(tabla)` marker: only a table thing is stored as a table


@dataclass
class Story:
    id: str
    title: str
    role: str
    state: str | None
    things: list[Thing]
    cases: list[Case]
    line: int
    free_text: list[tuple[int, str]] = field(default_factory=list)  # (línea, texto) de la prosa
    last_line: int = 0  # last line that belongs to the story (title included)
    reason: str | None = None  # the `(razón)` of `Estado: pospuesta (razón)`
    state_line: int = 0  # the line of `Estado:` (0 when the story has none)
    requires: str | None = None  # `Solo:` as a runtime scope (`read`/`write`/`admin`)
    requires_line: int = 0
    sessions: list[tuple[str, str, str]] = field(default_factory=list)  # (token, sujeto, scope)
    sessions_line: int = 0


@dataclass
class Rule:
    id: str
    text: str
    code: str | None  # None: a behaviour rule, backed by a [feliz] case
    cases: list[str]
    line: int


@dataclass
class Doc:
    stories: list[Story] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # the source lines, for span checks
    # (línea, texto) of the «Fuera de alcance» section
    left_out: list[tuple[int, str]] = field(default_factory=list)


_STORY_HEAD = re.compile(r"##\s+(H-\d+)\s+(.+?)\s*$")
# Greedy text: the last `. Casos:` is the delimiter; an `Error:` tail is split off afterwards so a
# malformed code is rejected instead of being absorbed into a behaviour rule's text.
_RULE_LINE = re.compile(r"(BR-\d+)\s+(.+)\.\s+Casos:\s*(.*?)\.?\s*$")
_RULE_ERROR = re.compile(r"(.+)\.\s+Error:\s*(.*)$")
_THING = re.compile(r"([a-z_][a-z0-9_]*)\s*(?:\(\s*tabla\s*\))?\s*:\s*(.+)$")


def _parse_things(text: str, line: int) -> list[Thing]:
    """`libro: titulo (texto, obligatorio), autor (texto)`, or `(ninguna)`."""
    if text.strip() == "(ninguna)":
        return []
    if not text.strip():
        return [Thing("", [], line)]
    return _split_things(split_top(text, ";"), line)


def _paren_split(text: str) -> list[str]:
    """Split on commas that are outside parentheses (`titulo (texto, obligatorio)`)."""
    parts, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


_FIELD_ITEM = re.compile(
    r"\s*([a-z_][a-z0-9_]*)\s*\("
    r"\s*(texto|numero|número|entero)\s*"
    r"(?:,\s*obligatorio)?"
    r"(?:,\s*único)?"
    r"(?:,\s*apunta a\s+([a-z_][a-z0-9_]*)\s*)?"
    r"\)\s*"
)


def _split_things(chunks: list[str], line: int) -> list[Thing]:
    things = []
    for raw in chunks:
        match = _THING.fullmatch(raw.strip())
        if match is None:
            raise StoryError(line, f"«Cosas» mal formado: «{raw.strip()[:40]}»")
        fields = []
        for part in _paren_split(match.group(2)):
            item = _FIELD_ITEM.fullmatch(part)
            if item is None:
                raise StoryError(
                    line,
                    f"«{part.strip()}»: cada campo se escribe «nombre (tipo [, obligatorio] "
                    "[, único] [, apunta a cosa])», en ese orden",
                )
            text = part
            kind = "string" if item.group(2) == "texto" else "i64"
            fields.append(
                (item.group(1), kind, ", obligatorio" in text, item.group(3), ", único" in text)
            )
        things.append(Thing(match.group(1), fields, line, table="(tabla)" in raw.split(":")[0]))
    return things


_POSTPONED = re.compile(r"pospuesta\s*(?:\(\s*(.*?)\s*\))?\s*")


def _parse_state(text: str, line: int) -> tuple[str, str | None]:
    """`aprobada`, `propuesta` or `pospuesta (razón)` -> `(estado, razón)`.

    A legacy `pospuesta` without a reason parses (the reason `None`); `check` demands it.
    """
    if text in STATUSES:
        return text, None
    postponed = _POSTPONED.fullmatch(text)
    if postponed is None:
        raise StoryError(line, f"estado desconocido «{text}»")
    reason = postponed.group(1)
    return "pospuesta", reason or None


def state_text(story: Story) -> str:
    """The `Estado:` value as the file writes it: `pospuesta` carries its reason."""
    if story.state is None:
        return ""
    if story.state == "pospuesta" and story.reason:
        return f"pospuesta ({story.reason})"
    return story.state


def _story_id_from_case(text: str, line: int) -> str:
    match = re.match(r"(C-\d+)", text)
    if match is None:
        raise StoryError(line, "falta el identificador del caso («C-01»)")
    return match.group(1)


def parse(text: str) -> Doc:
    """Parse a whole `historias.md`: stories in document order, then the rules."""
    from .english import normalize

    lines = normalize(text).splitlines()
    doc = Doc(lines=text.splitlines())
    index = 0
    story: Story | None = None
    things_open = False
    in_cases = False
    while index < len(lines):
        raw = lines[index]
        number = index + 1
        line = raw.strip()
        index += 1
        if not line or line.startswith("###"):
            continue
        # Document-level section: what the person asked for but no story can hold.
        if line.lower().startswith(_OUT_OF_SCOPE):
            story = None
            in_cases = False
            things_open = False
            index = _parse_left_out(lines, index, doc)
            continue
        head = _STORY_HEAD.match(line)
        if head is not None:
            if story is not None:
                # A heading closes the previous slice, even with no blank line before it.
                story.last_line = max(story.last_line, number - 1)
            story = Story(head.group(1), head.group(2), "alguien", None, [], [], number)
            doc.stories.append(story)
            in_cases = False
            continue
        rule = _RULE_LINE.match(line)
        if rule is None and re.match(r"BR-\d+\s*$|BR-\d+\s", line):
            # A rule that does not fit one line: join its indented continuation lines (as a case
            # does) and parse the joined rule again.
            joined = line
            while index < len(lines) and lines[index].startswith((" ", "\t")):
                joined += " " + lines[index].strip()
                index += 1
            line = joined
            rule = _RULE_LINE.match(joined)
            if rule is None:
                raise StoryError(number, f"regla mal formada: «{joined[:40]}»")
        if rule is not None:
            story = None
            cases = [c.strip().rstrip(".") for c in rule.group(3).split(",") if c.strip()]
            text, code = rule.group(2), None
            tail = _RULE_ERROR.match(text)
            if tail is not None:
                text, code = tail.group(1), tail.group(2)
                if not re.fullmatch(r"[a-z_]+", code):
                    raise StoryError(number, f"código de error no válido: «{code}»")
            doc.rules.append(Rule(rule.group(1), text, code, cases, number))
            in_cases = False
            continue
        if story is None:
            raise StoryError(number, "texto fuera de una historia o una regla")
        story.last_line = max(story.last_line, number)
        # The markers compare the stripped line: a title or a prose line that merely *starts*
        # with `Estado:`/`Cosas:` must not swallow the following lines.
        if line.startswith("Estado:"):
            state, reason = _parse_state(line[len("Estado:") :].strip(), number)
            story.state = state
            story.reason = reason
            story.state_line = number
            continue
        if line.startswith("Solo:"):
            if story.requires_line:
                raise StoryError(
                    number,
                    f"la historia «{story.id}» ya dice «Solo:» en la línea "
                    f"{story.requires_line}; una historia tiene una sola política",
                )
            level = line[len("Solo:") :].strip()
            if level not in LEVELS:
                raise StoryError(
                    number,
                    f"nivel desconocido «{level}»; se escribe «Solo: lector», «Solo: editor» o "
                    "«Solo: administrador»",
                )
            story.requires = LEVELS[level]
            story.requires_line = number
            in_cases = False
            continue
        if line == "Sesiones:":
            if story.sessions_line:
                raise StoryError(
                    number,
                    f"la historia «{story.id}» ya dice «Sesiones:» en la línea "
                    f"{story.sessions_line}; una historia tiene una sola lista de sesiones",
                )
            story.sessions, story.sessions_line, index = _parse_sessions(
                lines, index, number=number
            )
            in_cases = False
            # a blank line or a `###` note already closed the block: skip the blank too
            while index < len(lines) and not lines[index].strip():
                index += 1
            continue
        if line.startswith("Cosas:"):
            story.things = _parse_things(line[len("Cosas:") :], number)
            things_open = True
            in_cases = False
            continue
        if line.startswith("## ") or line.startswith("# "):
            story = None
            continue
        if line == "Casos:":
            things_open = False
            in_cases = True
            continue
        if line.startswith("- ") and not in_cases and things_open:
            # A `Cosas:` header followed by unindented `- libro: …` lines (section 4).
            if not story.things or story.things[-1].name:
                story.things.append(Thing("", [], number))
            placeholder = story.things[-1]
            placeholder.raw.append(line[2:])
            story.things = story.things[:-1] + _split_things(placeholder.raw, number)
            continue
        if line.startswith("- "):
            if not in_cases:
                raise StoryError(number, "caso fuera de «Casos:»")
            case = _parse_case_lines(lines, index - 1)
            index = case[1]
            story.last_line = max(story.last_line, index)
            story.cases.append(case[0])
            continue
        if line.startswith(("SUPUESTO:", "FUERA:")):
            story.free_text.append((number, line))
            continue
        prose = _PROSE.match(line)
        if prose is not None:
            story.role = prose.group(1).strip()
            story.free_text.append((number, line))
            in_cases = False
            continue
        raise StoryError(number, f"línea desconocida: «{line[:40]}»")
    # A story's span ends at its last non-empty line (a blank line or a rule ends the slice), and
    # never crosses into the next one: a heading closes the previous slice with no blank line.
    for position, entry in enumerate(doc.stories):
        limit = (
            doc.stories[position + 1].line - 1 if position + 1 < len(doc.stories) else len(lines)
        )
        while (
            entry.last_line < limit
            and lines[entry.last_line].strip()
            and not _RULE_LINE.match(lines[entry.last_line].strip())
        ):
            entry.last_line += 1
    return doc


_LEVELS_TEXT = "|".join(LEVELS)
# The token is a JSON string, the same value `Como:` reads: one encoding at both places.
_SESSION = re.compile(
    r"-\s*(" + r'"(?:[^"\\]|\\.)*"' + r")\s*:\s*(.+?)\s*\(" + "(" + _LEVELS_TEXT + r")\)\s*$"
)


def _parse_sessions(lines: list[str], index: int, *, number: int) -> tuple[list, int, int]:
    """The `- "token": nombre humano (nivel)` lines that start at `index`; `(rows, at, next)`.

    A blank line or a heading closes the block: it never reaches into the next section.
    """
    rows = []
    first = 0
    while index < len(lines):
        item = lines[index].strip()
        if not item or item.startswith("#"):
            break
        if not item.startswith("- "):
            break
        match = _SESSION.fullmatch(item)
        token = None
        if match is not None:
            token = parse_value(match.group(1), index + 1)
        if match is None or not isinstance(token, str) or not token or not match.group(2):
            raise StoryError(
                index + 1,
                f"«Sesiones» mal formada: «{item[:40]}» "
                '(se escribe «- "token": Nombre humano (lector|editor|administrador)»)',
            )
        rows.append((token, match.group(2), LEVELS[match.group(3)]))
        first = first or index + 1
        index += 1
    if not rows:
        raise StoryError(number, "«Sesiones:» sin sesiones; escribe al menos una")
    return rows, first, index


_PROSE = re.compile(r"(.+?)\s+quiere\s+(.+?)\s+para\s+(.+?)\.?\s*$")
_OUT_OF_SCOPE = "## fuera de alcance"


def _parse_left_out(lines: list[str], index: int, doc: Doc) -> int:
    """Read the `FUERA: <texto>.` lines that follow the «Fuera de alcance» heading."""
    while index < len(lines):
        line = lines[index].strip()
        if line.lower().startswith("## ") or line.lower().startswith("# "):
            break
        index += 1
        if not line:
            continue
        if not line.upper().startswith("FUERA:"):
            raise StoryError(
                index, f"en «Fuera de alcance» solo van líneas «FUERA: …»: «{line[:40]}»"
            )
        text = line[len("FUERA:") :].strip().rstrip(".").strip()
        if not text:
            raise StoryError(index, "«FUERA:» sin texto")
        doc.left_out.append((index, text))
    return index


def _parse_case_lines(lines: list[str], start: int) -> tuple[Case, int]:
    """Join the continuation lines of a case that starts at `lines[start]`."""
    # The case body starts after the id; its own leading spaces are only indentation.
    first = lines[start].strip()[len("- ") :]
    case_id = _story_id_from_case(first, start + 1)
    text = first[len(case_id) :].strip()
    if not text.startswith("[feliz]") and not text.startswith("[error]"):
        raise StoryError(start + 1, "falta «[feliz]» o «[error]»")
    index = start + 1
    while index < len(lines) and lines[index].strip().startswith("Regla: BR-"):
        text = f"{text} {lines[index].strip()}"
        index += 1
    while True:
        try:
            case = parse_case(text, start + 1)
        except StoryError as error:
            # A case split over lines may be missing its second half: join the next line.
            incomplete = error.message.startswith("«Entonces» mal formado")
            if (
                not error.message.startswith("falta «Cuando»")
                and not error.message.startswith("falta «Entonces»")
                and not incomplete
            ):
                raise
            while index < len(lines) and lines[index].strip().startswith("###"):
                index += 1  # a `###` note is skipped by `parse()`; it does not end a case
            if (
                index >= len(lines)
                or not lines[index].strip()
                or lines[index].strip().startswith("- ")
                # The next line reads as a new section: the case really lacks its tail.
                or any(lines[index].strip().startswith(prefix) for prefix in _SECTION_PREFIXES)
            ):
                raise error
            text = f"{text} {lines[index].strip()}"
            index += 1
            continue
        break
    case.id = case_id
    return case, index
