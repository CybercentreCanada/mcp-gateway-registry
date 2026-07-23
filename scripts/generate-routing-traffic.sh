#!/bin/bash

# Generate authenticated traffic across the gateway's routing axes so the
# `target_kind` label on mcpgw_registry_auth_request_total populates in
# Prometheus/Grafana. Drives three kinds of requests through /validate:
#
#   mcp_server    -> MCP tool calls   (POST /<server>/mcp)
#   a2a_agent     -> A2A invocations  (POST /agent/<path>/, gateway reverse-proxy)
#   control_plane -> registry API reads (GET /api/*)
#
# This is a load/observability helper, not a correctness test: it counts HTTP
# 200s per axis and does not assert on response bodies. Use it to demo or
# smoke-check the routing metric (see docs/OBSERVABILITY.md "Target-type
# routing"). Non-200s are reported but do not abort the run.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ---- Defaults -------------------------------------------------------------
REGISTRY_URL="${REGISTRY_URL:-http://localhost}"
TOKEN_FILE="${TOKEN_FILE:-.token}"
ITERATIONS=10
# Total run time. 0 (default) = a single pass. A positive value repeats the
# full pass in rounds until this many minutes elapse, so the charts show a
# sustained line over the rate() window instead of one brief spike.
DURATION_MINUTES=0
# Seconds to pause between rounds when DURATION_MINUTES > 0 (paces the load).
INTERVAL_SECONDS=5
# Space-separated MCP server paths (each must be enabled + healthy so nginx
# renders a /<server>/mcp location; an unhealthy/unrouted server returns 405).
# Default is the always-present built-in registry-tools server. Add your own
# healthy servers with --mcp-servers "airegistry-tools currenttime ...".
MCP_SERVERS="airegistry-tools"
# Space-separated A2A agent paths (registered + enabled in reverse-proxy mode).
A2A_AGENTS="flight-booking"
# Control-plane API paths (GET, read-only).
CONTROL_PLANE_PATHS="/api/agents /api/servers"


usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Generate traffic across the three gateway routing axes (mcp_server, a2a_agent,
control_plane) so the target_kind routing metric populates.

Options:
  --registry-url URL     Gateway base URL (default: \$REGISTRY_URL or http://localhost)
  --token-file PATH      JWT token file; accepts nested {"tokens":{"access_token"}}
                         or flat {"access_token"} (default: \$TOKEN_FILE or .token)
  --iterations N         Requests per target per axis, per round (default: $ITERATIONS)
  --duration-minutes M   Keep looping rounds for M minutes so the charts show a
                         sustained line (default: $DURATION_MINUTES = single pass)
  --interval-seconds S   Pause between rounds when --duration-minutes > 0
                         (default: $INTERVAL_SECONDS)
  --mcp-servers "a b"    MCP server paths to call (default: "$MCP_SERVERS")
  --a2a-agents "a b"     A2A agent paths to invoke (default: "$A2A_AGENTS")
  -h, --help             Show this help

Examples:
  # Single pass (10 requests per target per axis)
  $0 --registry-url http://localhost --token-file .token

  # Sustained traffic for 10 minutes to watch the timeseries charts fill in
  $0 --registry-url http://localhost --token-file .token --duration-minutes 10
EOF
}


# ---- Parse args -----------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --registry-url) REGISTRY_URL="$2"; shift 2 ;;
        --token-file) TOKEN_FILE="$2"; shift 2 ;;
        --iterations) ITERATIONS="$2"; shift 2 ;;
        --duration-minutes) DURATION_MINUTES="$2"; shift 2 ;;
        --interval-seconds) INTERVAL_SECONDS="$2"; shift 2 ;;
        --mcp-servers) MCP_SERVERS="$2"; shift 2 ;;
        --a2a-agents) A2A_AGENTS="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo -e "${RED}Unknown option: $1${NC}"; usage; exit 1 ;;
    esac
done

# Strip any trailing slash from the base URL.
REGISTRY_URL="${REGISTRY_URL%/}"


# ---- Load token (nested UI format or flat) --------------------------------
if [[ ! -f "$TOKEN_FILE" ]]; then
    echo -e "${RED}Token file not found: $TOKEN_FILE${NC}"
    exit 1
fi

# .tokens.access_token (registry UI "Get JWT Token") or top-level .access_token.
TOKEN="$(jq -r '.tokens.access_token // .access_token // empty' "$TOKEN_FILE")"
if [[ -z "$TOKEN" ]]; then
    echo -e "${RED}No access_token found in $TOKEN_FILE${NC}"
    echo "Expected {\"tokens\":{\"access_token\":...}} or {\"access_token\":...}"
    exit 1
fi

echo -e "${BLUE}Registry:${NC}   $REGISTRY_URL"
echo -e "${BLUE}Token:${NC}      $TOKEN_FILE (loaded, ${#TOKEN} chars)"
echo -e "${BLUE}Iterations:${NC} $ITERATIONS per target per axis, per round"
if [[ "$DURATION_MINUTES" -gt 0 ]]; then
    echo -e "${BLUE}Duration:${NC}   $DURATION_MINUTES min (rounds paced ${INTERVAL_SECONDS}s apart)"
else
    echo -e "${BLUE}Duration:${NC}   single pass"
fi
echo ""


# ---- Helpers --------------------------------------------------------------
# Count HTTP 200s over N requests; print a per-axis summary line.
_report() {
    local label="$1" ok="$2" total="$3"
    if [[ "$ok" -eq "$total" ]]; then
        echo -e "  ${GREEN}[OK]${NC} $label: $ok/$total returned 200"
    else
        echo -e "  ${YELLOW}[WARN]${NC} $label: $ok/$total returned 200 (rest non-200)"
    fi
}


_drive_mcp_servers() {
    echo -e "${BLUE}Axis 1 - MCP tool calls (target_kind=mcp_server)${NC}"
    for srv in $MCP_SERVERS; do
        local ok=0 code
        for ((i = 1; i <= ITERATIONS; i++)); do
            code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$REGISTRY_URL/$srv/mcp" \
                -H "Authorization: Bearer $TOKEN" \
                -H "Content-Type: application/json" \
                -H "Accept: application/json, text/event-stream" \
                -d "{\"jsonrpc\":\"2.0\",\"id\":\"$i\",\"method\":\"tools/list\",\"params\":{}}" || echo "000")
            [[ "$code" == "200" ]] && ok=$((ok + 1))
        done
        _report "/$srv/mcp" "$ok" "$ITERATIONS"
    done
}


_drive_a2a_agents() {
    echo -e "${BLUE}Axis 2 - A2A invocations through gateway (target_kind=a2a_agent)${NC}"
    for agent in $A2A_AGENTS; do
        local ok=0 code
        for ((i = 1; i <= ITERATIONS; i++)); do
            # X-Authorization = gateway token (gated + stripped at /validate);
            # Authorization = target-agent credential (presence-only accepts any).
            code=$(curl -s -o /dev/null -w "%{http_code}" "$REGISTRY_URL/agent/$agent/" \
                -H "X-Authorization: Bearer $TOKEN" \
                -H "Authorization: Bearer traffic-gen-placeholder" \
                -H "Content-Type: application/json" \
                -d "{\"jsonrpc\":\"2.0\",\"id\":\"$i\",\"method\":\"message/send\",\"params\":{\"message\":{\"role\":\"user\",\"messageId\":\"gen-$i\",\"parts\":[{\"kind\":\"text\",\"text\":\"traffic $i\"}]}}}" || echo "000")
            [[ "$code" == "200" ]] && ok=$((ok + 1))
        done
        _report "/agent/$agent/" "$ok" "$ITERATIONS"
    done
}


_drive_control_plane() {
    echo -e "${BLUE}Axis 3 - control-plane reads (target_kind=control_plane)${NC}"
    for path in $CONTROL_PLANE_PATHS; do
        local ok=0 code
        for ((i = 1; i <= ITERATIONS; i++)); do
            code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" \
                "$REGISTRY_URL$path" || echo "000")
            [[ "$code" == "200" ]] && ok=$((ok + 1))
        done
        _report "$path" "$ok" "$ITERATIONS"
    done
}


# One full pass across all three axes.
_run_round() {
    _drive_mcp_servers
    _drive_a2a_agents
    _drive_control_plane
}


main() {
    if [[ "$DURATION_MINUTES" -le 0 ]]; then
        # Single pass.
        _run_round
    else
        # Loop rounds until the wall-clock deadline. SECONDS is a bash builtin
        # that counts seconds since the shell started.
        local deadline=$((DURATION_MINUTES * 60))
        local round=0
        while [[ "$SECONDS" -lt "$deadline" ]]; do
            round=$((round + 1))
            local elapsed_min
            elapsed_min=$(awk "BEGIN{printf \"%.1f\", $SECONDS/60}")
            echo -e "${BLUE}=== Round $round (t+${elapsed_min}m / ${DURATION_MINUTES}m) ===${NC}"
            _run_round
            # Stop if the next pause would overrun the deadline.
            [[ "$SECONDS" -ge "$deadline" ]] && break
            sleep "$INTERVAL_SECONDS"
        done
        echo -e "${BLUE}Completed $round rounds over ~${DURATION_MINUTES} min.${NC}"
    fi
    echo ""
    echo -e "${GREEN}Done.${NC} Chart the split with (Prometheus/Grafana):"
    echo "  sum by (target_kind)(rate(mcpgw_registry_auth_request_total[5m]))"
}

main
