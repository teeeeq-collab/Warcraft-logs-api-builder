"""GraphQL client tests: error classification, retries, caching, rate limits."""

from __future__ import annotations

import httpx
import pytest

from wcl_mplus.client import (
    ApiError,
    GraphQLClient,
    GraphQLError,
    TransientApiError,
    _parse_retry_after,
)
from wcl_mplus.ratelimit import RateLimiter
from wcl_mplus.rawcache import RawCache

TOKEN = "test-token-aaaaaaaaaaaaaaaaaaaa"


class FakeTokenProvider:
    """Stands in for OAuth so client tests never touch the token endpoint."""

    def __init__(self):
        self.refreshes = 0

    def auth_header(self):
        return {"Authorization": f"Bearer {TOKEN}"}

    def token(self, force_refresh=False):
        if force_refresh:
            self.refreshes += 1
        return None

    def close(self):
        pass


def build_client(settings, handler, *, sleeps=None):
    slept = sleeps if sleeps is not None else []
    return GraphQLClient(
        settings,
        token_provider=FakeTokenProvider(),
        rate_limiter=RateLimiter(min_points_reserve=0, max_retries=settings.max_retries),
        cache=RawCache(settings.raw_cache_dir),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: slept.append(s),
    ), slept


def data_handler(payload, status=200):
    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(status, json=payload)

    return handler


# -- happy path ------------------------------------------------------------


def test_successful_query_returns_data(settings):
    client, _ = build_client(
        settings, data_handler({"data": {"reportData": {"report": {"code": "X"}}}})
    )
    out = client.execute("query Q { x }", {"a": 1}, kind="test_query")
    assert out == {"reportData": {"report": {"code": "X"}}}
    assert client.stats.requests == 1


def test_response_is_cached_and_reused(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"data": {"v": 1}})

    client, _ = build_client(settings, handler)
    first = client.execute("query Q { v }", {"a": 1}, kind="k")
    second = client.execute("query Q { v }", {"a": 1}, kind="k")
    assert first == second == {"v": 1}
    assert len(calls) == 1, "second call served from the raw cache"
    assert client.stats.cache_hits == 1


def test_different_variables_are_separate_cache_entries(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"data": {"v": len(calls)}})

    client, _ = build_client(settings, handler)
    client.execute("q", {"page": 1}, kind="k")
    client.execute("q", {"page": 2}, kind="k")
    assert len(calls) == 2


def test_cached_response_holds_no_authorization_header(settings):
    client, _ = build_client(settings, data_handler({"data": {"v": 1}}))
    client.execute("q", {"a": 1}, kind="k", report_code="ABC")
    files = list(settings.raw_cache_dir.rglob("*.json.gz"))
    assert files, "a cache file was written"
    import gzip

    for path in files:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            content = handle.read()
        assert TOKEN not in content
        assert "Authorization" not in content


# -- error classification --------------------------------------------------


def test_graphql_errors_are_not_retried(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"errors": [{"message": "Cannot query field 'bogus'"}]})

    client, _ = build_client(settings, handler)
    with pytest.raises(GraphQLError, match="bogus"):
        client.execute("q", {}, kind="bad")
    assert len(calls) == 1, "a validation error must not consume the retry budget"


def test_partial_data_with_errors_is_rejected(settings):
    """Half an answer must not be mistaken for a complete one."""
    client, _ = build_client(
        settings,
        data_handler({"data": {"a": 1}, "errors": [{"message": "field b failed"}]}),
    )
    with pytest.raises(GraphQLError, match="partial data"):
        client.execute("q", {}, kind="partial")


def test_missing_data_object_is_an_error(settings):
    client, _ = build_client(settings, data_handler({"notdata": {}}))
    with pytest.raises(GraphQLError, match="no 'data' object"):
        client.execute("q", {}, kind="k")


def test_403_is_not_retried_and_explains_private_reports(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, text="forbidden")

    client, _ = build_client(settings, handler)
    with pytest.raises(ApiError, match="private"):
        client.execute("q", {}, kind="k")
    assert len(calls) == 1


def test_server_error_is_retried_then_fails(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502, text="bad gateway")

    client, slept = build_client(settings, handler)
    with pytest.raises(TransientApiError, match="failed after"):
        client.execute("q", {}, kind="k")
    assert len(calls) == settings.max_retries
    assert len(slept) == settings.max_retries - 1
    assert client.stats.retries == settings.max_retries - 1


def test_retry_then_success(settings):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": {"ok": True}})

    client, slept = build_client(settings, handler)
    assert client.execute("q", {}, kind="k") == {"ok": True}
    assert len(calls) == 3
    assert len(slept) == 2


def test_backoff_grows(settings):
    def handler(request):
        return httpx.Response(500)

    client, slept = build_client(settings, handler)
    with pytest.raises(TransientApiError):
        client.execute("q", {}, kind="k")
    assert slept[1] > slept[0], "exponential backoff between attempts"


def test_timeout_is_transient_and_retried(settings):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"data": {"ok": 1}})

    client, _ = build_client(settings, handler)
    assert client.execute("q", {}, kind="k") == {"ok": 1}


def test_non_json_body_is_transient(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, text="<html>503 from a proxy</html>")

    client, _ = build_client(settings, handler)
    with pytest.raises(TransientApiError, match="non-JSON"):
        client.execute("q", {}, kind="k")
    assert len(calls) == settings.max_retries


# -- rate limiting ---------------------------------------------------------


def test_429_honours_retry_after(settings):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"data": {"ok": 1}})

    client, slept = build_client(settings, handler)
    assert client.execute("q", {}, kind="k") == {"ok": 1}
    assert 7 in slept, "server-provided Retry-After takes precedence over backoff"
    assert client.stats.rate_limited == 1


def test_429_without_retry_after_uses_backoff(settings):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"data": {"ok": 1}})

    client, slept = build_client(settings, handler)
    client.execute("q", {}, kind="k")
    assert slept and slept[0] > 0


def test_401_triggers_one_token_refresh(settings):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"data": {"ok": 1}})

    client, _ = build_client(settings, handler)
    assert client.execute("q", {}, kind="k") == {"ok": 1}
    assert client.tokens.refreshes == 1


def test_fetch_rate_limit_parses_and_feeds_limiter(settings):
    client, _ = build_client(
        settings,
        data_handler(
            {
                "data": {
                    "rateLimitData": {
                        "limitPerHour": 3600,
                        "pointsSpentThisHour": 120.5,
                        "pointsResetIn": 1800,
                    }
                }
            }
        ),
    )
    state = client.fetch_rate_limit()
    assert state.limit_per_hour == 3600
    assert state.points_remaining == pytest.approx(3479.5)
    assert client.rate_limiter.state.known


def test_rate_limit_query_is_never_cached(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "data": {"rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": len(calls)}}
            },
        )

    client, _ = build_client(settings, handler)
    client.fetch_rate_limit()
    client.fetch_rate_limit()
    assert len(calls) == 2, "a cached budget would be worse than none"


def test_unknown_rate_limit_shape_degrades_gracefully(settings):
    client, _ = build_client(settings, data_handler({"data": {"rateLimitData": {"surprise": 1}}}))
    state = client.fetch_rate_limit()
    assert not state.known
    assert state.points_remaining is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("5", 5.0),
        ("0", 0.0),
        ("  12  ", 12.0),
        (None, None),
        ("", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
    ],
)
def test_parse_retry_after(header, expected):
    assert _parse_retry_after(header) == expected


def test_stats_summary_is_serializable(settings):
    client, _ = build_client(settings, data_handler({"data": {"ok": 1}}))
    client.execute("q", {}, kind="k")
    import json

    assert json.loads(json.dumps(client.stats.summary()))["requests"] == 1
