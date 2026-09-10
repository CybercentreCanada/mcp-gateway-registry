"""Unit tests for single-use enforcement on internal service tokens.

Internal service JWTs (``registry/auth/internal.py``) are short-lived HS256
tokens minted right before a single service-to-service call. Before this change
they carried only ``iat``/``exp``, so a network-adjacent attacker who captured
one could replay it any number of times within the TTL window on the internal
cluster network.

The fix adds a unique ``jti`` claim and a shared consumed-jti store: the first
validation records the ``jti``; a replay of the same token is rejected. These
tests pin:

1. A minted token carries a unique ``jti``.
2. A token with a fresh ``jti`` is accepted exactly once.
3. The SAME token replayed is rejected (jti already consumed).
4. A token with no ``jti`` is rejected (fail closed).
5. A store failure rejects the token (fail closed).
6. Signature/expiry validation still runs before the single-use check.
7. ``_get_collection`` creates the unique ``jti`` index (the atomicity
   primitive) and the TTL index — a regression that drops ``unique=True``
   would silently disable replay protection.
8. ``consume_jti`` retains the record for ``ttl + margin`` (never under-retains).
9. The ``exp``/``iat`` fallback yields the nominal 60s TTL when the claims are
   unusable.
10. A token consumed at one ``/internal/*`` route is rejected at a *different*
    one (the actual cross-endpoint replay an attacker would attempt).
"""

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError
from starlette.requests import Request

from registry.auth import internal_replay_store
from registry.auth.internal import (
    _INTERNAL_JWT_AUDIENCE,
    _INTERNAL_JWT_ISSUER,
    _INTERNAL_JWT_TTL_SECONDS,
    _INTERNAL_TOKEN_KIND,
    _derive_internal_signing_key,
    _enforce_single_use,
    generate_internal_token,
    validate_internal_auth,
)
from registry.auth.internal_replay_store import (
    _JTI_RETENTION_MARGIN_SECONDS,
    consume_jti,
)

_SECRET_KEY: str = "x" * 40  # >= 32 bytes so the config-level guard is satisfied


def _make_request(token: str | None, path: str = "/internal/x") -> Request:
    """Build a minimal ASGI Request carrying an optional Bearer token.

    ``path`` lets a test aim the same token at different ``/internal/*`` routes
    to exercise cross-endpoint replay (single-use is not endpoint-scoped, so a
    token consumed anywhere is dead everywhere).
    """
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
    return Request(scope)


class _FakeConsumedStore:
    """In-memory stand-in for the shared consumed-jti collection.

    Mimics the atomic unique-index behaviour: the first insert of a jti
    succeeds; a second raises DuplicateKeyError (what a real unique index does).
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.insert_attempts: int = 0

    async def insert_one(self, doc: dict) -> None:
        self.insert_attempts += 1
        jti = doc["jti"]
        if jti in self._seen:
            raise DuplicateKeyError("duplicate jti")
        self._seen.add(jti)


class TestGeneratedTokenCarriesJti:
    """A minted internal token must carry a unique jti claim."""

    def test_token_has_jti(self) -> None:
        with patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}):
            token = generate_internal_token(subject="registry-service", purpose="test")
            claims = pyjwt.decode(
                token,
                _derive_internal_signing_key(_SECRET_KEY),
                algorithms=["HS256"],
                audience=_INTERNAL_JWT_AUDIENCE,
                issuer=_INTERNAL_JWT_ISSUER,
            )
        assert claims.get("jti")
        assert claims["token_kind"] == _INTERNAL_TOKEN_KIND

    def test_two_tokens_have_distinct_jti(self) -> None:
        with patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}):
            t1 = generate_internal_token(subject="s", purpose="p")
            t2 = generate_internal_token(subject="s", purpose="p")
            key = _derive_internal_signing_key(_SECRET_KEY)
            j1 = pyjwt.decode(
                t1,
                key,
                algorithms=["HS256"],
                audience=_INTERNAL_JWT_AUDIENCE,
                issuer=_INTERNAL_JWT_ISSUER,
            )["jti"]
            j2 = pyjwt.decode(
                t2,
                key,
                algorithms=["HS256"],
                audience=_INTERNAL_JWT_AUDIENCE,
                issuer=_INTERNAL_JWT_ISSUER,
            )["jti"]
        assert j1 != j2


class TestConsumeJti:
    """Direct tests of the shared consumed-jti store."""

    def teardown_method(self) -> None:
        internal_replay_store._reset_state_for_tests()

    @pytest.mark.asyncio
    async def test_first_use_accepted_replay_rejected(self) -> None:
        fake = _FakeConsumedStore()
        with patch.object(internal_replay_store, "_get_collection", AsyncMock(return_value=fake)):
            assert await consume_jti("abc123", 60) is True
            # Same jti a second time -> DuplicateKeyError -> reject.
            assert await consume_jti("abc123", 60) is False
            # A different jti is still accepted.
            assert await consume_jti("def456", 60) is True

    @pytest.mark.asyncio
    async def test_empty_jti_rejected(self) -> None:
        # No store access should even be attempted for an empty jti.
        assert await consume_jti("", 60) is False

    @pytest.mark.asyncio
    async def test_store_unreachable_rejected(self) -> None:
        with patch.object(
            internal_replay_store,
            "_get_collection",
            AsyncMock(side_effect=RuntimeError("mongo down")),
        ):
            assert await consume_jti("abc123", 60) is False


class TestReplayCheckMetric:
    """consume_jti emits an outcome-labelled counter so operators can tell a
    replay attack (result=replay) apart from a store outage (result=store_error).

    The label is the whole point of the metric — a regression that drops it or
    mislabels store_error as replay would defeat the SRE signal, so each branch
    is pinned explicitly.
    """

    def teardown_method(self) -> None:
        internal_replay_store._reset_state_for_tests()

    @pytest.mark.asyncio
    async def test_accepted_and_replay_emit_correct_labels(self) -> None:
        fake = _FakeConsumedStore()
        metric = MagicMock()
        with (
            patch.object(internal_replay_store, "_get_collection", AsyncMock(return_value=fake)),
            patch.object(internal_replay_store, "internal_token_replay_check_total", metric),
        ):
            await consume_jti("abc123", 60)  # first use -> accepted
            await consume_jti("abc123", 60)  # same jti -> replay

        labels_used = [call.kwargs["result"] for call in metric.labels.call_args_list]
        assert labels_used == ["accepted", "replay"]
        # One inc() per outcome.
        assert metric.labels.return_value.inc.call_count == 2

    @pytest.mark.asyncio
    async def test_missing_jti_emits_missing_jti_label(self) -> None:
        metric = MagicMock()
        with patch.object(internal_replay_store, "internal_token_replay_check_total", metric):
            assert await consume_jti("", 60) is False
        metric.labels.assert_called_once_with(result="missing_jti")
        metric.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_error_emits_store_error_label(self) -> None:
        metric = MagicMock()
        with (
            patch.object(
                internal_replay_store,
                "_get_collection",
                AsyncMock(side_effect=RuntimeError("mongo down")),
            ),
            patch.object(internal_replay_store, "internal_token_replay_check_total", metric),
        ):
            assert await consume_jti("abc123", 60) is False
        # store_error is the fail-closed signal, distinct from a replay reject.
        metric.labels.assert_called_once_with(result="store_error")
        metric.labels.return_value.inc.assert_called_once()


class TestValidateInternalAuthSingleUse:
    """End-to-end single-use enforcement through validate_internal_auth."""

    def teardown_method(self) -> None:
        internal_replay_store._reset_state_for_tests()

    @pytest.mark.asyncio
    async def test_fresh_token_accepted_once_then_replay_rejected(self) -> None:
        fake = _FakeConsumedStore()
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}),
            patch.object(internal_replay_store, "_get_collection", AsyncMock(return_value=fake)),
        ):
            token = generate_internal_token(subject="registry-service", purpose="test")

            # First presentation: accepted, returns the attributable caller
            # identity (`<service>@<instance_id>`; the instance suffix is
            # host-derived, so assert on the service component).
            caller = await validate_internal_auth(_make_request(token))
            assert caller.split("@", 1)[0] == "registry-service"

            # Replay of the exact same token: rejected 401.
            with pytest.raises(HTTPException) as exc_info:
                await validate_internal_auth(_make_request(token))
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Invalid token"

    @pytest.mark.asyncio
    async def test_token_without_jti_rejected(self) -> None:
        """A validly-signed internal token that omits jti is rejected (fail closed)."""
        fake = _FakeConsumedStore()
        now = int(time.time())
        claims = {
            "iss": _INTERNAL_JWT_ISSUER,
            "aud": _INTERNAL_JWT_AUDIENCE,
            "sub": "registry-service",
            "token_kind": _INTERNAL_TOKEN_KIND,
            "token_use": "access",
            "iat": now,
            "exp": now + 60,
            # no jti
        }
        token = pyjwt.encode(claims, _derive_internal_signing_key(_SECRET_KEY), algorithm="HS256")
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}),
            patch.object(internal_replay_store, "_get_collection", AsyncMock(return_value=fake)),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await validate_internal_auth(_make_request(token))
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid token"

    @pytest.mark.asyncio
    async def test_expired_token_rejected_before_jti_check(self) -> None:
        """Expiry still enforced; store is never consulted for an expired token."""
        now = int(time.time())
        claims = {
            "iss": _INTERNAL_JWT_ISSUER,
            "aud": _INTERNAL_JWT_AUDIENCE,
            "sub": "registry-service",
            "token_kind": _INTERNAL_TOKEN_KIND,
            "token_use": "access",
            "jti": "expired-jti",
            "iat": now - 300,
            "exp": now - 120,
        }
        token = pyjwt.encode(claims, _derive_internal_signing_key(_SECRET_KEY), algorithm="HS256")
        get_collection = AsyncMock()
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}),
            patch.object(internal_replay_store, "_get_collection", get_collection),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await validate_internal_auth(_make_request(token))
        assert exc_info.value.status_code == 401
        get_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_failure_rejects_valid_token(self) -> None:
        """A signature-valid, fresh token is denied when the store is unreachable."""
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}),
            patch.object(
                internal_replay_store,
                "_get_collection",
                AsyncMock(side_effect=RuntimeError("mongo down")),
            ),
        ):
            token = generate_internal_token(subject="registry-service", purpose="test")
            with pytest.raises(HTTPException) as exc_info:
                await validate_internal_auth(_make_request(token))
        assert exc_info.value.status_code == 401


class TestIndexCreation:
    """The unique jti index is the atomicity primitive; assert it is created.

    ``_get_collection`` builds the indexes lazily on first access. If a
    regression drops ``unique=True`` (or the index entirely), replay protection
    silently degrades to no-op while every mocked-store test still passes — so
    pin the exact index spec here against the real (unmocked) ``_get_collection``.
    """

    def teardown_method(self) -> None:
        internal_replay_store._reset_state_for_tests()

    @pytest.mark.asyncio
    async def test_unique_jti_and_ttl_indexes_created(self) -> None:
        collection = MagicMock()
        collection.create_index = AsyncMock()
        fake_db = {"internal_token_jti_default": collection}

        with (
            patch(
                "registry.repositories.documentdb.client.get_documentdb_client",
                AsyncMock(return_value=fake_db),
            ),
            patch(
                "registry.repositories.documentdb.client.get_collection_name",
                return_value="internal_token_jti_default",
            ),
        ):
            result = await internal_replay_store._get_collection()

        assert result is collection

        calls = {call.kwargs.get("name"): call for call in collection.create_index.call_args_list}
        assert set(calls) == {"ux_jti", "ttl_expires_at"}

        # Unique index on jti -> the DuplicateKeyError-on-replay guarantee.
        jti_call = calls["ux_jti"]
        assert jti_call.args[0] == [("jti", ASCENDING)]
        assert jti_call.kwargs["unique"] is True

        # TTL index reaps consumed entries at their absolute expires_at.
        ttl_call = calls["ttl_expires_at"]
        assert ttl_call.args[0] == [("expires_at", ASCENDING)]
        assert ttl_call.kwargs["expireAfterSeconds"] == 0

    @pytest.mark.asyncio
    async def test_collection_and_indexes_cached_after_first_access(self) -> None:
        collection = MagicMock()
        collection.create_index = AsyncMock()
        fake_db = {"internal_token_jti_default": collection}
        get_client = AsyncMock(return_value=fake_db)

        with (
            patch(
                "registry.repositories.documentdb.client.get_documentdb_client",
                get_client,
            ),
            patch(
                "registry.repositories.documentdb.client.get_collection_name",
                return_value="internal_token_jti_default",
            ),
        ):
            await internal_replay_store._get_collection()
            await internal_replay_store._get_collection()

        # Second call returns the cached collection: no re-connect, no re-index.
        get_client.assert_awaited_once()
        assert collection.create_index.await_count == 2  # two indexes, created once


class TestRetentionMargin:
    """consume_jti must retain the record for at least the token's full TTL."""

    def teardown_method(self) -> None:
        internal_replay_store._reset_state_for_tests()

    @pytest.mark.asyncio
    async def test_expires_at_is_ttl_plus_margin(self) -> None:
        collection = MagicMock()
        collection.insert_one = AsyncMock()

        with patch.object(
            internal_replay_store, "_get_collection", AsyncMock(return_value=collection)
        ):
            assert await consume_jti("some-jti", 60) is True

        doc = collection.insert_one.await_args.args[0]
        retained = (doc["expires_at"] - doc["consumed_at"]).total_seconds()
        # Never under-retain: at least ttl + margin so the TTL index cannot reap
        # a document while the token it guards could still be presented.
        assert retained == pytest.approx(60 + _JTI_RETENTION_MARGIN_SECONDS)


class TestExpIatFallback:
    """When exp/iat are unusable, single-use falls back to the nominal TTL."""

    def teardown_method(self) -> None:
        internal_replay_store._reset_state_for_tests()

    @pytest.mark.asyncio
    async def test_missing_exp_iat_uses_nominal_ttl(self) -> None:
        captured: dict = {}

        async def _fake_consume(jti: str | None, ttl_seconds: int) -> bool:
            captured["jti"] = jti
            captured["ttl"] = ttl_seconds
            return True

        # internal.py imports consume_jti at module scope, so patch it there.
        with patch("registry.auth.internal.consume_jti", _fake_consume):
            await _enforce_single_use({"jti": "no-exp-iat"})

        assert captured["ttl"] == _INTERNAL_JWT_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_exp_not_after_iat_uses_nominal_ttl(self) -> None:
        captured: dict = {}

        async def _fake_consume(jti: str | None, ttl_seconds: int) -> bool:
            captured["ttl"] = ttl_seconds
            return True

        with patch("registry.auth.internal.consume_jti", _fake_consume):
            # exp < iat is nonsensical -> ignore it, use the nominal TTL.
            await _enforce_single_use({"jti": "bad", "iat": 1000, "exp": 900})

        assert captured["ttl"] == _INTERNAL_JWT_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_exp_equal_iat_uses_nominal_ttl(self) -> None:
        """exp == iat is the exact boundary the ``exp > iat`` guard defends.

        A regression to ``exp >= iat`` would let this through as ttl == 0 (a
        zero-second retention that reopens the replay window); the guard must
        fall back to the nominal TTL instead.
        """
        captured: dict = {}

        async def _fake_consume(jti: str | None, ttl_seconds: int) -> bool:
            captured["ttl"] = ttl_seconds
            return True

        with patch("registry.auth.internal.consume_jti", _fake_consume):
            await _enforce_single_use({"jti": "eq", "iat": 1000, "exp": 1000})

        assert captured["ttl"] == _INTERNAL_JWT_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_valid_exp_iat_uses_claim_ttl(self) -> None:
        captured: dict = {}

        async def _fake_consume(jti: str | None, ttl_seconds: int) -> bool:
            captured["ttl"] = ttl_seconds
            return True

        with patch("registry.auth.internal.consume_jti", _fake_consume):
            await _enforce_single_use({"jti": "ok", "iat": 1000, "exp": 1045})

        assert captured["ttl"] == 45


class TestCrossEndpointReplay:
    """Single-use is cluster-wide, not endpoint-scoped: a token consumed at one
    /internal/* route is dead at every other one (the real attacker scenario)."""

    def teardown_method(self) -> None:
        internal_replay_store._reset_state_for_tests()

    @pytest.mark.asyncio
    async def test_token_consumed_at_one_route_rejected_at_another(self) -> None:
        fake = _FakeConsumedStore()
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}),
            patch.object(internal_replay_store, "_get_collection", AsyncMock(return_value=fake)),
        ):
            token = generate_internal_token(subject="registry-service", purpose="reload-scopes")

            # First hop: /internal/reload-scopes accepts and consumes the jti.
            # Caller is the attributable `<service>@<instance_id>`; the instance
            # suffix is host-derived, so assert on the service component.
            caller = await validate_internal_auth(
                _make_request(token, path="/internal/reload-scopes")
            )
            assert caller.split("@", 1)[0] == "registry-service"

            # Attacker replays the SAME token against a different internal route.
            with pytest.raises(HTTPException) as exc_info:
                await validate_internal_auth(_make_request(token, path="/internal/tokens"))

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid token"
        # Prove the rejection came from the single-use store (the replay path),
        # not an earlier signature/kind check: the store was hit on BOTH
        # presentations. "Invalid token" is also emitted by signature failures,
        # so the second insert attempt is what disambiguates a replay reject
        # from a validation reject.
        assert fake.insert_attempts == 2
