"""Tests for the public /rum.js frontend asset route (issue #1471).

The /rum.js route serves the operator-supplied Real User Monitoring (RUM)
snippet as JavaScript. The container entrypoint writes the file (an empty stub
when unconfigured), and FastAPI serves it. The route is declared before the SPA
catch-all so it wins over index.html.

Test harness notes:
- These tests follow the same pattern as tests/unit/api/test_update_check_route.py:
  import the shared ``app`` from ``registry.main`` and drive it with a
  ``TestClient``.
- The /rum.js route is only registered when ``FRONTEND_BUILD_PATH`` exists at
  import time. In the test environment the frontend build directory is present,
  so the route is registered. To control whether the served file exists (and its
  contents) without touching the real build tree, the tests monkeypatch the
  module-level ``registry.main.FRONTEND_BUILD_PATH`` to a temporary directory.
  The route reads that global on each request, so patching it is sufficient.
- If the other agent's route implementation is not yet present, the route is not
  registered and requests fall through to the SPA catch-all (returning
  index.html) or 404. Those cases are called out per test; they are expected to
  fail until integration and are not a defect in these tests.
"""

from pathlib import Path

import pytest
from fastapi import status
from fastapi.testclient import TestClient

import registry.main as registry_main
from registry.main import app

RUM_JS_CONTENT: str = '<script>window.__RUM_TEST__=true;console.log("rum loaded");</script>'


def _write_rum_js(
    build_dir: Path,
    content: str,
) -> Path:
    """Write a rum.js file into the given build directory.

    Args:
        build_dir: Directory that stands in for FRONTEND_BUILD_PATH.
        content: JavaScript/HTML content to write into rum.js.

    Returns:
        Path to the written rum.js file.
    """
    rum_path = build_dir / "rum.js"
    rum_path.write_text(content)
    return rum_path


@pytest.mark.unit
@pytest.mark.api
class TestRumRoute:
    """Tests for GET /rum.js."""

    def test_rum_js_served_when_file_exists(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """When rum.js exists, GET /rum.js returns 200 with the file contents.

        Content-Type must be application/javascript.
        """
        _write_rum_js(tmp_path, RUM_JS_CONTENT)
        monkeypatch.setattr(registry_main, "FRONTEND_BUILD_PATH", tmp_path)

        client = TestClient(app)
        response = client.get("/rum.js")

        assert response.status_code == status.HTTP_200_OK
        assert response.text == RUM_JS_CONTENT
        assert "application/javascript" in response.headers["content-type"]

    def test_rum_js_content_type_not_html(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """Content-Type is application/javascript, NOT text/html.

        A text/html content type would mean the SPA catch-all served index.html,
        proving the concrete /rum.js route did not win.
        """
        _write_rum_js(tmp_path, RUM_JS_CONTENT)
        monkeypatch.setattr(registry_main, "FRONTEND_BUILD_PATH", tmp_path)

        client = TestClient(app)
        response = client.get("/rum.js")

        content_type = response.headers["content-type"]
        assert "application/javascript" in content_type
        assert "text/html" not in content_type

    def test_rum_js_route_registered_before_catchall(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """GET /rum.js does not return index.html content.

        The SPA catch-all returns an HTML document beginning with
        ``<!DOCTYPE html>``. The RUM script must never contain that marker.
        """
        _write_rum_js(tmp_path, RUM_JS_CONTENT)
        monkeypatch.setattr(registry_main, "FRONTEND_BUILD_PATH", tmp_path)

        client = TestClient(app)
        response = client.get("/rum.js")

        assert response.status_code == status.HTTP_200_OK
        assert "<!DOCTYPE html>" not in response.text
        assert "<!doctype html>" not in response.text.lower()

    def test_rum_js_missing_file_returns_empty_script(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """When rum.js is absent, the route returns a 200 empty/comment JS.

        Dev safety: the route must never 500 just because the file is missing;
        it returns a harmless comment script instead.
        """
        # tmp_path intentionally does NOT contain rum.js.
        monkeypatch.setattr(registry_main, "FRONTEND_BUILD_PATH", tmp_path)

        client = TestClient(app)
        response = client.get("/rum.js")

        assert response.status_code == status.HTTP_200_OK
        assert "application/javascript" in response.headers["content-type"]
        assert "text/html" not in response.headers["content-type"]
        # A comment-only / empty stub, never an HTML document.
        assert "<!DOCTYPE html>" not in response.text
        assert response.text.strip().startswith("//")

    def test_rum_js_has_cache_control(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """The response carries a Cache-Control header with max-age=300."""
        _write_rum_js(tmp_path, RUM_JS_CONTENT)
        monkeypatch.setattr(registry_main, "FRONTEND_BUILD_PATH", tmp_path)

        client = TestClient(app)
        response = client.get("/rum.js")

        assert response.status_code == status.HTTP_200_OK
        cache_control = response.headers.get("cache-control", "")
        assert "max-age=300" in cache_control
