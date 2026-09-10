#!/usr/bin/env python3
"""Pre-release end-to-end smoke test for the MCP Gateway Registry.

A short battery of end-to-end tests meant to be run against a LIVE gateway
right before cutting a new release. It exercises the surface a release must not
break: the registry is up, the built-in ``airegistry-tools`` MCP server is
healthy and its search tool is invocable, servers/agents/skills support full
CRUD, semantic search returns results, security scans run, and one real
external MCP server (AWS knowledge base) is reachable end to end through the
gateway proxy path.

Each test is self-contained and self-cleaning: it creates uniquely-named
entities (``e2e-<timestamp>-...``) and deletes them at the end, even on
failure, so the suite is safe to run repeatedly against a shared environment.

The runner prints a pass/fail table and exits non-zero if any test fails, so it
can gate a release from CI or a runbook.

Usage:
    # Run against a local gateway with an admin token file at ./.token
    uv run python tests/e2e_release_test.py

    # Run against a remote gateway
    uv run python tests/e2e_release_test.py \\
        --registry-url https://your-gateway.example.com \\
        --token-file .oauth-tokens/ingress.json

    # Skip the external (AWS) test entirely, or enable debug logging
    uv run python tests/e2e_release_test.py --skip-external --debug
"""

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import requests

# Add api directory to path for RegistryClient imports.
sys.path.insert(0, str(Path(__file__).parent.parent / "api"))

from registry_client import (
    AgentRegistration,
    InternalServiceRegistration,
    RegistryClient,
    SkillRegistrationRequest,
)

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)


# Test constants
AIREGISTRY_TOOLS_PATH: str = "/airegistry-tools"
AIREGISTRY_TOOLS_MCP_SUFFIX: str = "/airegistry-tools/mcp"
SEARCH_TOOL_NAME: str = "intelligent_tool_finder"
SEARCH_QUERY: str = "find current time and date"
SEMANTIC_SEARCH_QUERY: str = "time and date services"

# External test server (real remote AWS knowledge base MCP server).
AWS_KB_PATH: str = "/aws-kb-e2e"
AWS_KB_PROXY_URL: str = "https://knowledge-mcp.global.api.aws"
AWS_KB_MCP_SUFFIX: str = "/aws-kb-e2e/mcp"

# A benign placeholder backend used only for CRUD lifecycle tests (never called).
PLACEHOLDER_PROXY_URL: str = "https://example.com"

MCP_PROTOCOL_VERSION: str = "2025-03-26"
MCP_REQUEST_TIMEOUT_SECONDS: int = 30


class TestStatus(Enum):
    """Test result status."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class TestResult:
    """Individual test result."""

    name: str
    status: TestStatus
    duration_ms: float
    message: str = ""


def _read_token(
    token_file: str,
) -> str:
    """Read a bearer token from a plain-text or JSON token file.

    Accepts a bare JWT, or JSON with ``access_token``/``token``/``jwt`` at the
    top level or nested under a ``tokens`` object.

    Args:
        token_file: Path to the token file.

    Returns:
        The bearer token string.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is empty or no token field is found.
    """
    path = Path(token_file)
    if not path.exists():
        raise FileNotFoundError(f"Token file not found: {token_file}")

    content = path.read_text().strip()
    if not content:
        raise ValueError(f"Token file is empty: {token_file}")

    if not content.startswith("{"):
        return content

    data = json.loads(content)
    for key in ("access_token", "token", "jwt"):
        if key in data:
            return data[key]
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        for key in ("access_token", "token", "jwt"):
            if key in tokens:
                return tokens[key]
    raise ValueError(f"No token field found in JSON token file: {token_file}")


def _parse_sse_or_json(
    text: str,
) -> dict[str, Any] | None:
    """Parse an MCP response body that is either plain JSON or SSE data lines."""
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


class ReleaseE2ETest:
    """End-to-end pre-release smoke test runner using RegistryClient."""

    def __init__(
        self,
        registry_url: str,
        token: str,
        skip_external: bool,
        scan_server_path: str,
    ):
        """Initialize the test runner.

        Args:
            registry_url: Base URL of the gateway (e.g. http://localhost).
            token: Admin/M2M bearer token.
            skip_external: Skip the external AWS knowledge-base test entirely.
            scan_server_path: Server path to run the security-scan test against.
        """
        self.registry_url = registry_url.rstrip("/")
        self.token = token
        self.skip_external = skip_external
        self.scan_server_path = scan_server_path
        self.client = RegistryClient(self.registry_url, token)
        self.results: list[TestResult] = []
        # Timestamp suffix keeps entity names unique across repeated runs.
        self.suffix = str(int(time.time()))

    def _record(
        self,
        name: str,
        status: TestStatus,
        duration_ms: float,
        message: str = "",
    ) -> None:
        """Record and log a single test result."""
        self.results.append(TestResult(name, status, duration_ms, message))
        logger.info(f"[{status.value}] {name}: {message} ({duration_ms:.0f}ms)")

    def _run(
        self,
        name: str,
        func: Callable[[], str],
    ) -> None:
        """Run one test function, timing it and capturing pass/fail/skip.

        The test function returns a summary string on success, raises
        ``_SkipTest`` to record a SKIPPED result, or raises any other exception
        to record a FAILED result.
        """
        start = time.time()
        try:
            message = func()
            self._record(name, TestStatus.PASSED, (time.time() - start) * 1000, message)
        except _SkipTest as skip:
            self._record(name, TestStatus.SKIPPED, (time.time() - start) * 1000, str(skip))
        except Exception as exc:
            self._record(name, TestStatus.FAILED, (time.time() - start) * 1000, f"Exception: {exc}")

    def _mcp_call_tool(
        self,
        mcp_url: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke a tool on a gateway-exposed MCP server (streamable-http).

        Performs the full handshake (initialize -> capture Mcp-Session-Id ->
        tools/call). Sends the token as both ``X-Authorization`` (read by the
        gateway's nginx auth_request) and ``Authorization`` (for the upstream).

        Returns:
            The parsed JSON-RPC ``result`` object from the tools/call response.

        Raises:
            RuntimeError: On any HTTP error, JSON-RPC error, or unparseable body.
        """
        headers = {
            "X-Authorization": f"Bearer {self.token}",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "release-e2e", "version": "1.0.0"},
            },
        }
        init_resp = requests.post(
            mcp_url,
            headers=headers,
            json=init_payload,
            timeout=MCP_REQUEST_TIMEOUT_SECONDS,
        )
        if init_resp.status_code != 200:
            raise RuntimeError(
                f"initialize returned HTTP {init_resp.status_code}: {init_resp.text[:200]}"
            )

        session_id = None
        for key, value in init_resp.headers.items():
            if key.lower() == "mcp-session-id":
                session_id = value
                break
        if session_id:
            headers["mcp-session-id"] = session_id

        call_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": tool_args},
        }
        call_resp = requests.post(
            mcp_url,
            headers=headers,
            json=call_payload,
            timeout=MCP_REQUEST_TIMEOUT_SECONDS,
        )
        if call_resp.status_code != 200:
            raise RuntimeError(
                f"tools/call returned HTTP {call_resp.status_code}: {call_resp.text[:200]}"
            )

        body = _parse_sse_or_json(call_resp.text)
        if body is None:
            raise RuntimeError(f"Could not parse tools/call response: {call_resp.text[:200]}")
        if "error" in body:
            raise RuntimeError(f"JSON-RPC error: {json.dumps(body['error'], default=str)}")

        result = body.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(
                f"Unexpected tools/call result shape: {json.dumps(body, default=str)[:200]}"
            )
        return result

    def test_registry_up(self) -> str:
        """Registry is up: health check passes and servers are listable."""
        health = self.client.healthcheck()
        status = health.get("status", "unknown")

        listing = self.client.list_services(limit=5)
        return f"health={status}, {listing.total_count} servers registered"

    def test_airegistry_tools_search(self) -> str:
        """The built-in airegistry-tools server is healthy and search works."""
        mcp_url = f"{self.registry_url}{AIREGISTRY_TOOLS_MCP_SUFFIX}"

        # healthcheck tool proves the server is up and reachable through the proxy.
        health = self._mcp_call_tool(mcp_url, "healthcheck", {})
        health_text = _first_content_text(health)

        # intelligent_tool_finder proves semantic search over the registry works.
        search = self._mcp_call_tool(
            mcp_url,
            SEARCH_TOOL_NAME,
            {"query": SEARCH_QUERY, "top_n": 3},
        )
        search_text = _first_content_text(search)
        results_count = _extract_json_field(search_text, "total_results")

        return f"healthcheck ok, {SEARCH_TOOL_NAME} returned {results_count} result(s)"

    def test_server_crud(self) -> str:
        """Full CRUD lifecycle for an MCP server."""
        path = f"/e2e-server-{self.suffix}"
        name = f"e2e-server-{self.suffix}"
        try:
            registration = InternalServiceRegistration(
                path=path,
                name=name,
                description="E2E release test server (safe to delete)",
                proxy_pass_url=PLACEHOLDER_PROXY_URL,
                supported_transports=["streamable-http"],
                auth_scheme="none",
                tags=["e2e-test"],
                visibility="public",
                overwrite=True,
            )
            self.client.register_service(registration)

            fetched = self.client.get_server(path)
            if fetched.path.rstrip("/") not in (path, path.lstrip("/")):
                raise RuntimeError(f"get_server returned wrong path: {fetched.path}")

            self.client.update_server(
                path,
                {
                    "server_name": name,
                    "description": "E2E release test server (updated)",
                },
            )

            toggled = self.client.toggle_service(path)
            self.client.toggle_service(path)  # toggle back

            return f"registered/read/updated/toggled server (was enabled={not toggled.is_enabled})"
        finally:
            self._safe_delete_server(path)

    def test_agent_crud(self) -> str:
        """Full CRUD lifecycle for an A2A agent."""
        path = f"/e2e-agent-{self.suffix}"
        name = f"e2e-agent-{self.suffix}"

        def _build_agent(description: str) -> AgentRegistration:
            return AgentRegistration(
                name=name,
                description=description,
                url="https://example.com/agent",
                version="1.0.0",
                path=path,
                tags=["e2e-test"],
                visibility="public",
                supported_protocol="a2a",
            )

        try:
            self.client.register_agent(_build_agent("E2E release test agent"))

            fetched = self.client.get_agent(path)
            if fetched.name != name:
                raise RuntimeError(f"get_agent returned wrong name: {fetched.name}")

            self.client.update_agent(path, _build_agent("E2E release test agent (updated)"))
            self.client.toggle_agent(path, enabled=True)

            return "registered/read/updated/toggled agent"
        finally:
            self._safe_delete_agent(path)

    def test_skill_crud(self) -> str:
        """Full CRUD lifecycle for an agent skill."""
        name = f"e2e-skill-{self.suffix}"
        skill_md_url = "https://github.com/anthropics/skills/blob/main/skills/mcp-builder/SKILL.md"
        skill_path = None
        try:
            request = SkillRegistrationRequest(
                name=name,
                skill_md_url=skill_md_url,
                description="E2E release test skill (safe to delete)",
                tags=["e2e-test"],
                visibility="public",
            )
            skill = self.client.register_skill(request)
            skill_path = skill.path

            fetched = self.client.get_skill(skill_path)
            if fetched.name != name:
                raise RuntimeError(f"get_skill returned wrong name: {fetched.name}")

            self.client.toggle_skill(skill_path, enabled=False)
            self.client.toggle_skill(skill_path, enabled=True)

            return f"registered/read/toggled skill at {skill_path}"
        finally:
            if skill_path:
                self._safe_delete_skill(skill_path)

    def test_semantic_search(self) -> str:
        """Cross-entity semantic search returns results for a known query."""
        response = self.client.semantic_search(
            query=SEMANTIC_SEARCH_QUERY,
            max_results=10,
        )
        total = (
            response.total_servers
            + response.total_tools
            + response.total_agents
            + response.total_skills
            + response.total_virtual_servers
        )
        if total == 0:
            raise RuntimeError(f"Semantic search returned no results for '{SEMANTIC_SEARCH_QUERY}'")
        return (
            f"{response.total_servers} servers, {response.total_tools} tools, "
            f"{response.total_agents} agents, {response.total_skills} skills"
        )

    def test_security_scan(self) -> str:
        """Trigger a security scan and read back the results."""
        rescan = self.client.rescan_server(self.scan_server_path)
        if rescan.scan_failed:
            raise RuntimeError(f"Scan failed: {rescan.error_message}")

        # Read-back proves the results are persisted and retrievable.
        self.client.get_security_scan(self.scan_server_path)

        safety = "SAFE" if rescan.is_safe else "UNSAFE"
        return (
            f"scanned {self.scan_server_path}: {safety} "
            f"(critical={rescan.critical_issues}, high={rescan.high_severity}, "
            f"analyzers={','.join(rescan.analyzers_used)})"
        )

    def test_external_server(self) -> str:
        """Register a real external MCP server and invoke a tool through the proxy.

        Uses the AWS knowledge-base MCP server. If the remote endpoint is
        unreachable (network/outage), the test is SKIPPED rather than FAILED so
        an external outage does not block a release.
        """
        if self.skip_external:
            raise _SkipTest("--skip-external was passed")

        mcp_url = f"{self.registry_url}{AWS_KB_MCP_SUFFIX}"
        try:
            registration = InternalServiceRegistration(
                path=AWS_KB_PATH,
                name=f"aws-kb-e2e-{self.suffix}",
                description="E2E release test: AWS knowledge base (safe to delete)",
                proxy_pass_url=AWS_KB_PROXY_URL,
                mcp_endpoint=AWS_KB_PROXY_URL,
                supported_transports=["streamable-http"],
                auth_scheme="none",
                tags=["e2e-test", "aws", "kb"],
                visibility="public",
                overwrite=True,
            )
            self.client.register_service(registration)

            # Enable the server so the gateway routes to it.
            fetched = self.client.get_server(AWS_KB_PATH)
            if not fetched.is_enabled:
                self.client.toggle_service(AWS_KB_PATH)

            # Give nginx a moment to pick up the new route.
            time.sleep(3)

            tool_count = self._list_external_tools(mcp_url)
            return f"registered aws-kb and reached it through the proxy ({tool_count} tools listed)"
        except requests.exceptions.RequestException as exc:
            raise _SkipTest(f"External endpoint unreachable: {exc}")
        finally:
            self._safe_delete_server(AWS_KB_PATH)

    def _list_external_tools(
        self,
        mcp_url: str,
    ) -> int:
        """List tools on an external MCP server via the protocol; return count.

        Raises requests exceptions on network failure so the caller can SKIP.
        """
        headers = {
            "X-Authorization": f"Bearer {self.token}",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        init_resp = requests.post(
            mcp_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "release-e2e", "version": "1.0.0"},
                },
            },
            timeout=MCP_REQUEST_TIMEOUT_SECONDS,
        )
        if init_resp.status_code != 200:
            raise RuntimeError(
                f"external initialize returned HTTP {init_resp.status_code}: {init_resp.text[:200]}"
            )
        for key, value in init_resp.headers.items():
            if key.lower() == "mcp-session-id":
                headers["mcp-session-id"] = value
                break

        list_resp = requests.post(
            mcp_url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            timeout=MCP_REQUEST_TIMEOUT_SECONDS,
        )
        if list_resp.status_code != 200:
            raise RuntimeError(
                f"external tools/list returned HTTP {list_resp.status_code}: {list_resp.text[:200]}"
            )
        body = _parse_sse_or_json(list_resp.text)
        if body is None or "error" in body:
            raise RuntimeError(f"external tools/list error: {list_resp.text[:200]}")
        tools = body.get("result", {}).get("tools", [])
        return len(tools)

    def _safe_delete_server(
        self,
        path: str,
    ) -> None:
        """Delete a server, swallowing errors (cleanup best-effort)."""
        try:
            self.client.remove_service(path)
        except Exception as exc:
            logger.warning(f"Cleanup: could not delete server {path}: {exc}")

    def _safe_delete_agent(
        self,
        path: str,
    ) -> None:
        """Delete an agent, swallowing errors (cleanup best-effort)."""
        try:
            self.client.delete_agent(path)
        except Exception as exc:
            logger.warning(f"Cleanup: could not delete agent {path}: {exc}")

    def _safe_delete_skill(
        self,
        path: str,
    ) -> None:
        """Delete a skill, swallowing errors (cleanup best-effort)."""
        try:
            self.client.delete_skill(path)
        except Exception as exc:
            logger.warning(f"Cleanup: could not delete skill {path}: {exc}")

    def run_all_tests(self) -> bool:
        """Run all tests in order and print the report."""
        logger.info("=" * 60)
        logger.info("Starting MCP Gateway Registry Release Smoke Test")
        logger.info(f"Registry URL: {self.registry_url}")
        logger.info("=" * 60)

        self._run("1. Registry Up & Config", self.test_registry_up)
        self._run("2. airegistry-tools Search", self.test_airegistry_tools_search)
        self._run("3. Server CRUD", self.test_server_crud)
        self._run("4. Agent CRUD", self.test_agent_crud)
        self._run("5. Skill CRUD", self.test_skill_crud)
        self._run("6. Semantic Search", self.test_semantic_search)
        self._run("7. Security Scan", self.test_security_scan)
        self._run("8. External Server (aws-kb)", self.test_external_server)

        return self._print_report()

    def _print_report(self) -> bool:
        """Print the test report and return True if no test failed."""
        passed = sum(1 for r in self.results if r.status == TestStatus.PASSED)
        failed = sum(1 for r in self.results if r.status == TestStatus.FAILED)
        skipped = sum(1 for r in self.results if r.status == TestStatus.SKIPPED)
        total_time = sum(r.duration_ms for r in self.results)

        print("\n")
        print("=" * 74)
        print("              MCP GATEWAY REGISTRY - RELEASE SMOKE TEST REPORT")
        print("=" * 74)
        print(f"  Registry URL: {self.registry_url}")
        print(f"  Test Run:     {datetime.now().isoformat()}")
        print("=" * 74)
        print("\n  TEST RESULTS:")
        print("  " + "-" * 70)

        for result in self.results:
            if result.status == TestStatus.PASSED:
                color = "\033[92m"
            elif result.status == TestStatus.FAILED:
                color = "\033[91m"
            else:
                color = "\033[93m"
            status_str = f"{color}[{result.status.value}]\033[0m"
            print(f"  {status_str} {result.name:32} {result.duration_ms:>9.0f}ms")
            if result.message:
                print(f"        {result.message}")

        print("  " + "-" * 70)
        print("\n  SUMMARY:")
        print(f"    Total Tests:  {len(self.results)}")
        print(f"    \033[92mPassed:\033[0m       {passed}")
        print(f"    \033[91mFailed:\033[0m       {failed}")
        print(f"    \033[93mSkipped:\033[0m      {skipped}")
        print(f"    Total Time:   {total_time / 1000:.2f}s")

        if failed > 0:
            print(f"\n  \033[91m*** {failed} TEST(S) FAILED - DO NOT RELEASE ***\033[0m")
        else:
            print("\n  \033[92m*** ALL TESTS PASSED ***\033[0m")

        print("=" * 74)
        print()
        return failed == 0


class _SkipTest(Exception):
    """Raised inside a test to record a SKIPPED result instead of a failure."""


def _first_content_text(
    result: dict[str, Any],
) -> str:
    """Extract the text of the first content block from an MCP tool result."""
    content = result.get("content", [])
    if content and isinstance(content, list):
        return content[0].get("text", "")
    return ""


def _extract_json_field(
    text: str,
    field: str,
) -> Any:
    """Best-effort: parse ``text`` as JSON and return ``field`` (or 'n/a')."""
    try:
        return json.loads(text).get(field, "n/a")
    except (json.JSONDecodeError, AttributeError):
        return "n/a"


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Pre-release end-to-end smoke test for the MCP Gateway Registry.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Local gateway, admin token at ./.token
    uv run python tests/e2e_release_test.py

    # Remote gateway
    uv run python tests/e2e_release_test.py \\
        --registry-url https://your-gateway.example.com \\
        --token-file .oauth-tokens/ingress.json

    # Skip the external AWS test
    uv run python tests/e2e_release_test.py --skip-external
""",
    )
    parser.add_argument(
        "--registry-url",
        default="http://localhost",
        help="Gateway base URL (default: http://localhost)",
    )
    parser.add_argument(
        "--token-file",
        default=".token",
        help="Path to the admin/M2M token file (default: .token)",
    )
    parser.add_argument(
        "--scan-server-path",
        default=AIREGISTRY_TOOLS_PATH,
        help=f"Server path for the security-scan test (default: {AIREGISTRY_TOOLS_PATH})",
    )
    parser.add_argument(
        "--skip-external",
        action="store_true",
        help="Skip the external AWS knowledge-base server test",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> int:
    """Parse args, load the token, run the suite, and return an exit code."""
    args = _parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        token = _read_token(args.token_file)
        logger.info(f"Loaded token from {args.token_file}")
    except (FileNotFoundError, ValueError) as exc:
        logger.error(f"ERROR: {exc}")
        return 1

    runner = ReleaseE2ETest(
        registry_url=args.registry_url,
        token=token,
        skip_external=args.skip_external,
        scan_server_path=args.scan_server_path,
    )
    success = runner.run_all_tests()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
