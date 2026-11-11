"""Write a Claude Code MCP config for the local AXON graph server."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def make_axon_mcp_config(db: Path, spec: Path, cases: Path, root: Path) -> dict:
    return {
        "mcpServers": {
            "axon": {
                "command": sys.executable,
                "args": [
                    "-m",
                    "axon.mcp_server",
                    str(db),
                    str(spec),
                    "--http-cases",
                    str(cases),
                    "--text",
                ],
                "env": {"PYTHONPATH": str(root)},
            }
        }
    }


def main() -> None:
    db, spec, cases, root, output = map(Path, sys.argv[1:])
    config = make_axon_mcp_config(db, spec, cases, root)
    output.write_text(json.dumps(config, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
