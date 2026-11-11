"""Telemetry: a deterministic summary of a session, built only from `history`.

`report(store)` reads the append-only history and reports how often each tool
ran, which error codes came back, how much iteration a name needed before its
first accepted `define`, the JSON size of deltas and feedbacks as a token proxy,
and the wall-clock span between the first and last entry. The output is a plain
JSON-able dict with a fixed key order (nested maps sorted), so it is stable.

The phase-1 gate looks at the human-authored specs, not at this report: here we
just make the agent's own activity visible.
"""

import json

TOOLS = ("define", "run", "query_graph", "patch", "run_spec")


def report(store) -> dict:
    """Summarize `store.history()` as a stable, JSON-able dict."""
    entries = store.history(limit=None)
    calls = dict.fromkeys(TOOLS, 0)
    rejected = {}
    bytes_in = bytes_out = 0
    for entry in entries:
        kind = entry["kind"]
        if kind in calls:
            calls[kind] += 1
        feedback = entry["feedback"]
        if isinstance(feedback, dict) and feedback.get("ok") is False:
            error = feedback.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, str):
                rejected[code] = rejected.get(code, 0) + 1
        bytes_in += _size(entry["payload"])
        bytes_out += _size(feedback)
    stamps = [entry["ts"] for entry in entries]
    return {
        "tool_calls": {tool: calls[tool] for tool in TOOLS},
        "rejected_by_code": {code: rejected[code] for code in sorted(rejected)},
        "defines_before_first_accept": _defines_before_first_accept(entries),
        "bytes": {"input": bytes_in, "output": bytes_out},
        "elapsed": (max(stamps) - min(stamps)) if stamps else 0.0,
    }


def _defines_before_first_accept(entries) -> dict:
    """Rejected `define`s per name before the first accepted one (accepted names only)."""
    rejected = {}
    found = {}
    for entry in reversed(entries):  # history is newest-first; replay oldest-first
        if entry["kind"] != "define":
            continue
        payload = entry["payload"]
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not name or name in found:
            continue
        feedback = entry["feedback"]
        if isinstance(feedback, dict) and feedback.get("ok") is True:
            found[name] = rejected.get(name, 0)
        else:
            rejected[name] = rejected.get(name, 0) + 1
    return {name: found[name] for name in sorted(found)}


def _size(value) -> int:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(raw.encode("utf-8"))
