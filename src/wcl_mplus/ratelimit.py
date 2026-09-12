"""Rate-limit awareness and polite scheduling.

Warcraft Logs meters the v2 API in "points" per hour rather than requests per
second. The schema exposes a rate-limit object; this module reads it and keeps
a local budget so collection slows down before the quota is exhausted rather
than after.

Field names are parsed **defensively**. The exact names on the rate-limit type
are not asserted by this repository -- `wclmplus recon` records the real ones
and `RateLimitState.parse` reports which key it actually matched, so a schema
change surfaces as a logged warning instead of a silent zero.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Candidate key names, most likely first. Matching is case-insensitive and
# ignores underscores, so limitPerHour / limit_per_hour / LimitPerHour all hit.
_LIMIT_KEYS = ("limitPerHour", "pointsPerHour", "limit", "hourlyLimit")
_SPENT_KEYS = ("pointsSpentThisHour", "pointsSpent", "spent", "usedPoints")
_RESET_KEYS = ("pointsResetIn", "resetIn", "secondsUntilReset", "resetsIn")


def _lookup(data: dict[str, Any], candidates: tuple[str, ...]) -> tuple[str | None, float | None]:
    """Find the first candidate key present, tolerant of naming style.

    Returns (matched_key, numeric_value).
    """
    normalized = {k.replace("_", "").lower(): (k, v) for k, v in data.items()}
    for candidate in candidates:
        hit = normalized.get(candidate.replace("_", "").lower())
        if hit is not None:
            key, value = hit
            if isinstance(value, (int, float)):
                return key, float(value)
            try:
                return key, float(value)
            except (TypeError, ValueError):
                return key, None
    return None, None


@dataclass
class RateLimitState:
    """A snapshot of the account's hourly point budget."""

    limit_per_hour: float | None = None
    points_spent: float | None = None
    reset_in_seconds: float | None = None
    observed_at: float = field(default_factory=time.time)
    #: Which schema keys were actually matched. Recorded for API_NOTES.md.
    matched_keys: dict[str, str | None] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: dict[str, Any] | None) -> RateLimitState:
        if not isinstance(data, dict):
            return cls()
        limit_key, limit = _lookup(data, _LIMIT_KEYS)
        spent_key, spent = _lookup(data, _SPENT_KEYS)
        reset_key, reset = _lookup(data, _RESET_KEYS)
        state = cls(
            limit_per_hour=limit,
            points_spent=spent,
            reset_in_seconds=reset,
            matched_keys={"limit": limit_key, "spent": spent_key, "reset": reset_key},
            raw=dict(data),
        )
        if limit_key is None or spent_key is None:
            logger.warning(
                "Rate-limit payload did not match expected field names; keys present: %s. "
                "Budget enforcement is degraded -- update ratelimit.py candidates.",
                sorted(data),
            )
        return state

    @property
    def known(self) -> bool:
        return self.limit_per_hour is not None and self.points_spent is not None

    @property
    def points_remaining(self) -> float | None:
        if not self.known:
            return None
        assert self.limit_per_hour is not None and self.points_spent is not None
        return max(0.0, self.limit_per_hour - self.points_spent)

    @property
    def fraction_used(self) -> float | None:
        if not self.known or not self.limit_per_hour:
            return None
        assert self.points_spent is not None
        return min(1.0, self.points_spent / self.limit_per_hour)

    def summary(self) -> dict[str, Any]:
        return {
            "limit_per_hour": self.limit_per_hour,
            "points_spent": self.points_spent,
            "points_remaining": self.points_remaining,
            "reset_in_seconds": self.reset_in_seconds,
            "matched_keys": self.matched_keys,
            "known": self.known,
        }


class QuotaExhausted(RuntimeError):
    """The hourly budget is spent and waiting was declined.

    Raised rather than silently sleeping for a long time, so the caller can
    checkpoint and exit cleanly instead of appearing to hang.
    """

    def __init__(self, state: RateLimitState, wait_seconds: float) -> None:
        super().__init__(
            f"Warcraft Logs hourly point budget exhausted "
            f"(spent {state.points_spent} of {state.limit_per_hour}). "
            f"Resets in ~{int(wait_seconds)}s. Collection checkpointed; resume later."
        )
        self.state = state
        self.wait_seconds = wait_seconds


class RateLimiter:
    """Local budget guard plus backoff helper.

    Two independent jobs:

    1. **Budget**: refuse to start a request when remaining points fall below
       `min_points_reserve`. The reserve exists so an interrupted run still has
       enough quota left to finish its current report rather than stopping
       halfway through a paginated event stream.
    2. **Backoff**: compute exponential-with-jitter sleeps, honouring an
       explicit `Retry-After` when the server sends one.
    """

    def __init__(
        self,
        *,
        min_points_reserve: int = 200,
        max_retries: int = 5,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
        jitter: float = 0.3,
    ) -> None:
        self.min_points_reserve = min_points_reserve
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.jitter = jitter
        self._state = RateLimitState()
        self._lock = threading.Lock()
        #: Set when a 429 tells us to hold off until a wall-clock time.
        self._hold_until: float = 0.0

    # -- state ------------------------------------------------------------

    @property
    def state(self) -> RateLimitState:
        return self._state

    def observe(self, state: RateLimitState) -> None:
        with self._lock:
            self._state = state
        remaining = state.points_remaining
        if remaining is not None and remaining < self.min_points_reserve:
            logger.warning(
                "Rate-limit reserve reached: %.0f points remaining (reserve %d).",
                remaining,
                self.min_points_reserve,
            )

    def note_retry_after(self, seconds: float) -> None:
        """Record a server-instructed hold (from a 429 `Retry-After`)."""
        with self._lock:
            self._hold_until = max(self._hold_until, time.time() + max(0.0, seconds))

    # -- gating -----------------------------------------------------------

    def budget_wait_seconds(self) -> float:
        """Seconds to wait before the next request may be issued (0 = go)."""
        now = time.time()
        with self._lock:
            hold = self._hold_until
            state = self._state
        if hold > now:
            return hold - now
        remaining = state.points_remaining
        if remaining is None:
            return 0.0  # unknown budget: proceed, conservatively paced elsewhere
        if remaining >= self.min_points_reserve:
            return 0.0
        # Below reserve: wait for the hourly window to roll over.
        reset = state.reset_in_seconds
        if reset is None:
            return 60.0
        elapsed = time.time() - state.observed_at
        return max(0.0, reset - elapsed) + 1.0

    def acquire(self, *, wait: bool = True, sleep=time.sleep) -> None:
        """Block until a request is permitted.

        With `wait=False` an exhausted budget raises `QuotaExhausted` so a
        caller can checkpoint and exit rather than sleep for many minutes.
        """
        delay = self.budget_wait_seconds()
        if delay <= 0:
            return
        if not wait:
            raise QuotaExhausted(self._state, delay)
        logger.warning("Pausing %.0fs for the rate-limit window to reset.", delay)
        sleep(delay)

    # -- backoff ----------------------------------------------------------

    def backoff_seconds(self, attempt: int, retry_after: float | None = None) -> float:
        """Sleep length before retry `attempt` (1-based).

        An explicit `Retry-After` always wins: the server knows better than
        our exponential curve.
        """
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, 3600.0)
        raw = self.base_backoff * (2 ** max(0, attempt - 1))
        capped = min(raw, self.max_backoff)
        # Jitter spreads retries so concurrent workers do not resynchronise.
        return capped * (1.0 + random.uniform(-self.jitter, self.jitter))
