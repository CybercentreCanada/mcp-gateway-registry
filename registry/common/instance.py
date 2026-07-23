"""Per-instance identity for audit attribution.

Internal service tokens historically all carried the same ``sub`` (e.g.
``registry-service``), and audit records did not record which replica produced
them. In a horizontally-scaled deployment that makes an action performed by one
replica indistinguishable from any other's, undermining attribution during
incident response.

This module exposes exactly one blessed helper, :func:`resolve_instance_id`, so
the internal-token minter and the audit middleware label actions with the same
identifier. It is a non-sensitive label used only for attribution — never a
security gate — so it never raises and always returns a non-empty value.
"""

from __future__ import annotations

import os
import socket

_UNKNOWN_INSTANCE: str = "unknown"


def resolve_instance_id() -> str:
    """Resolve a stable per-instance identifier for audit attribution.

    Resolution order (first non-empty, stripped value wins):
        1. ``AUDIT_INSTANCE_ID`` — explicit operator override.
        2. ``HOSTNAME`` — set per-container by Docker and per-pod by Kubernetes.
        3. ``socket.gethostname()`` — bare-metal / local fallback.
        4. ``"unknown"`` — never fail; attribution degrades but callers still
           get a usable label.

    Returns:
        A non-empty instance label safe to embed in a JWT claim and audit log.
    """
    for candidate in (os.environ.get("AUDIT_INSTANCE_ID"), os.environ.get("HOSTNAME")):
        if candidate and candidate.strip():
            return candidate.strip()

    try:
        host = socket.gethostname()
    except OSError:
        host = ""

    return host.strip() if host and host.strip() else _UNKNOWN_INSTANCE
