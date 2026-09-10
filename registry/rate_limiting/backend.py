"""Abstract counter backend for rate limiting.

The backend holds the shared, cross-replica counter state. The v1 implementation
is DocumentDB (see ``documentdb_backend.py``); a Redis implementation can be
added later behind this same interface for very high-throughput deployments.
"""

import logging
from abc import ABC, abstractmethod
from typing import NamedTuple

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)


class IncrResult(NamedTuple):
    """Outcome of a conditional increment.

    ``allowed`` False means the counter was already at the limit and was NOT
    incremented (the deny-does-not-consume rule). ``current`` is the counter
    value after the operation (the limit value itself when not allowed).
    """

    allowed: bool
    current: int


class RateLimiterBackend(ABC):
    """Shared-state counter backend. Implementations MUST be correct across processes."""

    @abstractmethod
    async def incr_if_allowed(
        self,
        key: str,
        window_seconds: int,
        max_requests: int,
    ) -> IncrResult:
        """Atomically increment the counter for ``key`` in its current fixed window, but only
        if it is currently below ``max_requests``.

        - Below the limit: increment and return ``IncrResult(True, new_count)``.
        - Already at/above the limit: do NOT increment and return
          ``IncrResult(False, max_requests)``.

        This is the deny-does-not-consume rule: a rejected request must never advance any
        counter, so a tight burst limit can never burn down a wider volume/day cap.
        Implementations create the counter on first use and expire it after the window
        (plus slack), and MUST be atomic and correct across concurrent replicas.
        """
        raise NotImplementedError

    @abstractmethod
    async def get(
        self,
        key: str,
        window_seconds: int,
    ) -> int:
        """Return the current count for ``key`` without incrementing (status introspection)."""
        raise NotImplementedError
