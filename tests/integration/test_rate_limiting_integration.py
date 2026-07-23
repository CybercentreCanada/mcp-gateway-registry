"""Integration tests for rate limiting against a real MongoDB (issue #295).

Exercises the DocumentDB backend's atomic conditional increment (the
DuplicateKeyError-at-limit path that unit tests with a fake backend cannot
reach), cross-replica correctness (two limiter instances sharing one store
jointly enforce a single N, not 2N), per-window counter separation, and the
end-to-end deny-does-not-consume property through the real limiter.

Requires a running MongoDB (the harness points DOCUMENTDB_HOST at localhost);
skipped automatically if unreachable.
"""

import uuid

import pytest

from registry.rate_limiting.definitions_repository import DefinitionsRepository
from registry.rate_limiting.documentdb_backend import DocumentDBRateLimiterBackend
from registry.rate_limiting.limiter import RateLimiter
from registry.rate_limiting.models import RateLimitDefinition

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def backend():
    """A DocumentDB backend on a live collection; skips if MongoDB is unreachable."""
    b = DocumentDBRateLimiterBackend()
    try:
        col = await b._get_collection()
        await col.database.command("ping")
    except Exception as e:
        pytest.skip(f"MongoDB not reachable: {e}")
    yield b


async def _fresh_key() -> str:
    """A unique counter key so parallel test runs never collide."""
    return f"clr:group:it-{uuid.uuid4().hex}"


class TestConditionalIncrement:
    """The real atomic conditional-increment contract."""

    async def test_increments_until_limit_then_denies(self, backend):
        key = await _fresh_key()
        results = [await backend.incr_if_allowed(key, 60, 3) for _ in range(5)]
        allowed = [r for r in results if r.allowed]
        denied = [r for r in results if not r.allowed]
        assert len(allowed) == 3
        assert len(denied) == 2
        # Counts are monotonic 1..3 for the allowed ops.
        assert [r.current for r in allowed] == [1, 2, 3]
        # The stored count never exceeds max_requests, even after denied attempts.
        assert await backend.get(key, 60) == 3

    async def test_get_on_missing_key_is_zero(self, backend):
        assert await backend.get(await _fresh_key(), 60) == 0

    async def test_distinct_windows_are_separate_counters(self, backend):
        # Same subject, two window lengths -> two independent documents.
        subject = f"clr:group:it-{uuid.uuid4().hex}"
        await backend.incr_if_allowed(subject, 60, 100)
        await backend.incr_if_allowed(subject, 60, 100)
        await backend.incr_if_allowed(subject, 86400, 100)
        assert await backend.get(subject, 60) == 2
        assert await backend.get(subject, 86400) == 1


class _FixedGroupDefs:
    """Definitions stub returning fixed caller defs (isolates the DB test from the definitions collection).

    Filters by entity_type and name like the real repo.
    """

    def __init__(self, caller_defs):
        self._caller_defs = caller_defs

    async def list_caller_limits(self, entity_type, names):
        return [d for d in self._caller_defs if d.entity_type == entity_type and d.name in names]

    async def list_target_limits(self, entity_type, name):
        return []


class _FixedMemberships:
    """Memberships stub mapping a username to fixed rate-limit groups."""

    def __init__(self, by_user):
        self._by_user = by_user

    async def get_groups_for(self, username, client_id):
        return list(self._by_user.get(username, []))


class TestCrossReplicaCorrectness:
    """Two limiter instances sharing one store must jointly enforce a single N."""

    async def test_two_instances_share_one_limit(self, backend):
        username = f"it-{uuid.uuid4().hex}"
        caller_defs = [
            RateLimitDefinition(
                axis="caller",
                entity_type="group",
                name="devs",
                user_max_requests=10,
                window_seconds=60,
            )
        ]
        memberships = _FixedMemberships({username: ["devs"]})
        # Two limiters, same backend collection = two replicas.
        lim_a = RateLimiter(backend, _FixedGroupDefs(caller_defs), memberships, fail_open=True)
        lim_b = RateLimiter(backend, _FixedGroupDefs(caller_defs), memberships, fail_open=True)

        allowed = 0
        for i in range(20):
            limiter = lim_a if i % 2 else lim_b
            decision = await limiter.check(username=username, client_id=None)
            if decision.allowed:
                allowed += 1

        # Exactly N across BOTH instances, not 2N. This is the whole point of a shared store.
        assert allowed == 10


class TestDenyDoesNotConsumeThroughRealBackend:
    """The Blocker-1 property, verified end-to-end against the real store."""

    async def test_burst_denial_does_not_consume_daily_counter(self, backend):
        username = f"it-{uuid.uuid4().hex}"
        caller_defs = [
            RateLimitDefinition(
                axis="caller",
                entity_type="group",
                name="devs",
                user_max_requests=5,
                window_seconds=60,
            ),
            RateLimitDefinition(
                axis="caller",
                entity_type="group",
                name="devs",
                user_max_requests=20,
                window_seconds=86400,
            ),
        ]
        limiter = RateLimiter(
            backend,
            _FixedGroupDefs(caller_defs),
            _FixedMemberships({username: ["devs"]}),
            fail_open=True,
        )

        allowed = 0
        for _ in range(30):
            decision = await limiter.check(username=username, client_id=None)
            if decision.allowed:
                allowed += 1

        assert allowed == 5
        # The daily counter must read 5 (the allowed requests), NOT 30.
        daily_key = f"clr:group:{username}:86400"
        assert await backend.get(daily_key, 86400) == 5


class TestDefinitionsRepositoryRoundtrip:
    """CRUD against the real mcp_rate_limits collection."""

    @pytest.fixture
    async def repo(self):
        r = DefinitionsRepository(cache_ttl_seconds=0.0)  # disable cache so reads hit the DB
        try:
            col = await r._get_collection()
            await col.database.command("ping")
        except Exception as e:
            pytest.skip(f"MongoDB not reachable: {e}")
        created: list[str] = []
        r._created = created
        yield r
        col = await r._get_collection()
        if created:
            await col.delete_many({"_id": {"$in": created}})

    async def test_upsert_then_read_and_delete(self, repo):
        name = f"it-{uuid.uuid4().hex}"
        definition = RateLimitDefinition(
            axis="target", entity_type="a2a_agent", name=name, max_requests=7, window_seconds=60
        )
        repo._created.append(definition.build_id())

        await repo.upsert(definition)
        found = await repo.list_target_limits("a2a_agent", name)
        assert len(found) == 1
        assert found[0].max_requests == 7

        deleted = await repo.delete(definition.build_id())
        assert deleted is True
        assert await repo.list_target_limits("a2a_agent", name) == []
