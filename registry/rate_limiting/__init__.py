"""Application-level, identity/group/target-aware rate limiting for MCP and A2A calls.

This package enforces per-caller and per-target aggregate request limits at the
auth-server ``/validate`` chokepoint, correct across horizontally-scaled replicas
via shared counters in DocumentDB/MongoDB. It is complementary to (not a
replacement for) the coarse per-IP nginx edge limiting shipped in PR #1431.

See ``.scratchpad/issue-295/lld.md`` for the full design. The feature is off by
default (``RATE_LIMITING_ENABLED=false``) and adds no required infrastructure.
"""

from .backend import IncrResult, RateLimiterBackend
from .definitions_repository import DefinitionsRepository
from .documentdb_backend import DocumentDBRateLimiterBackend
from .limiter import RateLimiter
from .memberships_repository import MembershipsRepository
from .models import RateLimitDecision, RateLimitDefinition, RateLimitMembership

__all__ = [
    "IncrResult",
    "RateLimiterBackend",
    "DocumentDBRateLimiterBackend",
    "DefinitionsRepository",
    "MembershipsRepository",
    "RateLimiter",
    "RateLimitDecision",
    "RateLimitDefinition",
    "RateLimitMembership",
]
