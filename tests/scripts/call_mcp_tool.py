#!/usr/bin/env python3
"""Call an MCP tool through the gateway, optionally in a loop (rate-limit testing).

This is a thin MCP client for exercising the gateway data plane end to end. It
performs the full Streamable-HTTP handshake (initialize -> capture session id ->
tools/call) against a server exposed through the registry, and can repeat the
tool call N times so you can watch application-level rate limiting kick in (HTTP
429 with X-RateLimit-* / Retry-After headers).

The server URL, tool name, and tool arguments (as JSON) are command-line args.
The bearer token and registry base URL default to a local dev setup and can be
overridden.

Examples
--------
    # Single call to the currenttime server's tool, local gateway, default token
    uv run python tests/scripts/call_mcp_tool.py \\
        --server-url http://localhost/currenttime/mcp \\
        --tool current_time_by_timezone \\
        --tool-args '{"timezone": "America/New_York"}'

    # Fire 20 calls to watch a rate limit trip (prints a per-call status line)
    uv run python tests/scripts/call_mcp_tool.py \\
        --server-url http://localhost/currenttime/mcp \\
        --tool current_time_by_timezone \\
        --tool-args '{"timezone": "UTC"}' \\
        --count 20

    # Override token file and registry base URL
    uv run python tests/scripts/call_mcp_tool.py \\
        --server-url https://mcpgateway.ddns.net/currenttime/mcp \\
        --tool current_time_by_timezone --tool-args '{}' \\
        --token-file .oauth-tokens/ingress.json \\
        --registry-url https://mcpgateway.ddns.net
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import requests

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

# Defaults per the local dev setup.
DEFAULT_TOKEN_FILE: str = ".token"
DEFAULT_REGISTRY_URL: str = "http://localhost"
MCP_PROTOCOL_VERSION: str = "2025-03-26"
REQUEST_TIMEOUT_SECONDS: int = 30


def _read_access_token(
    token_file: str,
) -> str:
    """Read a bearer token from a file.

    Accepts a plain JWT string, or JSON with ``.access_token``,
    ``.tokens.access_token``, or ``.token_data.access_token``.
    """
    path = Path(token_file)
    if not path.is_file():
        raise FileNotFoundError(f"Token file not found: {token_file}")

    # Strip whitespace and any trailing control bytes (some token files are
    # written with a stray control character after the closing brace).
    content = path.read_text().strip().strip("\x00\x01\x02\x03\x04").strip()

    data = None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Salvage a JSON object with trailing junk by parsing up to the last brace.
        last_brace = content.rfind("}")
        if last_brace != -1:
            try:
                data = json.loads(content[: last_brace + 1])
            except json.JSONDecodeError:
                data = None

    if data is None:
        return content  # plain JWT string

    for getter in (
        lambda d: d.get("access_token"),
        lambda d: (d.get("tokens") or {}).get("access_token"),
        lambda d: (d.get("token_data") or {}).get("access_token"),
    ):
        token = getter(data)
        if token:
            return token
    raise ValueError(f"Could not find an access token in {token_file}")


def _parse_sse_or_json(
    text: str,
) -> dict[str, Any] | None:
    """Parse a response body that is either plain JSON or SSE (``data:`` lines)."""
    text = text.strip()
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _post(
    server_url: str,
    token: str,
    payload: dict[str, Any],
    session_id: str | None,
) -> requests.Response:
    """POST a JSON-RPC payload to the MCP server with the standard headers.

    The gateway's nginx ``auth_request`` reads the bearer token from
    ``X-Authorization`` (the plain ``Authorization`` header is reserved for the
    upstream MCP server's own auth), so we send both: ``X-Authorization`` for the
    gateway and ``Authorization`` for direct-to-server use.
    """
    headers = {
        "X-Authorization": f"Bearer {token}",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    return requests.post(
        server_url,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _initialize(
    server_url: str,
    token: str,
) -> str | None:
    """Run the MCP initialize handshake and return the session id (if the server sets one)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "call-mcp-tool", "version": "1.0.0"},
        },
    }
    response = _post(server_url, token, payload, session_id=None)
    if response.status_code != 200:
        logger.warning(
            "initialize returned HTTP %s (continuing without a session): %s",
            response.status_code,
            response.text[:200],
        )
        return None
    # The session id is returned in a response header (name is case-insensitive).
    for key, value in response.headers.items():
        if key.lower() == "mcp-session-id":
            logger.info("MCP session established: %s", value)
            return value
    logger.info("No mcp-session-id header returned; proceeding without one")
    return None


def _call_tool_once(
    server_url: str,
    token: str,
    tool_name: str,
    tool_args: dict[str, Any],
    session_id: str | None,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    """Make a single tools/call and return (http_status, headers, parsed_body)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": tool_args},
    }
    response = _post(server_url, token, payload, session_id)
    return response.status_code, dict(response.headers), _parse_sse_or_json(response.text)


def _format_rate_limit_headers(
    headers: dict[str, str],
) -> str:
    """Extract the rate-limit headers into a compact string, or '' if none present."""
    lookup = {k.lower(): v for k, v in headers.items()}
    parts = []
    for name in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"):
        if name in lookup:
            parts.append(f"{name}={lookup[name]}")
    return "  [" + ", ".join(parts) + "]" if parts else ""


def _run_calls(
    server_url: str,
    token: str,
    tool_name: str,
    tool_args: dict[str, Any],
    session_id: str | None,
    count: int,
    delay_seconds: float,
) -> int:
    """Make ``count`` tool calls, log each result, and return the number of 429s seen."""
    throttled = 0
    for i in range(1, count + 1):
        status, headers, body = _call_tool_once(server_url, token, tool_name, tool_args, session_id)
        rate_info = _format_rate_limit_headers(headers)
        if status == 429:
            throttled += 1
            logger.warning("call %d/%d -> HTTP 429 (rate limited)%s", i, count, rate_info)
        elif status == 200 and body and "error" not in body:
            logger.info("call %d/%d -> HTTP 200 OK%s", i, count, rate_info)
        else:
            detail = json.dumps(body, default=str)[:300] if body else "<no body>"
            logger.warning("call %d/%d -> HTTP %s: %s%s", i, count, status, detail, rate_info)
        if delay_seconds > 0 and i < count:
            time.sleep(delay_seconds)
    return throttled


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Call an MCP tool through the gateway (optionally in a loop for rate-limit testing).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--server-url",
        required=True,
        help="Full MCP server URL through the gateway, e.g. http://localhost/currenttime/mcp",
    )
    parser.add_argument(
        "--tool",
        required=True,
        help="Tool name to call (tools/call params.name)",
    )
    parser.add_argument(
        "--tool-args",
        default="{}",
        help='Tool arguments as a JSON object string, e.g. \'{"timezone": "UTC"}\' (default: {})',
    )
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help=f"Path to the bearer token file (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--registry-url",
        default=DEFAULT_REGISTRY_URL,
        help=f"Registry base URL, for reference/logging (default: {DEFAULT_REGISTRY_URL})",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of times to call the tool (default: 1). Use a higher value to trip rate limits.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0.0,
        help="Delay between calls in seconds (default: 0 = as fast as possible)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> int:
    """Parse args, run the MCP handshake and tool call(s), report a summary."""
    args = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        tool_args = json.loads(args.tool_args)
        if not isinstance(tool_args, dict):
            raise ValueError("--tool-args must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Invalid --tool-args: %s", exc)
        return 1

    try:
        token = _read_access_token(args.token_file)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Calling tool '%s' on %s (registry=%s), count=%d",
        args.tool,
        args.server_url,
        args.registry_url,
        args.count,
    )

    session_id = _initialize(args.server_url, token)
    start = time.time()
    throttled = _run_calls(
        args.server_url,
        token,
        args.tool,
        tool_args,
        session_id,
        args.count,
        args.delay_seconds,
    )
    elapsed = time.time() - start

    logger.info(
        "Done: %d call(s) in %.1fs, %d throttled (429), %d succeeded/other",
        args.count,
        elapsed,
        throttled,
        args.count - throttled,
    )
    # Non-zero exit if every call was throttled, so scripts can detect a hard block.
    return 2 if throttled == args.count and args.count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
