"""Behavior tests for the openbao-init Job script embedded in the stack chart.

The script is extracted from charts/mcp-gateway-registry-stack/templates/openbao-init.yaml
and run as a subprocess against an in-process fake of the two APIs it talks to
(OpenBao and the Kubernetes API), so the init flow is exercised end to end
without a cluster. helm unittest only checks the rendered text.
"""

import base64
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent

import pytest

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "charts/mcp-gateway-registry-stack/templates/openbao-init.yaml"
)
NAMESPACE = "mcp"
KEYS_SECRET = "openbao-keys"
REVIEWER_SECRET = "openbao-reviewer-token"
REVIEWER_TOKEN = "reviewer-jwt-value"


def _extract_script() -> str:
    text = TEMPLATE.read_text()
    match = re.search(r"- python3\n\s+- -c\n\s+- \|\n(.*?)\n\{\{- end \}\}", text, re.DOTALL)
    assert match, "openbao-init Job no longer embeds a python3 -c script"
    return dedent(match.group(1)) + "\n"


class FakeCluster:
    """Minimal stateful fake of the OpenBao HTTP API and the Kubernetes secrets API."""

    def __init__(self, initialized: bool = False, sealed: bool = True) -> None:
        self.initialized = initialized
        self.sealed = sealed
        self.mounts: dict = {}
        self.auth_mounts: dict = {}
        self.policies: dict = {}
        self.auth_config: dict = {}
        self.roles: dict = {}
        self.secrets: dict = {}
        self.init_calls = 0
        self.unseal_key = "unseal-key-1"
        self.root_token = "root-token-1"

    def secret(self, name: str, **data: str) -> None:
        self.secrets[name] = {
            "metadata": {"name": name},
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
        }

    def decoded(self, name: str, key: str) -> str:
        return base64.b64decode(self.secrets[name]["data"][key]).decode()


def _handler(cluster: FakeCluster):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence request logging
            pass

        def _send(self, status: int, body: dict | None = None) -> None:
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length)) if length else {}

        def _route(self, method: str) -> None:
            path = self.path.split("?")[0]
            body = self._body() if method in ("POST", "PUT", "PATCH") else {}
            if path.startswith("/api/v1/namespaces/"):
                return self._k8s(method, path, body)
            return self._bao(method, path[len("/v1/") :], body)

        def _k8s(self, method: str, path: str, body: dict) -> None:
            assert self.headers["Authorization"] == "Bearer sa-token"
            prefix = f"/api/v1/namespaces/{NAMESPACE}/secrets"
            if method == "POST" and path == prefix:
                name = body["metadata"]["name"]
                if name in cluster.secrets:
                    return self._send(409, {"reason": "AlreadyExists"})
                if body.get("type") == "kubernetes.io/service-account-token":
                    body["data"] = {"token": base64.b64encode(REVIEWER_TOKEN.encode()).decode()}
                cluster.secrets[name] = body
                return self._send(201, body)
            name = path[len(prefix) + 1 :]
            if method == "GET":
                if name in cluster.secrets:
                    return self._send(200, cluster.secrets[name])
                return self._send(404, {"reason": "NotFound"})
            if method == "PATCH":
                cluster.secrets[name].update(body)
                return self._send(200, cluster.secrets[name])
            return self._send(405, {})

        def _bao(self, method: str, path: str, body: dict) -> None:
            if path == "sys/health":
                return self._send(200, {})
            if path == "sys/init" and method == "GET":
                return self._send(200, {"initialized": cluster.initialized})
            if path == "sys/init" and method == "PUT":
                cluster.init_calls += 1
                cluster.initialized = True
                return self._send(
                    200,
                    {"keys_base64": [cluster.unseal_key], "root_token": cluster.root_token},
                )
            if path == "sys/seal-status":
                return self._send(200, {"sealed": cluster.sealed})
            if path == "sys/unseal":
                assert body["key"] == cluster.unseal_key
                cluster.sealed = False
                return self._send(200, {"sealed": False})
            # Everything below needs the root token.
            assert self.headers.get("X-Vault-Token") == cluster.root_token, path
            if path == "sys/mounts" and method == "GET":
                return self._send(200, {"data": cluster.mounts})
            if path.startswith("sys/mounts/"):
                cluster.mounts[path[len("sys/mounts/") :] + "/"] = body
                return self._send(204)
            if path == "sys/auth" and method == "GET":
                return self._send(200, {"data": cluster.auth_mounts})
            if path.startswith("sys/auth/"):
                cluster.auth_mounts[path[len("sys/auth/") :] + "/"] = body
                return self._send(204)
            if path.startswith("sys/policies/acl/"):
                cluster.policies[path[len("sys/policies/acl/") :]] = body["policy"]
                return self._send(204)
            if re.fullmatch(r"auth/[^/]+/config", path):
                cluster.auth_config[path.split("/")[1]] = body
                return self._send(204)
            if re.fullmatch(r"auth/[^/]+/role/[^/]+", path):
                cluster.roles[path] = body
                return self._send(204)
            return self._send(404, {"errors": [path]})

        do_GET = lambda self: self._route("GET")  # noqa: E731
        do_POST = lambda self: self._route("POST")  # noqa: E731
        do_PUT = lambda self: self._route("PUT")  # noqa: E731
        do_PATCH = lambda self: self._route("PATCH")  # noqa: E731

    return Handler


@pytest.fixture
def run_script(tmp_path):
    servers: list[ThreadingHTTPServer] = []

    def _run(cluster: FakeCluster, **env_overrides: str) -> subprocess.CompletedProcess:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(cluster))
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"

        sa_dir = tmp_path / "sa"
        sa_dir.mkdir(exist_ok=True)
        (sa_dir / "token").write_text("sa-token")
        (sa_dir / "ca.crt").write_text("CA-PEM")

        script = (
            _extract_script()
            .replace('"/var/run/secrets/kubernetes.io/serviceaccount"', repr(str(sa_dir)))
            .replace('"https://kubernetes.default.svc"', repr(base))
            .replace('ssl.create_default_context(cafile=SA_DIR + "/ca.crt")', "None")
        )
        script_file = tmp_path / "init.py"
        script_file.write_text(script)

        env = {
            "BAO_ADDR": base,
            "NAMESPACE": NAMESPACE,
            "KEYS_SECRET": KEYS_SECRET,
            "KV_MOUNT": "secret",
            "EGRESS_PREFIX": "mcp/egress",
            "ROLE": "registry",
            "AUTH_MOUNT": "kubernetes",
            "REGISTRY_SA": "registry",
            "OPENBAO_SA": "openbao",
            "REVIEWER_SECRET": REVIEWER_SECRET,
            "OBO_CACHE_ENABLED": "false",
            "AUTH_SERVER_SA": "auth-server",
            "OBO_ROLE": "auth-server-obo-cache",
            **env_overrides,
        }
        return subprocess.run(
            [sys.executable, str(script_file)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    yield _run
    for server in servers:
        server.shutdown()


class TestFreshInstall:
    def test_initializes_unseals_and_configures(self, run_script):
        cluster = FakeCluster()
        result = run_script(cluster)

        assert result.returncode == 0, result.stderr
        assert cluster.init_calls == 1
        assert cluster.sealed is False
        # Keys persisted to the Secret before anything else depends on them.
        assert cluster.decoded(KEYS_SECRET, "unseal_key") == cluster.unseal_key
        assert cluster.decoded(KEYS_SECRET, "root_token") == cluster.root_token
        assert "secret/" in cluster.mounts
        assert cluster.mounts["secret/"]["options"] == {"version": "2"}
        assert 'path "secret/data/mcp/egress/*"' in cluster.policies["mcp-egress"]
        assert "kubernetes/" in cluster.auth_mounts

    def test_kubernetes_auth_uses_a_stable_reviewer_token(self, run_script):
        cluster = FakeCluster()
        assert run_script(cluster).returncode == 0

        config = cluster.auth_config["kubernetes"]
        assert config["token_reviewer_jwt"] == REVIEWER_TOKEN
        assert config["disable_iss_validation"] is True
        assert config["kubernetes_ca_cert"] == "CA-PEM"
        secret = cluster.secrets[REVIEWER_SECRET]
        assert secret["type"] == "kubernetes.io/service-account-token"
        assert secret["metadata"]["annotations"]["kubernetes.io/service-account.name"] == "openbao"

    def test_registry_role_is_bound_to_the_registry_service_account(self, run_script):
        cluster = FakeCluster()
        assert run_script(cluster).returncode == 0

        role = cluster.roles["auth/kubernetes/role/registry"]
        assert role["bound_service_account_names"] == "registry"
        assert role["bound_service_account_namespaces"] == NAMESPACE
        assert role["policies"] == "mcp-egress"

    def test_custom_auth_mount_is_used_everywhere(self, run_script):
        cluster = FakeCluster()
        assert run_script(cluster, AUTH_MOUNT="k8s-prod").returncode == 0

        assert "k8s-prod/" in cluster.auth_mounts
        assert "k8s-prod" in cluster.auth_config
        assert "auth/k8s-prod/role/registry" in cluster.roles
        assert "kubernetes/" not in cluster.auth_mounts


class TestOboCache:
    def test_disabled_by_default(self, run_script):
        cluster = FakeCluster()
        assert run_script(cluster).returncode == 0

        assert "mcp-obo-cache" not in cluster.policies
        assert "auth/kubernetes/role/auth-server-obo-cache" not in cluster.roles

    def test_enabled_creates_a_scoped_policy_and_role(self, run_script):
        cluster = FakeCluster()
        assert run_script(cluster, OBO_CACHE_ENABLED="true").returncode == 0

        policy = cluster.policies["mcp-obo-cache"]
        assert 'path "secret/data/mcp/egress/b2JvLWNhY2hl/*"' in policy
        assert "delete" not in policy
        role = cluster.roles["auth/kubernetes/role/auth-server-obo-cache"]
        assert role["bound_service_account_names"] == "auth-server"
        assert role["policies"] == "mcp-obo-cache"


class TestReruns:
    def test_initialized_vault_reuses_stored_keys_and_does_not_reinit(self, run_script):
        cluster = FakeCluster(initialized=True, sealed=True)
        cluster.secret(KEYS_SECRET, unseal_key=cluster.unseal_key, root_token=cluster.root_token)

        result = run_script(cluster)

        assert result.returncode == 0, result.stderr
        assert cluster.init_calls == 0
        assert cluster.sealed is False
        assert cluster.decoded(KEYS_SECRET, "root_token") == cluster.root_token

    def test_second_run_is_idempotent(self, run_script):
        cluster = FakeCluster()
        assert run_script(cluster).returncode == 0
        first_keys = dict(cluster.secrets[KEYS_SECRET]["data"])

        assert run_script(cluster).returncode == 0

        assert cluster.init_calls == 1
        assert cluster.secrets[KEYS_SECRET]["data"] == first_keys

    def test_initialized_vault_without_stored_keys_fails_loudly(self, run_script):
        cluster = FakeCluster(initialized=True)

        result = run_script(cluster)

        assert result.returncode != 0
        assert "unrecoverable" in result.stderr or "no keys" in result.stderr
