"""WWW-Authenticate middleware for MCP-facing 401 responses.

Per RFC 9728 §5.1 and the MCP 2025-06-18 authorization spec, an MCP server
returning HTTP 401 MUST include a `WWW-Authenticate` header pointing at the
gateway's Protected Resource Metadata document.

This middleware adds the header on FastAPI 401 responses for paths that
participate in MCP discovery: per-server `/<server>/mcp` paths and the
`/oauth/token` and `/oauth/authorize` endpoints (which arrive in sub-issue E).

For nginx-emitted 401s on `/<server>/mcp` (the auth_request 401 handled by
the `@auth_error` named location), the header is added directly in nginx
config via the `{{MCP_RESOURCE_METADATA_URL}}` placeholder.
"""

import logging
import re
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


_MCP_PATH_PATTERN = re.compile(r"^/[^/]+/mcp(/.*)?$|^/oauth/(token|authorize)$")


class WWWAuthenticateMiddleware(BaseHTTPMiddleware):
    """Add `WWW-Authenticate` to 401 responses on MCP-facing paths.

    Built once at startup with the canonical resource_metadata URL so the
    header is byte-for-byte identical to the `resource` field returned by
    the PRM endpoint. Mismatch breaks Claude Code's discovery flow.
    """

    def __init__(
        self,
        app,
        resource_metadata_url: str,
    ):
        super().__init__(app)
        self._header_value = f'Bearer realm="mcp", resource_metadata="{resource_metadata_url}"'

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        response = await call_next(request)

        if response.status_code != 401:
            return response

        if not _MCP_PATH_PATTERN.match(request.url.path):
            return response

        response.headers["WWW-Authenticate"] = self._header_value
        return response
