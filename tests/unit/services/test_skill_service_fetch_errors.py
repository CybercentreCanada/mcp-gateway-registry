"""Tests for SKILL.md fetch credential handling, error messages, and frontmatter parsing.

These import the real service functions (the older test_skill_service_parsing.py
tests a local copy of the parser, not the service code).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from registry.exceptions import SkillUrlValidationError
from registry.services import skill_service as svc

GHES_URL = "https://github.chimera.cyber.gc.ca/CCCS/skills/blob/main/x/SKILL.md"
TOKEN = "github_pat_ABCDEF1234567890"


class TestNormalizeCredential:
    @pytest.mark.parametrize(
        "raw",
        [TOKEN, f"{TOKEN}\n", f"  {TOKEN} ", f"Bearer {TOKEN}", f"bearer  {TOKEN}\r\n"],
    )
    def test_strips_whitespace_and_bearer_prefix(self, raw):
        assert svc._normalize_credential(raw) == TOKEN

    @pytest.mark.parametrize("raw", [None, "", "   ", "Bearer "])
    def test_empty_becomes_none(self, raw):
        assert svc._normalize_credential(raw) is None


class TestBuildFetchHeaders:
    def test_bearer_header_has_clean_token(self):
        _, headers = svc._build_fetch_headers(GHES_URL, "bearer", f"Bearer {TOKEN}\n")
        assert headers == {"Authorization": f"Bearer {TOKEN}"}

    def test_blank_bearer_credential_sends_no_header(self):
        _, headers = svc._build_fetch_headers(GHES_URL, "bearer", "  ")
        assert headers == {}


class TestDescribeRequestError:
    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectTimeout(""),
            httpx.ReadTimeout(""),
            httpx.ConnectError(""),
            httpx.RemoteProtocolError(""),
            httpx.ReadError(""),
        ],
    )
    def test_never_blank(self, error):
        assert svc._describe_request_error(error).strip()

    def test_timeout_mentions_network_access(self):
        assert "network access" in svc._describe_request_error(httpx.ConnectTimeout(""))

    def test_never_echoes_credential_from_exception_text(self):
        error = httpx.LocalProtocolError(f"Illegal header value b'Bearer {TOKEN}\\n'")
        message = svc._describe_request_error(error)
        assert TOKEN not in message
        assert "Bearer" not in message


class TestDescribeHttpStatus:
    def test_404_with_bearer_is_actionable(self):
        message = svc._describe_http_status(404, "bearer")
        assert message.startswith("HTTP 404")
        assert "token" in message

    def test_404_without_token_scheme_is_plain(self):
        assert svc._describe_http_status(404, "none") == "HTTP 404"

    def test_500_is_plain(self):
        assert svc._describe_http_status(500, "bearer") == "HTTP 500"


def _mock_client(response=None, error=None):
    client = MagicMock()
    client.get = AsyncMock(return_value=response, side_effect=error)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _response(status=200, text="", url="https://raw.github.chimera.cyber.gc.ca/x"):
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.content = text.encode()
    response.url = url
    return response


@pytest.mark.asyncio
class TestParseSkillMdContent:
    async def _parse(self, client_ctx, **kwargs):
        with (
            patch.object(svc, "guarded_async_client", return_value=client_ctx),
            patch.object(svc, "_is_safe_url", return_value=True),
        ):
            return await svc._parse_skill_md_content(GHES_URL, **kwargs)

    async def test_block_style_tags_are_parsed(self):
        content = (
            "---\nname: demo\ndescription: d\ntags:\n  - triage\n  - KT:TIER:STABLE\n---\n# Demo\n"
        )
        ctx = _mock_client(_response(text=content, url="https://raw.github.chimera.cyber.gc.ca/x"))
        result = await self._parse(ctx)
        assert result["tags"] == ["triage", "KT:TIER:STABLE"]
        assert result["name"] == "demo"

    @pytest.mark.parametrize(
        ("tags_line", "expected"),
        [("tags: [a, b]", ["a", "b"]), ("tags: a, b", ["a", "b"]), ("tags:", [])],
    )
    async def test_other_tag_formats(self, tags_line, expected):
        content = f"---\nname: demo\n{tags_line}\n---\n# Demo\n"
        ctx = _mock_client(_response(text=content, url="https://raw.github.chimera.cyber.gc.ca/x"))
        assert (await self._parse(ctx))["tags"] == expected

    async def test_malformed_frontmatter_does_not_raise(self):
        content = "---\nname: [unclosed\n---\n# Demo\n"
        ctx = _mock_client(_response(text=content, url="https://raw.github.chimera.cyber.gc.ca/x"))
        assert (await self._parse(ctx))["tags"] == []

    async def test_timeout_yields_nonblank_reason(self):
        ctx = _mock_client(error=httpx.ConnectTimeout(""))
        with pytest.raises(SkillUrlValidationError) as exc:
            await self._parse(ctx)
        assert exc.value.reason.strip()

    async def test_404_with_bearer_gets_hint(self):
        ctx = _mock_client(_response(status=404, url="https://raw.github.chimera.cyber.gc.ca/x"))
        with pytest.raises(SkillUrlValidationError) as exc:
            await self._parse(ctx, auth_scheme="bearer", auth_credential=TOKEN)
        assert "token" in exc.value.reason

    async def test_trailing_newline_token_is_sent_clean(self):
        ctx = _mock_client(
            _response(text="# Demo\n", url="https://raw.github.chimera.cyber.gc.ca/x")
        )
        await self._parse(ctx, auth_scheme="bearer", auth_credential=f"{TOKEN}\n")
        sent = ctx.__aenter__.return_value.get.call_args.kwargs["headers"]
        assert sent["Authorization"] == f"Bearer {TOKEN}"
