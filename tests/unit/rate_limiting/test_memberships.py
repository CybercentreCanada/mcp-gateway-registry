"""Unit tests for RateLimitMembership model and the group-resolution contract (#295).

Rate-limit groups come ONLY from memberships (keyed by username/client_id), never
from the token. These cover the model validation and the union/dedup resolution.
"""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from registry.rate_limiting.memberships_repository import MembershipsRepository
from registry.rate_limiting.models import RateLimitMembership


class TestRateLimitMembershipModel:
    """Model validation and id construction."""

    def test_valid_user_membership(self):
        m = RateLimitMembership(subject_type="user", subject="alice", groups=["devs"])
        assert m.build_id() == "user:alice"
        assert m.groups == ["devs"]

    def test_valid_client_membership(self):
        m = RateLimitMembership(subject_type="client", subject="agent-1", groups=["agents", "beta"])
        assert m.build_id() == "client:agent-1"

    def test_invalid_subject_type_rejected(self):
        with pytest.raises(ValidationError, match="subject_type must be one of"):
            RateLimitMembership(subject_type="group", subject="x", groups=["g"])

    def test_empty_subject_rejected(self):
        with pytest.raises(ValidationError):
            RateLimitMembership(subject_type="user", subject="", groups=["g"])

    def test_groups_default_empty(self):
        m = RateLimitMembership(subject_type="user", subject="alice")
        assert m.groups == []


class TestGroupResolution:
    """MembershipsRepository.get_groups_for resolution semantics."""

    def _repo_with_docs(self, docs_by_id):
        """A repository whose find_one returns docs keyed by _id."""
        repo = MembershipsRepository(cache_ttl_seconds=0.0)

        async def _fake_find_one(query):
            doc = docs_by_id.get(query["_id"])
            # Honor the enabled filter the repo applies.
            if doc and query.get("enabled") is True and not doc.get("enabled", True):
                return None
            return doc

        collection = AsyncMock()
        collection.find_one = _fake_find_one

        async def _get_collection():
            return collection

        repo._get_collection = _get_collection  # type: ignore[assignment]
        return repo

    @pytest.mark.asyncio
    async def test_resolves_user_groups(self):
        repo = self._repo_with_docs(
            {"user:alice": {"_id": "user:alice", "groups": ["devs"], "enabled": True}}
        )
        assert await repo.get_groups_for("alice", None) == ["devs"]

    @pytest.mark.asyncio
    async def test_unions_user_and_client_groups_deduped(self):
        repo = self._repo_with_docs(
            {
                "user:alice": {"_id": "user:alice", "groups": ["devs", "shared"], "enabled": True},
                "client:cli-1": {
                    "_id": "client:cli-1",
                    "groups": ["shared", "agents"],
                    "enabled": True,
                },
            }
        )
        groups = await repo.get_groups_for("alice", "cli-1")
        # union, de-duplicated, order preserved (user first)
        assert groups == ["devs", "shared", "agents"]

    @pytest.mark.asyncio
    async def test_no_membership_returns_empty(self):
        repo = self._repo_with_docs({})
        assert await repo.get_groups_for("nobody", "no-client") == []

    @pytest.mark.asyncio
    async def test_disabled_membership_yields_no_groups(self):
        repo = self._repo_with_docs(
            {"user:alice": {"_id": "user:alice", "groups": ["devs"], "enabled": False}}
        )
        assert await repo.get_groups_for("alice", None) == []
