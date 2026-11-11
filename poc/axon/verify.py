"""Linear O(n) verifier with structured errors."""

import json

DEFAULT_CAPS = {"fd_write": {1, 2}, "exit": True}
MAX_BUF = 4096


class Rejected(Exception):
    """Structured error: what goes back to the agent, not a stack trace."""

    def __init__(self, node, code, detail):
        self.info = {"node": node, "code": code, "detail": detail}
        super().__init__(self.info)


def load_caps(path):
    """Load capabilities from JSON: {"fd_write": [1, 2], "exit": true}.

    The 'fd_write' list is converted to a set. Any schema violation raises a
    ValueError with a message that points at the offending field.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid capabilities JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("capabilities must be a JSON object")
    unknown = sorted(set(raw) - {"fd_write", "exit"})
    if unknown:
        raise ValueError(f"unknown capability keys: {unknown}")
    for key in ("fd_write", "exit"):
        if key not in raw:
            raise ValueError(f"missing capability '{key}'")
    fds = raw["fd_write"]
    if not isinstance(fds, list) or any(
        isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in fds
    ):
        raise ValueError("'fd_write' must be a list of non-negative integers")
    if not isinstance(raw["exit"], bool):
        raise ValueError("'exit' must be a boolean")
    return {"fd_write": set(fds), "exit": raw["exit"]}



def verify(nodes, caps=DEFAULT_CAPS):
    """Single pass. If it passes, generated code cannot step outside what was granted."""
    if not nodes or nodes[-1][0] != "exit":
        raise Rejected(len(nodes), "E_NO_TERMINATOR", "graph must end in exit")
    for idx, nd in enumerate(nodes):
        if nd[0] == "write":
            _, fd, data = nd
            if fd not in caps["fd_write"]:
                raise Rejected(
                    idx, "E_CAP_DENIED", f"fd {fd} not granted; allowed {sorted(caps['fd_write'])}"
                )
            if len(data) > MAX_BUF:
                raise Rejected(idx, "E_BUF_LIMIT", f"{len(data)} > {MAX_BUF}")
        elif nd[0] == "exit":
            if not caps["exit"]:
                raise Rejected(idx, "E_CAP_DENIED", "exit not granted")
            if not 0 <= nd[1] <= 255:
                raise Rejected(idx, "E_RANGE", "exit code outside 0..255")
            if idx != len(nodes) - 1:
                raise Rejected(idx, "E_UNREACHABLE", "nodes after exit")
    return True
