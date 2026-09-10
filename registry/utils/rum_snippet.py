"""Process the operator-supplied RUM (Real User Monitoring) snippet.

The RUM snippet is arbitrary HTML/JavaScript provided by the operator via the
``RUM_SNIPPET_B64`` environment variable (base64-encoded). It is written to
``/app/frontend/build/rum.js`` at container start and served at ``/rum.js`` where
it runs in every user's browser.

This is a deploy-time trust boundary: only someone who can already set the
container environment (image, secrets, deployment config) can set it, so it is
the same trust tier as ``SECRET_KEY``. The host allowlist here is NOT a control
against a malicious operator; it is a fail-closed guardrail against
misconfiguration or tampering, and it makes the reachable RUM hosts auditable.

Design decisions:
- Fail closed: on invalid base64, or when the snippet references a host that is
  not on a non-empty allowlist, we return an empty stub and log an error rather
  than serve an unexpected snippet.
- The allowlist is disabled when empty (``RUM_ALLOWED_HOSTS`` unset), preserving
  the default "operator is trusted, serve whatever they configured" behavior.
"""

import base64
import binascii
import logging
import re

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


EMPTY_STUB: str = "// no RUM snippet configured\n"
INVALID_STUB: str = "// invalid or disallowed RUM_SNIPPET_B64; RUM disabled\n"

# Matches http(s) URLs so we can extract the host component for allowlisting.
_URL_PATTERN: re.Pattern[str] = re.compile(
    r"https?://([^/\s'\"()]+)",
    re.IGNORECASE,
)


def _decode_snippet(
    snippet_b64: str,
) -> str | None:
    """Decode the base64 snippet. Return None on invalid base64.

    Args:
        snippet_b64: Base64-encoded snippet from the environment.

    Returns:
        The decoded snippet text, or None if the value is not valid base64.
    """
    try:
        # validate=True rejects non-base64 characters instead of silently
        # ignoring them, so a garbled value fails closed here.
        decoded_bytes = base64.b64decode(snippet_b64, validate=True)
        return decoded_bytes.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        logger.error("RUM: RUM_SNIPPET_B64 is not valid base64/UTF-8: %s", e)
        return None


def _parse_allowlist(
    allowed_hosts: str,
) -> set[str]:
    """Parse the comma-separated host allowlist into a normalized set.

    Args:
        allowed_hosts: Comma-separated hosts, e.g. "cdn.signalfx.com,rum-ingest.signalfx.com".

    Returns:
        Set of lower-cased, stripped host names (empty if none configured).
    """
    hosts: set[str] = set()
    for raw_host in allowed_hosts.split(","):
        host = raw_host.strip().lower()
        if host:
            hosts.add(host)
    return hosts


def _extract_hosts(
    snippet: str,
) -> set[str]:
    """Extract the http(s) host names referenced by the snippet.

    Args:
        snippet: Decoded snippet HTML/JS.

    Returns:
        Set of lower-cased host names (without scheme, port, or path).
    """
    hosts: set[str] = set()
    for match in _URL_PATTERN.finditer(snippet):
        # Strip any credentials (user:pass@) and port from the authority.
        authority = match.group(1)
        host = authority.rsplit("@", 1)[-1].split(":", 1)[0].lower()
        if host:
            hosts.add(host)
    return hosts


def _find_disallowed_hosts(
    snippet: str,
    allowed_hosts: set[str],
) -> set[str]:
    """Return the referenced hosts that are not on the allowlist.

    Args:
        snippet: Decoded snippet text.
        allowed_hosts: Normalized allowlist (empty means "check disabled").

    Returns:
        Set of disallowed hosts (empty if all allowed or the check is disabled).
    """
    if not allowed_hosts:
        return set()

    referenced = _extract_hosts(snippet)
    return {host for host in referenced if host not in allowed_hosts}


def resolve_rum_snippet(
    snippet_b64: str,
    allowed_hosts: str = "",
) -> str:
    """Resolve the RUM snippet content to write to rum.js, failing closed.

    Args:
        snippet_b64: Base64-encoded snippet from RUM_SNIPPET_B64 (may be empty).
        allowed_hosts: Comma-separated host allowlist from RUM_ALLOWED_HOSTS.

    Returns:
        The snippet text to serve. Returns the empty stub when unconfigured and
        the invalid stub when the value cannot be decoded or references a
        disallowed host.
    """
    if not snippet_b64:
        logger.info("RUM: no snippet configured; serving empty stub")
        return EMPTY_STUB

    snippet = _decode_snippet(snippet_b64)
    if snippet is None:
        # _decode_snippet already logged the reason.
        return INVALID_STUB

    disallowed = _find_disallowed_hosts(snippet, _parse_allowlist(allowed_hosts))
    if disallowed:
        # Never log the snippet contents (may carry a token); log only the
        # offending host names so the operator can fix the allowlist.
        logger.error(
            "RUM: snippet references host(s) not in RUM_ALLOWED_HOSTS: %s; "
            "serving empty stub (fail closed)",
            ", ".join(sorted(disallowed)),
        )
        return INVALID_STUB

    logger.info("RUM: snippet accepted (%d bytes)", len(snippet))
    return snippet
