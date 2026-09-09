"""DocumentDB/MongoDB fixed-window counter backend.

Correct across replicas via an atomic conditional increment: for an at-limit
counter the ``{"count": {"$lt": max_requests}}`` predicate fails, so
``find_one_and_update`` finds nothing to update and (because ``upsert=True``)
tries to INSERT a duplicate ``_id`` -- which the unique ``_id`` index rejects
with ``DuplicateKeyError``. Two replicas racing on the boundary both go through
the same atomic compare-and-increment, so the aggregate can never exceed
``max_requests`` and a denied request performs no increment.

Reuses the singleton client and namespaced-collection helpers, mirroring the
TTL pattern in ``backend_session_repository.py``.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..observability.meters import rate_limit_backend_duration_ms
from ..repositories.documentdb.client import (
    get_collection_name,
    get_documentdb_client,
)
from .backend import IncrResult, RateLimiterBackend

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

# Base name of the ephemeral counter collection (namespaced at runtime).
COUNTER_COLLECTION_BASE: str = "rate_limit_counters"

# TTL slack multiplier: keep an expired window's doc around for one extra window
# so the periodic TTL sweep (which runs ~once/minute) has margin. Correctness
# never depends on precise expiry because the window index is part of the _id.
TTL_SLACK_MULTIPLIER: int = 2


def _window_index(
    now_epoch: float,
    window_seconds: int,
) -> int:
    """Return the integer index of the fixed window containing ``now_epoch``."""
    return int(now_epoch) // window_seconds


def _record_backend_duration(
    started_at: float,
    op: str,
) -> None:
    """Record the counter-store round-trip latency for one backend op."""
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    rate_limit_backend_duration_ms.record(elapsed_ms, {"backend": "documentdb", "op": op})


class DocumentDBRateLimiterBackend(RateLimiterBackend):
    """Fixed-window counters in DocumentDB/MongoDB. Correct across replicas via atomic $inc."""

    def __init__(self) -> None:
        self._collection: AsyncIOMotorCollection | None = None
        self._collection_name = get_collection_name(COUNTER_COLLECTION_BASE)
        self._indexes_created = False

    async def _get_collection(self) -> AsyncIOMotorCollection:
        """Get the counter collection, creating the TTL index on first access."""
        if self._collection is None:
            db = await get_documentdb_client()
            self._collection = db[self._collection_name]
            await self._ensure_indexes()
        return self._collection

    async def _ensure_indexes(self) -> None:
        """Create the TTL index on ``expire_at`` if not present.

        Index creation must never break a request; a failure here is logged and
        retried on the next access.
        """
        if self._indexes_created or self._collection is None:
            return
        try:
            await self._collection.create_index(
                [("expire_at", ASCENDING)],
                expireAfterSeconds=0,
                name="ttl_expire_at",
            )
            self._indexes_created = True
            logger.info(f"Created TTL index for {self._collection_name} collection")
        except Exception as exc:
            logger.warning(
                f"rate-limit TTL index creation failed for {self._collection_name}: {exc}"
            )

    async def incr_if_allowed(
        self,
        key: str,
        window_seconds: int,
        max_requests: int,
    ) -> IncrResult:
        """Conditionally increment the fixed-window counter for ``key``. See interface docstring."""
        collection = await self._get_collection()
        now = datetime.now(UTC)
        window_index = _window_index(now.timestamp(), window_seconds)
        window_start = datetime.fromtimestamp(window_index * window_seconds, UTC)
        expire_at = window_start + timedelta(seconds=window_seconds * TTL_SLACK_MULTIPLIER)
        doc_id = f"{key}:{window_index}"

        started_at = time.perf_counter()
        try:
            result = await collection.find_one_and_update(
                {"_id": doc_id, "count": {"$lt": max_requests}},
                {
                    "$inc": {"count": 1},
                    "$setOnInsert": {"window_start": window_start, "expire_at": expire_at},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return IncrResult(allowed=True, current=int(result["count"]))
        except DuplicateKeyError:
            # The at-limit doc already exists; the conditional filter did not match,
            # so upsert tried to insert a duplicate _id. Already at limit, no increment.
            return IncrResult(allowed=False, current=max_requests)
        finally:
            _record_backend_duration(started_at, "incr")

    async def get(
        self,
        key: str,
        window_seconds: int,
    ) -> int:
        """Return the current count for ``key`` in its current window without incrementing."""
        collection = await self._get_collection()
        now = datetime.now(UTC)
        window_index = _window_index(now.timestamp(), window_seconds)
        doc_id = f"{key}:{window_index}"

        started_at = time.perf_counter()
        try:
            doc = await collection.find_one({"_id": doc_id})
            return int(doc["count"]) if doc else 0
        finally:
            _record_backend_duration(started_at, "get")
