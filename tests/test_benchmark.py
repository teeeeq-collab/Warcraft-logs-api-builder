"""The benchmark must measure, not estimate — and must say when it could not.

A cost figure that silently understates is worse than no figure: it licenses a
corpus size the quota or the disk cannot support. These tests pin the places
where that could happen.
"""

from __future__ import annotations

import json

import pytest
from wcl_simulator import REPORT_CODE, WclSimulator

from wcl_mplus.auth import Token
from wcl_mplus.benchmark import StreamBenchmark, render_json, render_report
from wcl_mplus.client import GraphQLClient
from wcl_mplus.ratelimit import RateLimiter
from wcl_mplus.rawcache import RawCache


class FakeTokens:
    def auth_header(self):
        return {"Authorization": "Bearer test"}

    def token(self, force_refresh=False):
        return Token(access_token="test", expires_at=9e9)

    def close(self):
        pass


@pytest.fixture
def bench(settings):
    def build(**kwargs):
        sim = WclSimulator(**kwargs)
        client = GraphQLClient(
            settings,
            token_provider=FakeTokens(),
            rate_limiter=RateLimiter(min_points_reserve=0),
            cache=RawCache(settings.raw_cache_dir),
            http_client=sim.client(),
            sleep=lambda s: None,
        )
        return StreamBenchmark(client, page_limit=25), sim

    return build


def _measure(bench, **over):
    probe, _ = bench(event_page_limit=25)
    return probe.measure(
        report_code=REPORT_CODE,
        fight_id=7,
        rel_start_ms=0,
        rel_end_ms=10_000_000,
        data_type=over.pop("data_type", "Casts"),
        **over,
    )


def test_measure_reports_cost_and_observed_shape(bench):
    m = _measure(bench)
    assert m.events > 0
    assert m.pages > 0
    assert m.seconds >= 0
    assert m.fields_seen, "field names must be observed, not assumed"
    assert m.event_types_seen
    summary = m.summary()
    assert summary["bytes_per_event"] is None or summary["bytes_per_event"] >= 0


def test_capped_probe_is_marked_as_a_lower_bound(bench):
    """Hitting the page cap must never be reported as a complete total."""
    m = _measure(bench, max_pages=1)
    assert m.pages == 1
    assert m.exhausted is False
    assert "lower bound" in render_report([m], [], runs_probed=1)


def test_hostility_comparison_names_the_split(bench):
    probe, _ = bench(event_page_limit=25)
    comparison, runs = probe.compare_hostility(
        report_code=REPORT_CODE,
        fight_id=7,
        rel_start_ms=0,
        rel_end_ms=10_000_000,
        data_type="Casts",
    )
    assert len(runs) == 3
    assert [r.hostility for r in runs] == [None, "Enemies", "Friendlies"]
    assert comparison.verdict
    assert comparison.summary()["sum_of_parts"] == comparison.enemies + comparison.friendlies


def test_inconclusive_comparison_is_not_dressed_up_as_a_verdict(bench):
    probe, _ = bench(event_page_limit=25)
    comparison, _ = probe.compare_hostility(
        report_code=REPORT_CODE,
        fight_id=7,
        rel_start_ms=0,
        rel_end_ms=10_000_000,
        data_type="Casts",
        max_pages=1,
    )
    assert "inconclusive" in comparison.verdict


def test_reports_render_without_measurements(bench):
    """An empty probe must still produce a readable artefact, not a crash."""
    assert "Stream cost benchmark" in render_report([], [], runs_probed=0)
    assert json.loads(render_json([], [], runs_probed=0))["runs_probed"] == 0


def test_json_report_is_machine_readable(bench):
    m = _measure(bench)
    payload = json.loads(render_json([m], [], runs_probed=1))
    assert payload["measurements"][0]["stream"] == m.label
    assert payload["max_pages_per_stream"] >= 1


def test_an_ignored_hostility_filter_is_not_read_as_friendly_only():
    """Three identical counts mean the filter did nothing, not that it worked.

    CombatantInfo returns the same five per-player rows for unfiltered,
    @Enemies and @Friendlies. Read as "friendlies only" a profile would split a
    stream that cannot be split and collect the same events twice, and the
    sum of parts would double-count them.
    """
    from wcl_mplus.benchmark import HostilityComparison

    c = HostilityComparison(
        data_type="CombatantInfo", unfiltered=5, enemies=5, friendlies=5, all_exhausted=True
    )
    assert c.filter_ignored is True
    assert "IGNORED" in c.verdict
    assert c.summary()["sum_of_parts"] is None

    real = HostilityComparison(
        data_type="Threat", unfiltered=1941, enemies=10090, friendlies=1941, all_exhausted=True
    )
    assert real.filter_ignored is False
    assert "FRIENDLIES ONLY" in real.verdict
    assert real.summary()["sum_of_parts"] == 12031


def test_capped_probes_returning_equal_counts_are_not_called_ignored():
    """Three probes stopping at the same page cap look identical but prove nothing."""
    from wcl_mplus.benchmark import HostilityComparison

    capped = HostilityComparison(
        data_type="DamageDone",
        unfiltered=24000,
        enemies=24000,
        friendlies=24000,
        all_exhausted=False,
    )
    assert capped.filter_ignored is False
    assert "inconclusive" in capped.verdict


def test_benchmark_never_measures_the_cache(bench):
    """A cached page costs nothing, so a cached probe understates the API.

    Re-probing a stream measured earlier reported ~98 bytes and 1 point for
    22,843 events, because the pages came from the raw cache. Cost figures must
    come from the wire or be marked as not a measurement.
    """
    probe, _ = bench(event_page_limit=25)
    kwargs = dict(
        report_code=REPORT_CODE,
        fight_id=7,
        rel_start_ms=0,
        rel_end_ms=10_000_000,
        data_type="Casts",
    )
    first = probe.measure(**kwargs)
    second = probe.measure(**kwargs)

    assert first.cache_hits == 0
    assert second.cache_hits == 0, "the second probe was served from the cache"
    assert second.bytes_received > 0
    # Same stream, same fight: the wire cost must reproduce, not collapse.
    assert second.bytes_received == first.bytes_received
    assert second.summary()["cost_is_measured"] is True
