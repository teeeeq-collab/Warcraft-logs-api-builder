"""GraphQL client for the Warcraft Logs v2 public API.

Responsibilities:

* attach a bearer token, refreshing once on a 401;
* classify failures into retryable (network, 5xx, 429) and non-retryable
  (GraphQL validation, auth, 4xx) -- a malformed query must fail immediately
  rather than burn the retry budget and the hourly quota;
* honour `Retry-After` on 429 and feed the rate limiter;
* write every successful response to the raw cache, and serve from it on
  reruns so collection is idempotent and cheap to repeat.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .auth import AuthError, TokenProvider
from .ratelimit import RateLimiter, RateLimitState
from .rawcache import RawCache
from .redaction import RedactedError, redact
from .settings import Settings

logger = logging.getLogger(__name__)


class ApiError(RedactedError):
    """Base class for API failures."""

    retryable = False


class TransientApiError(ApiError):
    """Network problem, 5xx, or 429. Worth retrying."""

    retryable = True


class GraphQLError(ApiError):
    """The server understood the request and rejected it.

    Not retryable: a query that fails validation will fail identically on
    every attempt, so retrying only wastes quota.
    """

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class RateLimitedError(TransientApiError):
    """HTTP 429."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class RequestStats:
    """Counters for the validation report."""

    requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    rate_limited: int = 0
    failures: int = 0
    bytes_received: int = 0
    seconds_spent: float = 0.0
    points_spent_observed: float | None = None
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "failures": self.failures,
            "bytes_received": self.bytes_received,
            "seconds_spent": round(self.seconds_spent, 3),
            "points_spent_observed": self.points_spent_observed,
            "errors": self.errors[:20],
        }


#: Minimal query used to read the hourly budget. The field name is the one
#: concept this client has to guess; `recon` verifies it against the live
#: schema and reports a mismatch rather than failing silently.
RATE_LIMIT_QUERY = (
    "query RateLimit { rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }"
)


class GraphQLClient:
    """Thin, cache-aware GraphQL client."""

    def __init__(
        self,
        settings: Settings,
        *,
        token_provider: TokenProvider | None = None,
        rate_limiter: RateLimiter | None = None,
        cache: RawCache | None = None,
        http_client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self.settings = settings
        self.tokens = token_provider or TokenProvider(settings)
        self.rate_limiter = rate_limiter or RateLimiter(
            min_points_reserve=settings.min_points_reserve,
            max_retries=settings.max_retries,
        )
        self.cache = cache if cache is not None else RawCache(settings.raw_cache_dir)
        self._http = http_client
        self._owns_http = http_client is None
        self._sleep = sleep
        self.stats = RequestStats()
        #: Cache location of the most recent response, relative to the cache
        #: root, or None if the response was not cached. Read immediately after
        #: `execute` by callers that record provenance: it is the only link from
        #: a stored row back to the exact payload it came from.
        self.last_cache_path: str | None = None

    # -- lifecycle --------------------------------------------------------

    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=httpx.Timeout(self.settings.timeout_seconds),
                headers={"User-Agent": "wcl-mplus/0.1 (research collector)"},
            )
        return self._http

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None
        self.tokens.close()

    def __enter__(self) -> GraphQLClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- core -------------------------------------------------------------

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        kind: str,
        report_code: str | None = None,
        use_cache: bool = True,
        write_cache: bool = True,
        wait_for_quota: bool = True,
    ) -> dict[str, Any]:
        """Run a query and return its `data` object.

        `kind` is the logical request name used for cache identity and
        reporting (e.g. "report_fights", "events_casts"). `variables` form the
        rest of the cache identity.
        """
        variables = variables or {}
        # The rendered query text is part of the cache identity, not just its
        # name. Queries here are *generated* from introspection, so the same
        # `kind` can legitimately produce different documents between runs --
        # after a schema change, or after narrowing a sub-selection. Keying on
        # the name alone would serve a response that answers a different
        # question. `query_version` remains a coarse manual override.
        cache_params = {
            "variables": variables,
            "query_name": kind,
            "query_sha": hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
        }

        self.last_cache_path = None

        if use_cache:
            entry = self.cache.get(kind, cache_params, report_code=report_code)
            if entry is not None:
                self.stats.cache_hits += 1
                self.last_cache_path = self._cache_ref(kind, cache_params, report_code)
                logger.debug("Cache hit for %s (%s)", kind, report_code or "-")
                return entry.response if isinstance(entry.response, dict) else {}

        data = self._execute_uncached(query, variables, kind=kind, wait_for_quota=wait_for_quota)

        if write_cache:
            self.cache.put(kind, cache_params, data, report_code=report_code)
            self.last_cache_path = self._cache_ref(kind, cache_params, report_code)
        return data

    def _cache_ref(
        self, kind: str, cache_params: dict[str, Any], report_code: str | None
    ) -> str | None:
        """Cache location relative to the cache root.

        Relative, not absolute: the path is stored in the database and an
        absolute one would be meaningless on any other machine, which is
        precisely where a corpus gets audited.
        """
        try:
            path = self.cache.path_for(kind, cache_params, report_code=report_code)
            return str(path.relative_to(self.cache.root)).replace("\\", "/")
        except (ValueError, OSError):  # pragma: no cover - defensive
            return None

    def _execute_uncached(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        kind: str,
        wait_for_quota: bool,
    ) -> dict[str, Any]:
        attempts = self.settings.max_retries
        last_error: ApiError | None = None

        for attempt in range(1, attempts + 1):
            self.rate_limiter.acquire(wait=wait_for_quota, sleep=self._sleep)
            started = time.time()
            try:
                data = self._single_attempt(query, variables, kind=kind)
            except GraphQLError as exc:
                # Non-retryable by construction.
                self.stats.failures += 1
                self.stats.errors.append(f"{kind}: {exc}")
                raise
            except AuthError as exc:
                self.stats.failures += 1
                self.stats.errors.append(f"{kind}: {exc}")
                raise
            except TransientApiError as exc:
                last_error = exc
                self.stats.seconds_spent += time.time() - started
                if isinstance(exc, RateLimitedError):
                    self.stats.rate_limited += 1
                    if exc.retry_after is not None:
                        self.rate_limiter.note_retry_after(exc.retry_after)
                if attempt >= attempts:
                    break
                delay = self.rate_limiter.backoff_seconds(
                    attempt,
                    getattr(exc, "retry_after", None),
                )
                self.stats.retries += 1
                logger.warning(
                    "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                    kind,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                self._sleep(delay)
                continue
            else:
                self.stats.seconds_spent += time.time() - started
                return data

        self.stats.failures += 1
        message = f"{kind} failed after {attempts} attempts: {last_error}"
        self.stats.errors.append(message)
        raise TransientApiError(message)

    def _single_attempt(
        self, query: str, variables: dict[str, Any], *, kind: str
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self.tokens.auth_header())

        try:
            response = self.http().post(
                self.settings.api_url,
                json={"query": query, "variables": variables},
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise TransientApiError(f"Timeout contacting the API: {redact(exc)}") from None
        except httpx.HTTPError as exc:
            raise TransientApiError(
                f"Network error contacting the API: {type(exc).__name__}: {redact(exc)}"
            ) from None

        self.stats.requests += 1
        self.stats.bytes_received += len(response.content or b"")

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise RateLimitedError(
                "API returned HTTP 429 (rate limited)."
                + (f" Retry-After: {retry_after}s." if retry_after is not None else ""),
                retry_after=retry_after,
            )
        if response.status_code == 401:
            # One forced refresh, then treat as fatal: repeated 401s mean the
            # credentials are wrong, not that the token went stale.
            logger.info("Got HTTP 401; refreshing access token once.")
            self.tokens.token(force_refresh=True)
            raise TransientApiError("HTTP 401 from the API; token refreshed.")
        if response.status_code == 403:
            raise ApiError(
                "HTTP 403 from the API. The report may be private, or the client "
                "may lack access. Private-report access is out of scope for v1."
            )
        if response.status_code >= 500:
            raise TransientApiError(f"API returned HTTP {response.status_code}.")
        if response.status_code != 200:
            raise ApiError(
                f"Unexpected HTTP {response.status_code} from the API: "
                f"{redact(response.text[:300])}"
            )

        try:
            payload = response.json()
        except ValueError:
            raise TransientApiError(
                "API returned a non-JSON body (possibly an error page)."
            ) from None

        if not isinstance(payload, dict):
            raise GraphQLError(f"API returned a {type(payload).__name__}, expected an object.")

        errors = payload.get("errors")
        if errors:
            data = payload.get("data")
            rendered = "; ".join(
                str(err.get("message", err))[:200] for err in errors if isinstance(err, dict)
            )
            # Partial data with errors happens when one field resolves and
            # another does not. Surface it as a GraphQL error so a caller
            # cannot mistake a half-answer for a complete one.
            raise GraphQLError(
                f"GraphQL error(s) for {kind}: {rendered or errors!r}"
                + ("" if data is None else " (partial data was returned and discarded)"),
                errors=[e for e in errors if isinstance(e, dict)],
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise GraphQLError(f"GraphQL response for {kind} had no 'data' object.")
        return data

    # -- rate limit -------------------------------------------------------

    def fetch_rate_limit(self, *, wait_for_quota: bool = False) -> RateLimitState:
        """Read the hourly point budget and feed it to the limiter.

        Never cached: a cached budget is worse than no budget.
        """
        data = self._execute_uncached(
            RATE_LIMIT_QUERY, {}, kind="rate_limit", wait_for_quota=wait_for_quota
        )
        state = RateLimitState.parse(data.get("rateLimitData"))
        self.rate_limiter.observe(state)
        return state

    def measure_query_cost(
        self, query: str, variables: dict[str, Any], *, kind: str
    ) -> dict[str, Any]:
        """Measure the point cost of one query by reading the budget around it.

        The delta includes the cost of the two budget reads themselves, so the
        result is reported as an upper bound rather than an exact figure.
        """
        before = self.fetch_rate_limit()
        self._execute_uncached(query, variables, kind=kind, wait_for_quota=True)
        after = self.fetch_rate_limit()
        delta = None
        if before.points_spent is not None and after.points_spent is not None:
            delta = after.points_spent - before.points_spent
        return {
            "kind": kind,
            "points_before": before.points_spent,
            "points_after": after.points_spent,
            "delta_including_two_budget_reads": delta,
            "note": "Upper bound: includes the cost of the two rateLimitData reads.",
        }


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header given as seconds.

    HTTP-date form is not parsed; returning None falls back to exponential
    backoff, which is safe.
    """
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None
