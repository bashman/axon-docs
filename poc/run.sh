#!/usr/bin/env bash
# Run the five-step story-to-HTTP demonstration.
set -euo pipefail

POC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STORY_ARG="${1:-stories-library-small.md}"
if [[ "$STORY_ARG" = /* ]]; then
    STORIES="$(realpath -m "$STORY_ARG")"
else
    STORIES="$(realpath -m "$POC_ROOT/$STORY_ARG")"
fi
if [[ "$STORIES" != "$POC_ROOT"/* ]]; then
    echo "Error: story files must be inside the poc directory." >&2
    exit 2
fi
WORKDIR="$(mktemp -d -t axon-poc-XXXXXX)"
mkdir -p "$WORKDIR"
DB="$WORKDIR/axon.sqlite"
SPEC="$WORKDIR/spec.json"
CASES="$WORKDIR/cases.json"
MCP_CONFIG="$WORKDIR/mcp.json"
AGENT_OUT="$WORKDIR/agent.json"
SERVER_LOG="$WORKDIR/server.log"
SERVER_PID=""

cleanup() {
    if [[ -n "$SERVER_PID" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

axon() { uv run --project "$POC_ROOT" axon "$@"; }
python_poc() { uv run --project "$POC_ROOT" python "$@"; }

echo "[1/5] Validating and compiling stories"
axon story check "$STORIES"
axon story compile "$STORIES" -o "$SPEC"
axon story cases "$STORIES" -o "$CASES"

echo "[2/5] Preparing AXON MCP configuration"
python_poc "$POC_ROOT/mcp_config.py" "$DB" "$SPEC" "$CASES" "$POC_ROOT" "$MCP_CONFIG"

echo "[3/5] Asking Claude Code to implement handlers through MCP (model: ${DEMO_MODEL:-haiku})"
PROMPT="The AXON MCP server has a registered spec and visible HTTP cases. Call describe_spec first. Define one handler per route with define_many, then call run_http_cases. Keep going until all cases pass. Do not write files. Reply with a short English summary."
if ! claude -p "$PROMPT" \
    --model "${DEMO_MODEL:-haiku}" \
    --mcp-config "$MCP_CONFIG" \
    --strict-mcp-config \
    --allowedTools "mcp__axon" \
    --disallowedTools "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit" \
    --setting-sources user \
    --no-session-persistence \
    --output-format json > "$AGENT_OUT"; then
    echo "Error: Claude Code could not complete the agent step; no HTTP verdict was produced." >&2
    exit 1
fi
echo "Agent response saved to $AGENT_OUT"

echo "[4/5] Starting the test-mode HTTP server"
PORT="$(python_poc -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
AXON_BIN="$(uv run --project "$POC_ROOT" which axon)"
"$AXON_BIN" serve-http --db "$DB" --spec "$SPEC" --port "$PORT" --test-mode > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
READY=0
for _ in $(seq 100); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
        READY=1
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "Error: the HTTP server did not start; see $SERVER_LOG." >&2
        exit 1
    fi
    sleep 0.1
done
if [[ "$READY" != "1" ]]; then
    echo "Error: the HTTP server did not respond; see $SERVER_LOG." >&2
    exit 1
fi

echo "[5/5] Replaying cases over real HTTP"
python_poc "$POC_ROOT/judge_http.py" "http://127.0.0.1:$PORT" "$CASES"
