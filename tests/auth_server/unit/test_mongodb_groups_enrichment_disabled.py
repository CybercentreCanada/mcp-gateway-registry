"""Tests that the auth-server group enrichment honors the disabled flag.

A disabled user-group mapping or a disabled M2M client must not contribute
groups (and therefore scopes) via the enrichment fallback. These tests fail
against the pre-fix code, which returned groups from any matching record
regardless of its ``enabled`` field.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import mongodb_groups_enrichment as enrichment
import pytest


def _make_collection(docs: list[dict[str, Any]]):
    """Build a fake Motor collection that honors an ``enabled: {$ne: False}`` filter.

    Args:
        docs: The documents stored in the fake collection.

    Returns:
        An object with an async ``find_one`` that filters like MongoDB would for
        the queries used by the enrichment functions (equality on the id field
        plus the ``$ne: False`` operator on ``enabled``).
    """

    async def _find_one(query: dict[str, Any]) -> dict[str, Any] | None:
        enabled_filter = query.get("enabled")
        for doc in docs:
            # Match the id field (username or client_id) present in the query.
            id_match = all(
                doc.get(key) == value for key, value in query.items() if key != "enabled"
            )
            if not id_match:
                continue
            if isinstance(enabled_filter, dict) and "$ne" in enabled_filter:
                if doc.get("enabled", True) == enabled_filter["$ne"]:
                    continue
            return doc
        return None

    collection = AsyncMock()
    collection.find_one = _find_one
    return collection


def _patch_db(docs: list[dict[str, Any]]):
    """Patch ``_get_mongodb`` so enrichment reads from a fake collection.

    The fake DB is a dict-like object whose ``__getitem__`` returns the same
    fake collection for whichever collection name the code requests.
    """
    collection = _make_collection(docs)

    class _FakeDB:
        def __getitem__(self, _name: str):
            return collection

    return patch.object(enrichment, "_get_mongodb", AsyncMock(return_value=_FakeDB()))


class TestUserGroupEnrichmentDisabled:
    """Disabled user-group mappings must not contribute groups."""

    @pytest.mark.asyncio
    async def test_enabled_user_group_contributes_groups(self):
        docs = [{"username": "alice", "groups": ["developers"], "enabled": True}]
        with _patch_db(docs):
            result = await enrichment.enrich_user_groups_from_mongodb("alice", [], "pingfederate")
        assert result == ["developers"]

    @pytest.mark.asyncio
    async def test_disabled_user_group_contributes_no_groups(self):
        docs = [{"username": "alice", "groups": ["developers"], "enabled": False}]
        with _patch_db(docs):
            result = await enrichment.enrich_user_groups_from_mongodb("alice", [], "pingfederate")
        # Disabled record must be ignored; caller keeps its (empty) groups.
        assert result == []

    @pytest.mark.asyncio
    async def test_missing_enabled_field_treated_as_active(self):
        # Backward compatibility: records predating the flag still work.
        docs = [{"username": "alice", "groups": ["developers"]}]
        with _patch_db(docs):
            result = await enrichment.enrich_user_groups_from_mongodb("alice", [], "pingfederate")
        assert result == ["developers"]

    @pytest.mark.asyncio
    async def test_defense_in_depth_ignores_disabled_doc_if_query_bypassed(self):
        # Simulate a collection whose find_one returns a disabled doc even
        # though the query asked to exclude it (e.g. a future regression that
        # loosens the query). The in-code check must still drop it.
        disabled_doc = {"username": "alice", "groups": ["admins"], "enabled": False}
        collection = AsyncMock()
        collection.find_one = AsyncMock(return_value=disabled_doc)

        class _FakeDB:
            def __getitem__(self, _name: str):
                return collection

        with patch.object(enrichment, "_get_mongodb", AsyncMock(return_value=_FakeDB())):
            result = await enrichment.enrich_user_groups_from_mongodb("alice", [], "pingfederate")
        assert result == []


class TestM2MClientEnrichmentDisabled:
    """Disabled M2M clients must not contribute groups."""

    @pytest.mark.asyncio
    async def test_enabled_m2m_client_contributes_groups(self):
        docs = [{"client_id": "svc", "groups": ["registry-admins"], "enabled": True}]
        with _patch_db(docs):
            result = await enrichment.enrich_groups_from_mongodb("svc", [])
        assert result == ["registry-admins"]

    @pytest.mark.asyncio
    async def test_disabled_m2m_client_contributes_no_groups(self):
        docs = [{"client_id": "svc", "groups": ["registry-admins"], "enabled": False}]
        with _patch_db(docs):
            result = await enrichment.enrich_groups_from_mongodb("svc", [])
        assert result == []

    @pytest.mark.asyncio
    async def test_missing_enabled_field_treated_as_active(self):
        docs = [{"client_id": "svc", "groups": ["registry-admins"]}]
        with _patch_db(docs):
            result = await enrichment.enrich_groups_from_mongodb("svc", [])
        assert result == ["registry-admins"]

    @pytest.mark.asyncio
    async def test_defense_in_depth_ignores_disabled_doc_if_query_bypassed(self):
        disabled_doc = {"client_id": "svc", "groups": ["admins"], "enabled": False}
        collection = AsyncMock()
        collection.find_one = AsyncMock(return_value=disabled_doc)

        class _FakeDB:
            def __getitem__(self, _name: str):
                return collection

        with patch.object(enrichment, "_get_mongodb", AsyncMock(return_value=_FakeDB())):
            result = await enrichment.enrich_groups_from_mongodb("svc", [])
        assert result == []


class TestNonBooleanEnabledFailsClosed:
    """A record whose ``enabled`` is present but not the boolean True must not
    grant groups, even if it slips past the MongoDB query filter (BSON is not
    type-enforced). This proves the in-code re-check is fail-closed."""

    @pytest.mark.parametrize("bad_value", [None, 0, "", "false", "true", 1, "True"])
    def test_helper_denies_non_true_values(self, bad_value):
        assert enrichment._is_record_enabled({"enabled": bad_value}) is False

    @pytest.mark.parametrize("bad_value", [None, 0, "false"])
    @pytest.mark.asyncio
    async def test_user_group_non_boolean_enabled_contributes_no_groups(self, bad_value):
        # find_one returns the doc directly (query filter bypassed) so only the
        # in-code re-check stands between a non-boolean enabled and a grant.
        doc = {"username": "alice", "groups": ["admins"], "enabled": bad_value}
        collection = AsyncMock()
        collection.find_one = AsyncMock(return_value=doc)

        class _FakeDB:
            def __getitem__(self, _name: str):
                return collection

        with patch.object(enrichment, "_get_mongodb", AsyncMock(return_value=_FakeDB())):
            result = await enrichment.enrich_user_groups_from_mongodb("alice", [], "pingfederate")
        assert result == []

    @pytest.mark.parametrize("bad_value", [None, 0, "false"])
    @pytest.mark.asyncio
    async def test_m2m_non_boolean_enabled_contributes_no_groups(self, bad_value):
        doc = {"client_id": "svc", "groups": ["registry-admins"], "enabled": bad_value}
        collection = AsyncMock()
        collection.find_one = AsyncMock(return_value=doc)

        class _FakeDB:
            def __getitem__(self, _name: str):
                return collection

        with patch.object(enrichment, "_get_mongodb", AsyncMock(return_value=_FakeDB())):
            result = await enrichment.enrich_groups_from_mongodb("svc", [])
        assert result == []


class TestEnrichmentDbErrorFailsClosed:
    """A database error during enrichment must not grant groups: the functions
    return the caller's original (empty) groups, which map to empty scopes."""

    @pytest.mark.asyncio
    async def test_user_group_db_error_returns_empty_groups(self):
        with patch.object(
            enrichment, "_get_mongodb", AsyncMock(side_effect=RuntimeError("db down"))
        ):
            result = await enrichment.enrich_user_groups_from_mongodb("alice", [], "pingfederate")
        assert result == []

    @pytest.mark.asyncio
    async def test_m2m_db_error_returns_empty_groups(self):
        with patch.object(
            enrichment, "_get_mongodb", AsyncMock(side_effect=RuntimeError("db down"))
        ):
            result = await enrichment.enrich_groups_from_mongodb("svc", [])
        assert result == []


class TestIsRecordEnabledHelper:
    """Unit coverage for the shared enabled-check helper."""

    def test_explicit_true(self):
        assert enrichment._is_record_enabled({"enabled": True}) is True

    def test_explicit_false(self):
        assert enrichment._is_record_enabled({"enabled": False}) is False

    def test_missing_field_is_active(self):
        assert enrichment._is_record_enabled({}) is True
