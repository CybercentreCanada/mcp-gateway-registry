"""Regression tests for the /validate rate-limit import context (issue #295).

Background: the deployed auth-server container runs ``server.py`` with ``/app``
as the module root, so sibling modules must be importable **top-level**
(``import rate_limiting_config``), NOT as ``auth_server.rate_limiting_config``.
An earlier revision imported the package path only, which passed the repo-root
test suite but raised ``ModuleNotFoundError`` inside the container on every
``/validate`` call -- breaking login (login depends on ``/validate``).

These tests reproduce the container's top-level import context (the
``tests/auth_server`` conftest puts ``auth_server/`` on ``sys.path``) and assert
the enforcement hook's import resolves there, so the regression cannot recur.
"""

import ast
import inspect
from pathlib import Path

import pytest

# Path to the auth_server package source (not the installed/repo-root view).
AUTH_SERVER_DIR = Path(__file__).parent.parent.parent.parent / "auth_server"


@pytest.mark.unit
class TestTopLevelImportContext:
    """The container imports sibling modules top-level; these must work that way."""

    def test_rate_limiting_config_imports_top_level(self):
        """`import rate_limiting_config` (container style) exposes the names /validate needs."""
        # The auth_server conftest has already inserted auth_server/ on sys.path,
        # which is exactly how the container sees /app. A top-level import here
        # reproduces the container context that the repo-root suite does not.
        import rate_limiting_config

        assert hasattr(rate_limiting_config, "RATE_LIMITING_ENABLED")
        assert callable(rate_limiting_config.get_rate_limiter)

    def test_enforce_rate_limit_import_resolves_in_container_context(self):
        """The exact import statement inside _enforce_rate_limit must resolve top-level.

        We parse the function's source, extract the import it performs, and execute
        the top-level form. If someone changes it back to an unqualified
        ``auth_server.``-only import, this fails with ModuleNotFoundError -- the
        same failure the container hit.
        """
        from auth_server.server import _enforce_rate_limit

        source = inspect.getsource(_enforce_rate_limit)
        tree = ast.parse(source.strip())

        # Collect every module referenced by an `import ... from` inside the function.
        imported_modules = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]

        # The function must reference the top-level (container) module name, not
        # only the auth_server-qualified path.
        assert "rate_limiting_config" in imported_modules, (
            "_enforce_rate_limit must import 'rate_limiting_config' top-level "
            "(container runs server.py with /app as root); "
            f"found imports: {imported_modules}"
        )

    def test_no_auth_server_only_import_at_module_top(self):
        """server.py must not import auth_server.rate_limiting_config at module top level.

        A top-of-module `from auth_server...` (outside a try/except fallback) would
        fail at container import time. The enforcement hook uses a lazy dual-context
        import instead; guard that the pattern is not reintroduced unguarded.
        """
        server_source = (AUTH_SERVER_DIR / "server.py").read_text()
        tree = ast.parse(server_source)

        # Module-level ImportFrom nodes (direct children of the module body).
        top_level_imports = [
            node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
        ]
        assert "auth_server.rate_limiting_config" not in top_level_imports, (
            "auth_server.rate_limiting_config must not be imported at module top level "
            "(it fails in the container); use the lazy dual-context import inside "
            "_enforce_rate_limit instead"
        )
