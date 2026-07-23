"""Unit tests for the definitions repository read cache and parsing.

These patch the collection accessor so no real DocumentDB is needed; the focus is
the cache behavior (hit / miss / invalidation) and malformed-doc resilience.
"""

from registry.rate_limiting.definitions_repository import DefinitionsRepository


class _FakeCursor:
    """Async-iterable cursor over a fixed list of docs."""

    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for doc in self._docs:
            yield dict(doc)


class _FakeCollection:
    """Records find() calls and returns queued doc sets, to prove caching."""

    def __init__(self, docs):
        self._docs = docs
        self.find_calls = 0

    def find(self, query):
        self.find_calls += 1
        return _FakeCursor(self._docs)


def _make_repo_with_docs(docs, ttl=30.0):
    """Build a repository whose collection is a fake with the given docs."""
    repo = DefinitionsRepository(cache_ttl_seconds=ttl)
    collection = _FakeCollection(docs)

    async def _fake_get_collection():
        return collection

    repo._get_collection = _fake_get_collection  # type: ignore[assignment]
    return repo, collection


class TestDefinitionsCache:
    """Tests for the in-process read cache."""

    async def test_caller_limits_parsed(self):
        """A caller-limit doc is parsed into a model."""
        docs = [
            {
                "_id": "caller:group:dev:60",
                "axis": "caller",
                "entity_type": "group",
                "name": "dev",
                "user_max_requests": 5,
                "window_seconds": 60,
                "enabled": True,
            }
        ]
        repo, _ = _make_repo_with_docs(docs)
        result = await repo.list_caller_limits("group", ["dev"])
        assert len(result) == 1
        assert result[0].user_max_requests == 5

    async def test_second_read_is_cached(self):
        """A repeated identical query does not hit the collection again."""
        docs = [
            {
                "_id": "caller:group:dev:60",
                "axis": "caller",
                "entity_type": "group",
                "name": "dev",
                "user_max_requests": 5,
                "window_seconds": 60,
                "enabled": True,
            }
        ]
        repo, collection = _make_repo_with_docs(docs)
        await repo.list_caller_limits("group", ["dev"])
        await repo.list_caller_limits("group", ["dev"])
        assert collection.find_calls == 1

    async def test_empty_names_short_circuits(self):
        """No group names => no query, no limits."""
        repo, collection = _make_repo_with_docs([])
        result = await repo.list_caller_limits("group", [])
        assert result == []
        assert collection.find_calls == 0

    async def test_invalidate_cache_forces_reread(self):
        """After invalidation the next read hits the collection again."""
        docs = [
            {
                "_id": "target:mcp_server:mcpgw:60",
                "axis": "target",
                "entity_type": "mcp_server",
                "name": "mcpgw",
                "max_requests": 500,
                "window_seconds": 60,
                "enabled": True,
            }
        ]
        repo, collection = _make_repo_with_docs(docs)
        await repo.list_target_limits("mcp_server", "mcpgw")
        repo.invalidate_cache()
        await repo.list_target_limits("mcp_server", "mcpgw")
        assert collection.find_calls == 2

    async def test_malformed_doc_is_skipped(self):
        """A malformed definition must not break the read (auth path resilience)."""
        docs = [
            {
                "_id": "caller:group:dev:60",
                "axis": "caller",
                "entity_type": "group",
                "name": "dev",
                "user_max_requests": 5,
                "window_seconds": 60,
                "enabled": True,
            },
            {
                "_id": "caller:group:bad:60",
                "axis": "caller",
                "entity_type": "group",
                "name": "bad",
                "user_max_requests": -1,  # invalid: ge=1
                "window_seconds": 60,
                "enabled": True,
            },
        ]
        repo, _ = _make_repo_with_docs(docs)
        result = await repo.list_caller_limits("group", ["dev", "bad"])
        assert len(result) == 1
        assert result[0].name == "dev"
