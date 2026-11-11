"""Deterministic checks of a `historias.md` (sections 7 and 10 A1.4/A1.5).

Every finding is a Spanish `StoryError` with its line: the program reports what the
document says, it never repairs it.
"""

from __future__ import annotations

from .parse import LEVEL_NAMES, Doc, StoryError

_RANK = {"read": 0, "write": 1, "admin": 2}


def _cross_references(doc: Doc, cases_by_id: dict, rules_by_id: dict) -> list[StoryError]:
    """Each side of a case <-> rule link must name the other (section 7)."""
    errors: list[StoryError] = []
    for story in doc.stories:
        for case in story.cases:
            rule = rules_by_id.get(case.rule)
            if rule is not None and case.id not in rule.cases:
                errors.append(
                    StoryError(
                        case.line,
                        f"el caso «{case.id}» cita la regla «{rule.id}», que no lo lista",
                    )
                )
    for rule in doc.rules:
        for case_id in rule.cases:
            case = cases_by_id.get(case_id)
            if case is not None and case.rule != rule.id:
                errors.append(
                    StoryError(
                        rule.line,
                        f"la regla «{rule.id}» cita el caso «{case_id}», que cita otra regla",
                    )
                )

    return errors


def check(doc: Doc) -> list[StoryError]:
    """All problems in `doc`, in document order; an empty list means it is consistent."""
    errors: list[StoryError] = []

    story_ids: dict[str, int] = {}
    case_ids: dict[str, int] = {}
    rule_ids: dict[str, int] = {}
    cases_by_id = {}
    rules_by_id = {rule.id: rule for rule in doc.rules}
    things: dict[str, dict[str, tuple[str, bool, int, str | None]]] = {}
    targets: dict[str, str] = {}  # campo único -> cosa a la que apunta
    stored: dict[str, bool] = {}  # cosa -> se guarda como tabla
    unique_fields: dict[str, list[str]] = {}  # cosa -> campos únicos
    for story in doc.stories:
        for thing in story.things:
            if thing.name:
                previous_table = stored.get(thing.name)
                if previous_table is not None and previous_table != thing.table:
                    errors.append(
                        StoryError(
                            thing.line,
                            f"la cosa «{thing.name}» se declara una vez con «(tabla)» y otra "
                            "sin él; el marcador es el mismo en todas sus declaraciones",
                        )
                    )
                stored.setdefault(thing.name, thing.table)
            fields = things.setdefault(thing.name, {})
            for name, kind, required, target, unique in thing.fields:
                if name == "id":
                    errors.append(
                        StoryError(
                            thing.line,
                            f"la cosa «{thing.name}» no declara el campo «id»: cada cosa ya "
                            "recibe su número automático",
                        )
                    )
                if unique and not thing.table:
                    errors.append(
                        StoryError(
                            thing.line,
                            f"el campo «{name}» de «{thing.name}» es único, pero la cosa no se "
                            "guarda como tabla; marca la cosa con «(tabla)»",
                        )
                    )
                if unique:
                    unique_fields.setdefault(thing.name, []).append(name)
                previous = fields.get(name)
                if previous is not None and previous[:2] != (kind, required):
                    errors.append(
                        StoryError(
                            thing.line,
                            f"el campo «{name}» de la cosa «{thing.name}» contradice su "
                            f"declaración de la línea {previous[2]} "
                            "(tipo u obligatoriedad distintos)",
                        )
                    )
                elif previous is not None and previous[3] != target:
                    before = previous[3] or "nada"
                    now = target or "nada"
                    errors.append(
                        StoryError(
                            thing.line,
                            f"el campo «{name}» de la cosa «{thing.name}» apunta a «{now}» y "
                            f"en la línea {previous[2]} apuntaba a «{before}»; un campo apunta "
                            "siempre a lo mismo",
                        )
                    )
                fields.setdefault(name, (kind, required, thing.line, target))
            for name, _kind, _required, target, _unique in thing.fields:
                if target is not None:
                    targets.setdefault(name, target)
    # Relations are declarative: the target must exist and the field must be a number.
    for story in doc.stories:
        for thing in story.things:
            for name, kind, _, target, _unique in thing.fields:
                if target is None:
                    continue
                if kind != "i64":
                    errors.append(
                        StoryError(
                            thing.line,
                            f"el campo «{name}» de la cosa «{thing.name}» apunta a «{target}», "
                            "pero solo un campo de número puede apuntar a otra cosa",
                        )
                    )
                if target not in things:
                    errors.append(
                        StoryError(
                            thing.line,
                            f"el campo «{name}» de la cosa «{thing.name}» apunta a «{target}», "
                            "que no está declarada en «Cosas» de ninguna historia",
                        )
                    )
                elif thing.table and not stored[target]:
                    errors.append(
                        StoryError(
                            thing.line,
                            f"el campo «{name}» de la cosa «{thing.name}» apunta a «{target}», "
                            "pero el destino no se guarda como tabla; marca la cosa destino "
                            "con «(tabla)»",
                        )
                    )
    for story in doc.stories:
        if story.id in story_ids:
            errors.append(StoryError(story.line, f"la historia «{story.id}» está repetida"))
        story_ids.setdefault(story.id, story.line)
        for case in story.cases:
            if case.id in case_ids:
                errors.append(StoryError(case.line, f"el caso «{case.id}» está repetido"))
            case_ids.setdefault(case.id, case.line)
            cases_by_id.setdefault(case.id, case)
            if case.creates is not None and case.creates not in things:
                errors.append(
                    StoryError(
                        case.line,
                        f"la cosa «{case.creates}» de «Crea» no está declarada en «Cosas» de "
                        f"ninguna historia; la usa el caso «{case.id}» de la historia «{story.id}»",
                    )
                )
            for given in case.given:
                if given.thing not in things:
                    errors.append(
                        StoryError(
                            case.line,
                            f"el caso «{case.id}» de la historia «{story.id}» usa la cosa "
                            f"«{given.thing}» en «Dado», pero no está declarada en «Cosas» "
                            "de ninguna historia",
                        )
                    )
            # A `Dado` relation value names an instance of this very case: the review would
            # otherwise show «en poder de socio 2» with no socio 2. `Cuando` is not checked: a
            # request may deliberately point at a missing thing (the 404 case).
            instances = {(entry.thing, entry.id) for entry in case.given}
            for entry in case.given:
                for name, value in entry.fields.items():
                    target = targets.get(name)
                    if target is None or value is None or not isinstance(value, int):
                        continue
                    if (target, str(value)) not in instances:
                        errors.append(
                            StoryError(
                                case.line,
                                f"el campo «{name}» apunta a «{target} {value}», pero el caso "
                                f"«{case.id}» no da ese {target} en «Dado»",
                            )
                        )
    errors += _table_cases(doc, stored, unique_fields, rules_by_id)
    for rule in doc.rules:
        if rule.id in rule_ids:
            errors.append(StoryError(rule.line, f"la regla «{rule.id}» está repetida"))
        rule_ids.setdefault(rule.id, rule.line)

    # Every BR/C reference exists, in both directions.
    for rule in doc.rules:
        for case_id in rule.cases:
            if case_id not in case_ids:
                errors.append(
                    StoryError(
                        rule.line, f"la regla «{rule.id}» cita un caso que no existe: {case_id}"
                    )
                )
    for story in doc.stories:
        for case in story.cases:
            if case.rule is not None and case.rule not in rule_ids:
                errors.append(
                    StoryError(
                        case.line, f"el caso «{case.id}» cita una regla que no existe: {case.rule}"
                    )
                )

    # A rule needs at least one case that names it; an [error] case needs its rule.
    used: dict[str, set[str]] = {rule.id: set() for rule in doc.rules}
    for case in cases_by_id.values():
        if case.rule in used:
            used[case.rule].add(case.id)
    for rule in doc.rules:
        if not rule.cases:
            errors.append(StoryError(rule.line, f"la regla «{rule.id}» no tiene ningún caso"))
        elif not used[rule.id]:
            errors.append(
                StoryError(rule.line, f"la regla «{rule.id}» no la cita ningún caso")
            )

    # Section 7: a rule and a case must name each other, not just exist.
    errors += _cross_references(doc, cases_by_id, rules_by_id)
    for story in doc.stories:
        for case in story.cases:
            rule = rules_by_id.get(case.rule)
            if rule is not None and case.kind == "feliz" and rule.code is not None:
                errors.append(
                    StoryError(
                        case.line, f"el caso feliz «{case.id}» cita una regla de error «{rule.id}»"
                    )
                )
            if rule is not None and case.kind == "error" and rule.code is None:
                errors.append(
                    StoryError(
                        case.line,
                        f"el caso de error «{case.id}» cita una regla sin error «{rule.id}»",
                    )
                )
            if case.kind == "error" and case.rule is None:
                errors.append(
                    StoryError(
                        case.line,
                        f"el caso de error «{case.id}» no indica su regla (Regla: BR-NN)",
                    )
                )

    errors += _permissions(doc)

    # A postponed story says why; the reason is what makes `approve` possible later.
    for story in doc.stories:
        if story.state == "pospuesta" and not story.reason:
            errors.append(
                StoryError(
                    story.state_line or story.line,
                    f"la historia «{story.id}» está pospuesta y no dice la razón "
                    "(Estado: pospuesta (razón))",
                )
            )

    # An approved story cannot keep an open question, anywhere in its span (section 7).
    for story in doc.stories:
        if story.state != "aprobada":
            continue
        for line_number in range(story.line, story.last_line + 1):
            if "PENDIENTE:" in doc.lines[line_number - 1]:
                errors.append(
                    StoryError(
                        line_number, f"la historia «{story.id}» está aprobada y tiene un PENDIENTE:"
                    )
                )
    return errors


def _rows(case, thing_name: str) -> list:
    return [entry for entry in case.given if entry.thing == thing_name]


def _same_value(left, right) -> bool:
    """Exact typed equality (case-sensitive text, i64 numbers): no normalisation."""
    return type(left) is type(right) and left == right


def _table_cases(
    doc: Doc, stored: dict[str, bool], unique: dict[str, list[str]], rules: dict
) -> list[StoryError]:
    """Seeds of a table thing cannot repeat an id or a unique value (section B, «Unicidad»)."""
    errors: list[StoryError] = []
    for story in doc.stories:
        for case in story.cases:
            for thing_name, is_table in stored.items():
                if not is_table:
                    continue
                # One row per id: a row may be completed in several `Dado` chunks.
                rows: dict[str, dict] = {}
                for entry in _rows(case, thing_name):
                    rows.setdefault(entry.id, {}).update(entry.fields)
                for field in unique.get(thing_name, []):
                    seen_values = []
                    for row_id, fields in rows.items():
                        if field not in fields:
                            continue  # an absent optional value never collides (like NULL)
                        value = fields[field]
                        for other_id, other in seen_values:
                            if _same_value(value, other):
                                errors.append(
                                    StoryError(
                                        case.line,
                                        f"el caso «{case.id}» repite en «{field}» el valor "
                                        f"{value!r} ({thing_name} {other_id} y {thing_name} "
                                        f"{row_id}); «{field}» no se repite",
                                    )
                                )
                        seen_values.append((row_id, value))
            errors += _collision(case, unique, rules)
    return errors


def _collision(case, unique: dict[str, list[str]], rules: dict) -> list[StoryError]:
    """A `Crea:` that sends a unique value already seeded is the 409 conflict case, nothing else."""
    if case.creates is None or not case.request.body:
        return []
    errors: list[StoryError] = []
    for field in unique.get(case.creates, []):
        if field not in case.request.body:
            continue
        value = case.request.body[field]
        seeded = any(
            _same_value(row.fields.get(field), value) and field in row.fields
            for row in _rows(case, case.creates)
        )
        if not seeded:
            continue
        code = (case.response.body or {}).get("error")
        rule = rules[case.rule].code if case.rule in rules else None
        problems = []
        if case.kind != "error":
            problems.append("es un caso feliz")
        if case.response.status != 409:
            problems.append(f"responde {case.response.status}")
        if code != "conflict":
            problems.append(f"responde «{code}»")
        if rule != "conflict":
            problems.append(f"cita la regla «{case.rule}»")
        if problems:
            errors.append(
                StoryError(
                    case.line,
                    f"el caso «{case.id}» crea {case.creates} con «{field}» igual a una fila de "
                    f"«Dado»: se escribe [error], 409 {{error: \"conflict\"}} y una regla de "
                    f"«conflict» (ahora {', '.join(problems)})",
                )
            )
    return errors


def _permissions(doc: Doc) -> list[StoryError]:
    """One access policy per story (section 1 of report-codex-f-design.md), checked per case.

    A story without `Solo:` is public: no sessions and no `Como:`. A protected story needs a
    session whose scope reaches `Solo:` in a happy case and, in an `[error]` case, a real
    rejection: 401 without a session (`Como: nadie`) or 403 with a valid but insufficient one.
    """
    errors: list[StoryError] = []
    rule_codes = {rule.id: rule.code for rule in doc.rules}
    sessions: dict[str, tuple[str, str, int]] = {}  # token -> (sujeto, alcance, línea)
    for story in doc.stories:
        # Only an approved story reaches the compiled table (`approved_doc`): a proposal's
        # sessions neither compile nor conflict, so a draft may reuse a token.
        for token, subject, scope in story.sessions if story.state == "aprobada" else ():
            previous = sessions.get(token)
            if previous is not None and previous[:2] != (subject, scope):
                errors.append(
                    StoryError(
                        story.sessions_line,
                        f"el token de «{subject}» ya se usa en la línea {previous[2]} como "
                        f"«{previous[0]}» ({LEVEL_NAMES[previous[1]]}); un token es siempre la "
                        "misma persona y el mismo nivel",
                    )
                )
            sessions.setdefault(token, (subject, scope, story.sessions_line))
        if story.requires is None:
            if story.sessions:
                errors.append(
                    StoryError(
                        story.sessions_line,
                        f"la historia «{story.id}» declara «Sesiones:» pero no dice «Solo:» "
                        "(lector, editor o administrador)",
                    )
                )
            for case in story.cases:
                if case.actor is not None:
                    errors.append(
                        StoryError(
                            case.line,
                            f"el caso «{case.id}» dice «Como:» pero la historia «{story.id}» es "
                            "pública (no tiene «Solo:»)",
                        )
                    )
            continue
        authorised = rejection = False
        for case in story.cases:
            if case.actor is not None and case.actor not in {t for t, _, _ in story.sessions}:
                errors.append(
                    StoryError(
                        case.line,
                        f"el caso «{case.id}» usa la sesión «{case.actor}», que no está en "
                        f"«Sesiones:» de la historia «{story.id}»",
                    )
                )
                continue
            held = next(
                (scope for token, _, scope in story.sessions if token == case.actor), None
            )
            reaches = held is not None and _RANK[held] >= _RANK[story.requires]
            if case.kind == "feliz":
                if reaches:
                    authorised = True
                else:
                    who = "sin identificarse" if case.actor is None else f"como «{case.actor}»"
                    errors.append(
                        StoryError(
                            case.line,
                            f"el caso feliz «{case.id}» va {who}, que no tiene el permiso de "
                            f"{LEVEL_NAMES[story.requires]} que pide la historia «{story.id}»",
                        )
                    )
                continue
            status = case.response.status
            if status not in (401, 403):
                continue
            rejection = True
            code = (case.response.body or {}).get("error")
            expected = "unauthorized" if status == 401 else "forbidden"
            if code != expected:
                errors.append(
                    StoryError(
                        case.line,
                        f"una respuesta {status} se explica con la regla «{expected}»; el caso "
                        f"«{case.id}» responde «{code}»",
                    )
                )
            if case.rule is not None and rule_codes.get(case.rule) != expected:
                errors.append(
                    StoryError(
                        case.line,
                        f"el caso «{case.id}» responde {status} y cita la regla «{case.rule}», "
                        f"que no es de «{expected}»",
                    )
                )
            if status == 401 and held is not None:
                errors.append(
                    StoryError(
                        case.line,
                        f"el caso «{case.id}» responde 401 con la sesión «{case.actor}»; sin "
                        "identificarse se escribe «Como: nadie»",
                    )
                )
            elif status == 403 and held is None:
                errors.append(
                    StoryError(
                        case.line,
                        f"el caso «{case.id}» responde 403 sin sesión; sin identificarse el "
                        "rechazo es 401",
                    )
                )
            elif status == 403 and reaches:
                errors.append(
                    StoryError(
                        case.line,
                        f"el caso «{case.id}» responde 403 con la sesión «{case.actor}», que sí "
                        f"tiene el permiso de {LEVEL_NAMES[story.requires]}",
                    )
                )
        if not authorised and any(case.kind == "feliz" for case in story.cases):
            errors.append(
                StoryError(
                    story.requires_line,
                    f"la historia «{story.id}» pide el permiso de "
                    f"{LEVEL_NAMES[story.requires]} pero ningún caso feliz lo tiene",
                )
            )
        if not rejection:
            errors.append(
                StoryError(
                    story.requires_line,
                    f"la historia «{story.id}» pide el permiso de "
                    f"{LEVEL_NAMES[story.requires]} pero ningún caso muestra el rechazo "
                    "(401 o 403)",
                )
            )
    return errors


def check_text(text: str) -> tuple[list[StoryError], Doc | None]:
    """Parse then check; parse errors are returned as the (usually single) finding."""
    from .parse import parse

    try:
        doc = parse(text)
    except StoryError as error:
        return [error], None
    return check(doc), doc
