"""Unit tests for targeted ui_permissions merge/remove on the scope repository.

These back the per-type custom-entity scope mint (merge_ui_permissions) and
cleanup (remove_ui_permission_keys / *_from_all_groups). They assert the exact
Mongo update shape ($set / $unset per key) and the matched-count -> bool
contract, without a live DB.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.repositories.documentdb.scope_repository import DocumentDBScopeRepository


def _make_repo():
    repo = DocumentDBScopeRepository()
    collection = MagicMock()
    repo._get_collection = AsyncMock(return_value=collection)
    repo._scopes_cache = {}
    return repo, collection


@pytest.mark.unit
class TestMergeUiPermissions:
    @pytest.mark.asyncio
    async def test_merge_sets_each_key(self):
        repo, collection = _make_repo()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        scopes = {"create_dataset_entity": ["all"], "list_dataset_entity": ["all"]}
        result = await repo.merge_ui_permissions("mcp-registry-admin", scopes)

        assert result is True
        _, update = collection.update_one.call_args[0]
        set_fields = update["$set"]
        assert set_fields["ui_permissions.create_dataset_entity"] == ["all"]
        assert set_fields["ui_permissions.list_dataset_entity"] == ["all"]
        assert "updated_at" in set_fields
        # Cache reflects the merge.
        assert repo._scopes_cache["UI-Scopes"]["mcp-registry-admin"]["create_dataset_entity"] == [
            "all"
        ]

    @pytest.mark.asyncio
    async def test_merge_group_missing_returns_false(self):
        repo, collection = _make_repo()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
        result = await repo.merge_ui_permissions("nope", {"create_x_entity": ["all"]})
        assert result is False

    @pytest.mark.asyncio
    async def test_merge_empty_is_noop(self):
        repo, collection = _make_repo()
        collection.update_one = AsyncMock()
        assert await repo.merge_ui_permissions("g", {}) is False
        collection.update_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_merge_db_error_returns_false(self):
        repo, collection = _make_repo()
        collection.update_one = AsyncMock(side_effect=RuntimeError("boom"))
        assert await repo.merge_ui_permissions("g", {"create_x_entity": ["all"]}) is False


@pytest.mark.unit
class TestRemoveUiPermissionKeys:
    @pytest.mark.asyncio
    async def test_unset_each_key(self):
        repo, collection = _make_repo()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        repo._scopes_cache = {"UI-Scopes": {"g": {"create_x_entity": ["all"], "keep": ["all"]}}}

        result = await repo.remove_ui_permission_keys("g", ["create_x_entity"])

        assert result is True
        _, update = collection.update_one.call_args[0]
        assert "ui_permissions.create_x_entity" in update["$unset"]
        # Cache pruned but unrelated keys preserved.
        assert "create_x_entity" not in repo._scopes_cache["UI-Scopes"]["g"]
        assert "keep" in repo._scopes_cache["UI-Scopes"]["g"]

    @pytest.mark.asyncio
    async def test_group_missing_returns_false(self):
        repo, collection = _make_repo()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
        assert await repo.remove_ui_permission_keys("g", ["create_x_entity"]) is False


@pytest.mark.unit
class TestRemoveFromAllGroups:
    @pytest.mark.asyncio
    async def test_sweep_returns_modified_count(self):
        repo, collection = _make_repo()
        collection.update_many = AsyncMock(return_value=MagicMock(modified_count=3))

        modified = await repo.remove_ui_permission_keys_from_all_groups(
            ["create_x_entity", "list_x_entity"]
        )

        assert modified == 3
        match_filter, update = collection.update_many.call_args[0]
        # Only groups holding at least one key are touched.
        assert "$or" in match_filter
        assert "ui_permissions.create_x_entity" in update["$unset"]

    @pytest.mark.asyncio
    async def test_empty_keys_noop(self):
        repo, collection = _make_repo()
        collection.update_many = AsyncMock()
        assert await repo.remove_ui_permission_keys_from_all_groups([]) == 0
        collection.update_many.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_error_returns_zero(self):
        repo, collection = _make_repo()
        collection.update_many = AsyncMock(side_effect=RuntimeError("boom"))
        assert await repo.remove_ui_permission_keys_from_all_groups(["create_x_entity"]) == 0
