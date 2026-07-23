"""Pydantic models for rate-limit definitions and decisions.

A definition is described by two orthogonal dimensions plus a window:

- ``axis``: which side of the call the limit applies to (``caller`` or ``target``).
- ``entity_type``: what kind of thing on that axis. Allowlisted per axis so the
  model is not tied to any single target kind (MCP server, A2A agent, and later
  tool/skill all plug in without a schema change).
- ``window_seconds``: the fixed window. Ranges from seconds to a full day, so a
  per-day volume cap is the same mechanism as a per-second burst cap.

See ``.scratchpad/issue-295/lld.md`` (Data Models) for the full rationale.
"""

import logging

from pydantic import BaseModel, Field, model_validator

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)


# Allowlisted entity types per axis. Extend these as new entity kinds gain a
# gateway chokepoint. Unknown values are rejected in the validator (fail closed).
#   Caller kind (name from the validated token's groups claim):
#     "group" -> a group the caller belongs to. A specific user or agent is made
#                subject to a group limit by being a MEMBER of that group (via the
#                IAM membership APIs + token group enrichment), not by naming the
#                user/client directly here.
#   Target kinds:
#     Coarse (name = path-derived):  "mcp_server", "a2a_agent"
#     Fine   (name = "<parent>:<leaf>", from the JSON-RPC payload; later phase,
#             NOT enforced in v1): "mcp_tool", "a2a_skill"
CALLER_ENTITY_TYPES: frozenset[str] = frozenset({"group"})
TARGET_ENTITY_TYPES: frozenset[str] = frozenset(
    {"mcp_server", "a2a_agent", "mcp_tool", "a2a_skill"}
)

# Fine-grained target kinds whose enforcement classifier is not wired in v1.
# The admin API accepts these only when explicitly acknowledged, so an operator
# never gets a silently-inert limit. See lld.md API Design.
UNENFORCED_ENTITY_TYPES: frozenset[str] = frozenset({"mcp_tool", "a2a_skill"})

# Axis abbreviations used in counter keys (kept short and stable).
AXIS_ABBREVIATION: dict[str, str] = {"caller": "clr", "target": "tgt"}

# Bounds for window_seconds: 1 second up to one full day.
MIN_WINDOW_SECONDS: int = 1
MAX_WINDOW_SECONDS: int = 86400


# Caller types for floor selection and per-type limit resolution.
CALLER_TYPE_USER: str = "user"
CALLER_TYPE_AGENT: str = "agent"


class RateLimitDefinition(BaseModel):
    """A rate-limit definition on one axis, for one entity type, at one window.

    - ``axis="caller"``: aggregate limit on a group the caller belongs to. A group
      carries a SEPARATE limit for human users (``user_max_requests``) and for
      agents/M2M clients (``agent_max_requests``); at least one is required, both
      are allowed. A specific user/agent is subject to the group by a membership.
    - ``axis="target"``: aggregate limit on one target entity across all callers
      via ``max_requests`` (v1 enforced: an MCP server or an A2A agent; tool/skill
      modeled but not wired).
    """

    axis: str = Field(
        ...,
        description="Which side of the call: 'caller' or 'target'",
    )
    entity_type: str = Field(
        ...,
        description="caller: 'group'; target: 'mcp_server' | 'a2a_agent' | 'mcp_tool' | 'a2a_skill'",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Group name, server path, or agent name this applies to",
    )
    # Target axis uses this single aggregate limit.
    max_requests: int | None = Field(
        default=None,
        ge=1,
        description="Target-axis: max requests per window across all callers",
    )
    # Caller (group) axis uses these per-caller-type limits. At least one required.
    user_max_requests: int | None = Field(
        default=None,
        ge=1,
        description="Caller/group: max requests per window for a human user in the group",
    )
    agent_max_requests: int | None = Field(
        default=None,
        ge=1,
        description="Caller/group: max requests per window for an agent/client in the group",
    )
    window_seconds: int = Field(
        default=60,
        ge=MIN_WINDOW_SECONDS,
        le=MAX_WINDOW_SECONDS,
        description="Window length in seconds (up to one day)",
    )
    fail_closed: bool = Field(
        default=False,
        description="If true, deny on backend error (security-critical only)",
    )
    enabled: bool = Field(
        default=True,
        description="Toggle without deleting",
    )

    @model_validator(mode="after")
    def _check_axis_and_entity_type(self) -> "RateLimitDefinition":
        """Validate axis/entity_type allowlists and the per-axis limit fields (fail closed)."""
        if self.axis not in AXIS_ABBREVIATION:
            raise ValueError(f"axis must be one of {sorted(AXIS_ABBREVIATION)}, got '{self.axis}'")

        allowed = CALLER_ENTITY_TYPES if self.axis == "caller" else TARGET_ENTITY_TYPES
        if self.entity_type not in allowed:
            raise ValueError(
                f"entity_type '{self.entity_type}' invalid for axis '{self.axis}' "
                f"(allowed: {sorted(allowed)})"
            )

        if self.axis == "caller":
            # A group must set at least one of the per-caller-type limits.
            if self.user_max_requests is None and self.agent_max_requests is None:
                raise ValueError(
                    "a caller (group) definition must set user_max_requests and/or "
                    "agent_max_requests"
                )
        else:
            # Target axis uses the single max_requests.
            if self.max_requests is None:
                raise ValueError("a target definition must set max_requests")
        return self

    def limit_for_caller_type(
        self,
        caller_type: str,
    ) -> int | None:
        """Return the per-window limit for a caller type ('user'/'agent'), or None if unset."""
        if caller_type == CALLER_TYPE_AGENT:
            return self.agent_max_requests
        return self.user_max_requests

    def build_id(self) -> str:
        """Build the definition document ``_id``: '<axis>:<entity_type>:<name>:<window_seconds>'."""
        return f"{self.axis}:{self.entity_type}:{self.name}:{self.window_seconds}"


# Subject kinds for a rate-limit membership: a human user (by username) or an
# agent / M2M client (by client_id). Rejected if outside this set (fail closed).
MEMBERSHIP_SUBJECT_TYPES: frozenset[str] = frozenset({"user", "client"})


class RateLimitMembership(BaseModel):
    """Maps a caller (a user or an agent/client) to rate-limit group name(s).

    This is deliberately SEPARATE from the token's authz groups: no IdP emits
    rate-limit groups, and mixing them into the authz groups list could change a
    caller's scopes. The limiter reads *only* this collection to resolve which
    rate-limit ``group`` definitions apply to a caller; it never affects
    authorization.
    """

    subject_type: str = Field(
        ...,
        description="'user' (name = username) or 'client' (name = client_id / azp)",
    )
    subject: str = Field(
        ...,
        min_length=1,
        description="The username or client_id this membership applies to",
    )
    groups: list[str] = Field(
        default_factory=list,
        description="Rate-limit group names this subject belongs to",
    )

    @model_validator(mode="after")
    def _check_subject_type(self) -> "RateLimitMembership":
        """Reject a subject_type outside the allowlist (fail closed)."""
        if self.subject_type not in MEMBERSHIP_SUBJECT_TYPES:
            raise ValueError(
                f"subject_type must be one of {sorted(MEMBERSHIP_SUBJECT_TYPES)}, "
                f"got '{self.subject_type}'"
            )
        return self

    def build_id(self) -> str:
        """Build the membership document ``_id``: '<subject_type>:<subject>'."""
        return f"{self.subject_type}:{self.subject}"


class RateLimitDecision(BaseModel):
    """The result of enforcing a single gate (or the aggregate allow).

    ``reset_epoch`` is used to pick the friendliest ``Retry-After`` when several
    gates deny at once: the caller should back off until the gate with the
    longest reset clears, not a short burst window.
    """

    allowed: bool
    axis: str | None = None
    entity_type: str | None = None
    limit: int | None = None
    remaining: int = 0
    reset_epoch: int = 0
    retry_after: int = 0

    @classmethod
    def allow(
        cls,
        remaining: int = 0,
    ) -> "RateLimitDecision":
        """Build an allow decision. ``remaining`` is best-effort headroom for headers."""
        return cls(allowed=True, remaining=max(0, remaining))

    def headers(self) -> dict[str, str]:
        """Build the ``X-RateLimit-*`` / ``Retry-After`` headers for a throttle response.

        ``X-RateLimit-Throttled`` is a marker the nginx ``auth_request`` layer uses
        to tell a throttle-403 apart from a genuine authorization 403: the throttle
        response leaves ``/validate`` as a 403 (the only non-2xx status other than
        401 that ``auth_request`` forwards), and nginx rewrites it back into a real
        429 with these headers for the client. See the ``@forbidden_error`` named
        location in the nginx templates.
        """
        return {
            "X-RateLimit-Throttled": "1",
            "X-RateLimit-Limit": str(self.limit if self.limit is not None else 0),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(self.reset_epoch),
            "Retry-After": str(self.retry_after),
            "Connection": "close",
        }
