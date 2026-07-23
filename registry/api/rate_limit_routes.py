"""Admin CRUD + status endpoints for rate-limit definitions (issue #295).

Definitions live in the ``mcp_rate_limits`` collection and are enforced at the
auth-server ``/validate`` hop. All mutating endpoints are admin-only, mirroring
``m2m_management_routes.py``. The ``_id`` is always derived server-side from the
definition body (``<axis>:<entity_type>:<name>:<window_seconds>``); the URL id on
PUT is only a consistency check, never parsed into fields (names may contain
colons).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

from registry.audit.context import set_audit_action
from registry.auth.dependencies import nginx_proxied_auth
from registry.core.config import settings
from registry.rate_limiting.definitions_repository import DefinitionsRepository
from registry.rate_limiting.memberships_repository import MembershipsRepository
from registry.rate_limiting.models import (
    UNENFORCED_ENTITY_TYPES,
    RateLimitDefinition,
    RateLimitMembership,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_RESOURCE_TYPE: str = "rate_limit"


def _require_admin(
    user_context: dict | None,
) -> None:
    """Enforce admin permission or raise 401/403."""
    if not user_context:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user_context.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator permissions are required")


# Floor is a per-minute rate; enforced only on short windows so daily/hourly
# volume caps (which are legitimately below a per-minute floor) are exempt.
_FLOOR_WINDOW_SECONDS: int = 60


def _validate_enforceable(
    definition: RateLimitDefinition,
) -> None:
    """Reject definitions whose entity_type is not yet enforced (fail-closed footgun guard).

    ``mcp_tool`` / ``a2a_skill`` are modeled but their classifier is a later phase,
    so accepting them would create a silently-inert limit. Reject with a clear message.
    """
    if definition.entity_type in UNENFORCED_ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"entity_type '{definition.entity_type}' is not enforced in this version "
                f"(tool/skill rate limiting is a later phase)"
            ),
        )


def _validate_caller_floor(
    definition: RateLimitDefinition,
) -> None:
    """Reject a group definition whose short-window limit is below its caller-type floor.

    Lockout safeguard: on windows <= 60s, a group's ``user_max_requests`` must be
    >= the user floor and ``agent_max_requests`` >= the agent floor (both config-only,
    no API). Longer windows (hourly/daily volume caps) are exempt because a low
    per-day cap is a legitimate limit that a per-minute floor should not block.
    """
    if definition.axis != "caller" or definition.window_seconds > _FLOOR_WINDOW_SECONDS:
        return

    user_floor = settings.rate_limit_user_floor_per_min
    agent_floor = settings.rate_limit_agent_floor_per_min

    if definition.user_max_requests is not None and definition.user_max_requests < user_floor:
        raise HTTPException(
            status_code=400,
            detail=(
                f"user_max_requests {definition.user_max_requests} is below the user floor "
                f"of {user_floor}/min for windows <= {_FLOOR_WINDOW_SECONDS}s. Set it to at "
                f"least {user_floor} (the floor is a fixed config safeguard against lockout)."
            ),
        )
    if definition.agent_max_requests is not None and definition.agent_max_requests < agent_floor:
        raise HTTPException(
            status_code=400,
            detail=(
                f"agent_max_requests {definition.agent_max_requests} is below the agent floor "
                f"of {agent_floor}/min for windows <= {_FLOOR_WINDOW_SECONDS}s. Set it to at "
                f"least {agent_floor} (the floor is a fixed config safeguard against lockout)."
            ),
        )


def _parse_definition(
    body: dict,
) -> RateLimitDefinition:
    """Parse a request body into a RateLimitDefinition or raise 400."""
    try:
        return RateLimitDefinition(**body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid rate-limit definition: {exc}"
        ) from exc


_repository: DefinitionsRepository | None = None
_memberships_repository: MembershipsRepository | None = None


def _get_repository() -> DefinitionsRepository:
    """Return a shared DefinitionsRepository singleton for the registry process."""
    global _repository
    if _repository is None:
        _repository = DefinitionsRepository()
    return _repository


def _get_memberships_repository() -> MembershipsRepository:
    """Return a shared MembershipsRepository singleton for the registry process."""
    global _memberships_repository
    if _memberships_repository is None:
        _memberships_repository = MembershipsRepository()
    return _memberships_repository


def _parse_membership(
    body: dict,
) -> RateLimitMembership:
    """Parse a request body into a RateLimitMembership or raise 400."""
    try:
        return RateLimitMembership(**body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid rate-limit membership: {exc}"
        ) from exc


class _DeleteResponse(BaseModel):
    """Response body for a delete operation."""

    deleted: bool


@router.get("/rate-limits")
async def list_rate_limits(
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """List all rate-limit definitions (admin only)."""
    _require_admin(user_context)
    set_audit_action(request, "list", _RESOURCE_TYPE)
    definitions = await _get_repository().list_all()
    return {"definitions": [d.model_dump() for d in definitions]}


@router.get("/rate-limits/{definition_id:path}")
async def get_rate_limit(
    definition_id: str,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """Read a single rate-limit definition by id (admin only). 404 if absent."""
    _require_admin(user_context)
    set_audit_action(request, "read", _RESOURCE_TYPE, resource_id=definition_id)
    definition = await _get_repository().get_by_id(definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Definition not found")
    return definition.model_dump()


@router.post("/rate-limits-enabled/{definition_id:path}")
async def set_rate_limit_enabled(
    definition_id: str,
    request: Request,
    enabled: Annotated[bool, Query()],
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """Enable or disable a definition in place without re-specifying it (admin only).

    A distinct ``/rate-limits-enabled/`` prefix avoids the greedy ``:path`` under
    ``/rate-limits/`` swallowing a trailing action segment.
    """
    _require_admin(user_context)
    action = "enable" if enabled else "disable"
    set_audit_action(request, action, _RESOURCE_TYPE, resource_id=definition_id)
    updated = await _get_repository().set_enabled(definition_id, enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="Definition not found")
    return updated.model_dump()


@router.put("/rate-limits/{definition_id:path}")
async def put_rate_limit(
    definition_id: str,
    body: dict,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """Create or update a rate-limit definition (admin only).

    The ``_id`` is derived from the body fields; the URL ``{definition_id}`` must
    match it exactly or the request is rejected (400). The URL id is never split.
    """
    _require_admin(user_context)
    set_audit_action(request, "update", _RESOURCE_TYPE, resource_id=definition_id)
    definition = _parse_definition(body)
    _validate_enforceable(definition)
    _validate_caller_floor(definition)

    built_id = definition.build_id()
    if built_id != definition_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"URL id '{definition_id}' does not match the definition "
                f"'{built_id}' derived from the body"
            ),
        )

    stored = await _get_repository().upsert(definition)
    return stored.model_dump()


@router.delete("/rate-limits/{definition_id:path}")
async def delete_rate_limit(
    definition_id: str,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> _DeleteResponse:
    """Delete a rate-limit definition by id (admin only)."""
    _require_admin(user_context)
    set_audit_action(request, "delete", _RESOURCE_TYPE, resource_id=definition_id)
    deleted = await _get_repository().delete(definition_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Definition not found")
    return _DeleteResponse(deleted=True)


@router.get("/rate-limits-status")
async def rate_limit_status(
    request: Request,
    identity: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    name: Annotated[str | None, Query()] = None,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """Introspect current definitions for a caller and/or a target entity (admin only).

    Returns the matching definitions (windows and limits) without consuming any
    counter. Counter-value introspection is a later enhancement.
    """
    _require_admin(user_context)
    set_audit_action(request, "read", _RESOURCE_TYPE)
    repository = _get_repository()

    result: dict = {"caller": [], "target": []}
    if identity:
        # The caller's group definitions are what governs them; the caller passes
        # the group name(s) via identity for introspection.
        caller_defs = await repository.list_caller_limits("group", [identity])
        result["caller"] = [d.model_dump() for d in caller_defs]
    if entity_type and name:
        target_defs = await repository.list_target_limits(entity_type, name)
        result["target"] = [d.model_dump() for d in target_defs]
    return result


# ---------------------------------------------------------------------------
# Rate-limit MEMBERSHIPS: which caller (user/client) belongs to which rate-limit
# group. This is the ONLY source of a caller's rate-limit groups (no IdP emits
# them; kept separate from authz groups). Admin-only, mirroring the definitions.
# ---------------------------------------------------------------------------

_MEMBERSHIP_RESOURCE_TYPE: str = "rate_limit_membership"


@router.get("/rate-limit-memberships")
async def list_rate_limit_memberships(
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """List all rate-limit memberships (admin only)."""
    _require_admin(user_context)
    set_audit_action(request, "list", _MEMBERSHIP_RESOURCE_TYPE)
    memberships = await _get_memberships_repository().list_all()
    return {"memberships": [m.model_dump() for m in memberships]}


@router.get("/rate-limit-memberships/{membership_id:path}")
async def get_rate_limit_membership(
    membership_id: str,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """Read a single membership by id ('<subject_type>:<subject>'); 404 if absent (admin only)."""
    _require_admin(user_context)
    set_audit_action(request, "read", _MEMBERSHIP_RESOURCE_TYPE, resource_id=membership_id)
    membership = await _get_memberships_repository().get_by_id(membership_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    return membership.model_dump()


@router.put("/rate-limit-memberships/{membership_id:path}")
async def put_rate_limit_membership(
    membership_id: str,
    body: dict,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> dict:
    """Create or update a membership (admin only).

    The ``_id`` is derived from the body (``<subject_type>:<subject>``); the URL id
    must match it exactly (a colon-bearing subject is never parsed out of the URL).
    """
    _require_admin(user_context)
    set_audit_action(request, "update", _MEMBERSHIP_RESOURCE_TYPE, resource_id=membership_id)
    membership = _parse_membership(body)

    built_id = membership.build_id()
    if built_id != membership_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"URL id '{membership_id}' does not match the membership "
                f"'{built_id}' derived from the body"
            ),
        )

    stored = await _get_memberships_repository().upsert(membership)
    return stored.model_dump()


@router.delete("/rate-limit-memberships/{membership_id:path}")
async def delete_rate_limit_membership(
    membership_id: str,
    request: Request,
    user_context: Annotated[dict | None, Depends(nginx_proxied_auth)] = None,
) -> _DeleteResponse:
    """Delete a membership by id (admin only)."""
    _require_admin(user_context)
    set_audit_action(request, "delete", _MEMBERSHIP_RESOURCE_TYPE, resource_id=membership_id)
    deleted = await _get_memberships_repository().delete(membership_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Membership not found")
    return _DeleteResponse(deleted=True)
