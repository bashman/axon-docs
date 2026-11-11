"""Normalize the English story surface syntax to AXON's Spanish parser grammar."""

from __future__ import annotations

import re


def is_english(text: str) -> bool:
    return (
        bool(
            re.search(r"(?im)^\s*(?:Status:|Things:|Cases:|Only:|Sessions:|## Out of scope)", text)
        )
        or bool(re.search(r"(?im)^\s*-\s*C-\d+.*\b(?:Given|When|Then)\b", text))
        or bool(re.search(r"(?i)\[(?:happy|error)\].*\bGiven\b", text))
    )


def _outside_quotes(text: str, replacements: tuple[tuple[str, str], ...]) -> str:
    parts = re.split(r'("(?:[^"\\]|\\.)*")', text)
    for index in range(0, len(parts), 2):
        for pattern, replacement in replacements:
            parts[index] = re.sub(pattern, replacement, parts[index], flags=re.IGNORECASE)
    return "".join(parts)


_CASE_WORDS = (
    (r"\bGiven\b", "Dado"),
    (r"\bWhen\b", "Cuando"),
    (r"\bThen\b", "Entonces"),
    (r"\bCreates:", "Crea:"),
    (r"\bRule:", "Regla:"),
    (r"\bAs:", "Como:"),
    (r"\bnobody\b", "nadie"),
    (r"\bnothing\b", "nada"),
    (r"\[happy\]", "[feliz]"),
)
_THING_WORDS = (
    (r"\(\s*table\s*\)", "(tabla)"),
    (r"\breferences\b", "apunta a"),
    (r"\brequired\b", "obligatorio"),
    (r"\bunique\b", "único"),
    (r"\btext\b", "texto"),
    (r"\bnumber\b", "numero"),
)


def normalize(text: str) -> str:
    """Translate only structural English keywords, never quoted values or prose."""
    if not is_english(text):
        return text
    lines = text.splitlines()
    result: list[str] = []
    things = False
    sessions = False
    for raw in lines:
        stripped = raw.strip()
        line = raw
        if re.match(r"##\s+Out of scope\s*$", stripped, re.IGNORECASE):
            line = re.sub(r"Out of scope", "Fuera de alcance", raw, flags=re.IGNORECASE)
            things = False
        elif re.match(r"Status:\s*", stripped, re.IGNORECASE):
            line = re.sub(r"Status:", "Estado:", raw, count=1, flags=re.IGNORECASE)
            state = re.match(r"(\s*Estado:\s*)(approved|proposed|postponed)\b", line, re.IGNORECASE)
            if state:
                translated = {
                    "approved": "aprobada",
                    "proposed": "propuesta",
                    "postponed": "pospuesta",
                }[state.group(2).lower()]
                line = line[: state.start(2)] + translated + line[state.end(2) :]
        elif re.match(r"Things:\s*", stripped, re.IGNORECASE):
            line = re.sub(r"Things:", "Cosas:", raw, count=1, flags=re.IGNORECASE)
            line = _outside_quotes(line, _THING_WORDS)
            things = True
            sessions = False
        elif re.match(r"Cases:\s*$", stripped, re.IGNORECASE):
            line = re.sub(r"Cases:", "Casos:", raw, count=1, flags=re.IGNORECASE)
            things = False
            sessions = False
        elif re.match(r"Only:\s*", stripped, re.IGNORECASE):
            line = re.sub(r"Only:", "Solo:", raw, count=1, flags=re.IGNORECASE)
            line = re.sub(
                r"\b(reader|administrator|admin)\b",
                lambda m: {
                    "reader": "lector",
                    "administrator": "administrador",
                    "admin": "administrador",
                }[m.group(1).lower()],
                line,
                flags=re.IGNORECASE,
            )
            things = False
            sessions = False
        elif re.match(r"Sessions:\s*$", stripped, re.IGNORECASE):
            line = re.sub(r"Sessions:", "Sesiones:", raw, count=1, flags=re.IGNORECASE)
            things = False
            sessions = True
        elif re.match(r"-\s+C-\d+\b", stripped):
            line = _outside_quotes(line, _CASE_WORDS)
            things = False
            sessions = False
        elif stripped.startswith("BR-"):
            line = _outside_quotes(line, ((r"\bCases:", "Casos:"),))
            things = False
            sessions = False
        elif sessions and stripped.startswith("-"):
            line = _outside_quotes(
                line,
                (
                    (r"\breader\b", "lector"),
                    (r"\badministrator\b", "administrador"),
                    (r"\badmin\b", "administrador"),
                ),
            )
        elif things and stripped.startswith("-"):
            line = _outside_quotes(line, _THING_WORDS)
        elif re.search(r"\bwants to\b", line, re.IGNORECASE):
            prose = re.fullmatch(
                r"(.*?)\s+wants to\s+(.*?)\s+so that\s+(.*?)\.?", stripped, re.IGNORECASE
            )
            if prose:
                line = f"{prose.group(1)} quiere {prose.group(2)} para {prose.group(3)}."
        elif stripped and not stripped.startswith("-"):
            things = False
            sessions = False
        result.append(line)
    normalized = "\n".join(result)
    return normalized + ("\n" if text.endswith("\n") else "")


def approve_status(line: str) -> str:
    """Set an English or Spanish state line to approved without changing its language."""
    if re.match(r"\s*Status:", line, re.IGNORECASE):
        indent = line[: len(line) - len(line.lstrip())]
        return f"{indent}Status: approved"
    return "Estado: aprobada"


def english_error(error: Exception) -> str:
    """Keep English story diagnostics readable without exposing Spanish parser terminology."""
    line = getattr(error, "line", None)
    return (
        f"line {line}: invalid story syntax; check the English story format"
        if line
        else "invalid story syntax; check the English story format"
    )
