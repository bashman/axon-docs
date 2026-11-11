# AXON Story-to-HTTP PoC

A self-contained demo of an approved user story becoming an AXON spec and HTTP cases, an agent implementing handlers inside AXON's graph through MCP, and a judge replaying those cases over real HTTP.

## Run it

```bash
bash run.sh                         # five cases
bash run.sh stories-library.md      # fifteen cases
DEMO_MODEL=sonnet bash run.sh      # optional model choice; default: haiku
```

Requirements: Python 3.11+, `uv`, and Claude Code (`claude`) with an active login. No Docker or Postgres is used. The final line reports `N/N cases pass over real HTTP` when all cases pass.

## English story syntax

| English keyword | Meaning in the story format |
|---|---|
| `Given` / `When` / `Then` | Starting state / request / expected response |
| `Status: approved` (also `proposed`, `postponed`) | Story state |
| `Things:` / `Cases:` | Entity declarations / cases |
| `Creates:` / `Rule:` | Created entity / linked rule |
| `As:` / `nothing` / `nobody` | Session identity / empty seed / no session |
| `Error:` | Error code (kept as written) |
| `[happy]` / `[error]` | Expected success / expected error |
| `text` / `number`, `required`, `unique`, `(table)`, `references` | Field types and constraints |
| `Only:` / `Sessions:` | Role gate / test identities |
| `Out of scope` | Document-level excluded work |

The normalization layer maps these markers to AXON's canonical story grammar before parsing. Keep JSON property names and route paths consistent between a request and its cases. Story prose follows `A role wants to do something so that a goal is met.`

## What this does not show

- Cases are single-request only; there are no create-then-delete sequences.
- IDs are `count + 1`, which is safe only when there are no deletes.
- Test mode resets data before each case; it does not show persistence.
- These are example stories written for the demo, not a real user's requirements.
- A cheap-model run is not a cost measurement.
