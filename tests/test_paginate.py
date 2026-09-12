"""Pagination correctness tests.

The paginator must be correct without knowing whether the API's
`nextPageTimestamp` cursor is inclusive or exclusive, so the central tests run
against a simulator parameterized over both semantics.
"""

from __future__ import annotations

import pytest

from wcl_mplus.paginate import (
    EventPaginator,
    PageResult,
    PaginationCheckpoint,
    PaginationError,
    PaginationStall,
    event_fingerprint,
)


class FakeEventApi:
    """Simulates a paginated event endpoint over a known event list.

    `inclusive_cursor=True` models a cursor that points *at* the last returned
    timestamp, so boundary events are re-sent on the next page. `False` models
    a cursor already advanced past it. Real semantics are unverified, so the
    paginator is tested against both.
    """

    def __init__(self, events, *, limit=100, inclusive_cursor=True):
        self.events = sorted(events, key=lambda e: e["timestamp"])
        self.limit = limit
        self.inclusive_cursor = inclusive_cursor
        self.calls: list[int | float] = []
        self.fail_times: dict[int, int] = {}

    def fetch(self, start_time):
        self.calls.append(start_time)
        remaining = [e for e in self.events if e["timestamp"] >= start_time]
        page = remaining[: self.limit]
        if len(remaining) <= self.limit:
            next_ts = None
        else:
            last_ts = page[-1]["timestamp"]
            next_ts = last_ts if self.inclusive_cursor else last_ts + 1
        return PageResult(events=[dict(e) for e in page], next_page_timestamp=next_ts)


def make_events(n, *, start=1000, step=100, ability=1):
    return [
        {"timestamp": start + i * step, "type": "cast", "abilityGameID": ability, "seq": i}
        for i in range(n)
    ]


def paginator(api, **kwargs):
    kwargs.setdefault("kind", "events_test")
    kwargs.setdefault("start_time", 0)
    return EventPaginator(api.fetch, **kwargs)


# -- basic completeness ----------------------------------------------------


@pytest.mark.parametrize("inclusive", [True, False])
def test_single_page(inclusive):
    events = make_events(10)
    api = FakeEventApi(events, limit=100, inclusive_cursor=inclusive)
    out = paginator(api).collect()
    assert out == events
    assert len(api.calls) == 1


@pytest.mark.parametrize("inclusive", [True, False])
def test_multi_page_no_loss_no_duplication(inclusive):
    events = make_events(250)
    api = FakeEventApi(events, limit=100, inclusive_cursor=inclusive)
    pag = paginator(api)
    out = pag.collect()
    assert [e["seq"] for e in out] == list(range(250)), "every event exactly once, in order"
    assert pag.diagnostics.pages >= 3
    if inclusive:
        assert pag.diagnostics.boundary_repeats_dropped > 0
    else:
        assert pag.diagnostics.boundary_repeats_dropped == 0


@pytest.mark.parametrize("inclusive", [True, False])
def test_ten_thousand_events_full_pages(inclusive):
    """A full 10,000-event page followed by more, at the documented page cap."""
    events = make_events(10_000 + 137)
    api = FakeEventApi(events, limit=10_000, inclusive_cursor=inclusive)
    out = paginator(api).collect()
    assert len(out) == 10_137
    assert len({e["seq"] for e in out}) == 10_137


@pytest.mark.parametrize("inclusive", [True, False])
def test_exact_multiple_of_page_size(inclusive):
    """The off-by-one case: the last page is exactly full."""
    events = make_events(200)
    api = FakeEventApi(events, limit=100, inclusive_cursor=inclusive)
    out = paginator(api).collect()
    assert [e["seq"] for e in out] == list(range(200))


# -- shared boundary timestamps -------------------------------------------


def test_many_events_share_boundary_timestamp():
    """Several events on the identical millisecond must all survive."""
    events = (
        make_events(98)
        + [
            {"timestamp": 20_000, "type": "damage", "abilityGameID": 7, "seq": 900 + i}
            for i in range(5)
        ]
        + make_events(50, start=30_000, step=10)
    )
    api = FakeEventApi(events, limit=100, inclusive_cursor=True)
    out = paginator(api).collect()
    at_boundary = [e for e in out if e["timestamp"] == 20_000]
    assert len(at_boundary) == 5, "no boundary event dropped"
    assert len(out) == len(events)


def test_identical_events_at_same_timestamp_are_both_kept():
    """Byte-identical events at one timestamp are distinct observations.

    A set-based dedupe would collapse these and silently lose a real event.
    """
    duplicate = {"timestamp": 5_000, "type": "damage", "abilityGameID": 42, "amount": 100}
    events = make_events(99) + [dict(duplicate), dict(duplicate)] + make_events(5, start=90_000)
    api = FakeEventApi(events, limit=100, inclusive_cursor=True)
    out = paginator(api).collect()
    identical = [e for e in out if e.get("amount") == 100 and e["timestamp"] == 5_000]
    assert len(identical) == 2, "multiset matching keeps both genuine copies"
    assert len(out) == len(events)


def test_timestamp_larger_than_page_raises_rather_than_looping():
    """A timestamp holding more events than one page cannot be paged through.

    That is a real API limitation. It must surface loudly, not as an infinite
    loop or a silently truncated stream.
    """
    events = [
        {"timestamp": 1_000, "type": "damage", "abilityGameID": 1, "seq": i} for i in range(12)
    ]
    api = FakeEventApi(events, limit=5, inclusive_cursor=True)
    with pytest.raises(PaginationStall, match="boundary repeats"):
        paginator(api).collect()


# -- loop and stall guards -------------------------------------------------


def test_repeated_cursor_with_empty_page_stalls():
    class StuckApi:
        def fetch(self, start_time):
            return PageResult(events=[], next_page_timestamp=start_time)

    pag = EventPaginator(StuckApi().fetch, kind="stuck", start_time=0)
    with pytest.raises(PaginationStall, match="unchanged cursor"):
        pag.collect()


def test_backwards_cursor_stalls():
    class BackwardsApi:
        def __init__(self):
            self.n = 0

        def fetch(self, start_time):
            self.n += 1
            return PageResult(
                events=make_events(3, start=10_000 * self.n),
                next_page_timestamp=0 if self.n > 1 else 10_000,
            )

    pag = EventPaginator(BackwardsApi().fetch, kind="back", start_time=0)
    with pytest.raises(PaginationStall, match="backwards"):
        pag.collect()


def test_page_ceiling_is_enforced():
    class EndlessApi:
        def __init__(self):
            self.t = 0

        def fetch(self, start_time):
            self.t += 1000
            return PageResult(
                events=[{"timestamp": self.t, "type": "cast"}],
                next_page_timestamp=self.t + 1,
            )

    pag = EventPaginator(EndlessApi().fetch, kind="endless", start_time=0, max_pages=25)
    with pytest.raises(PaginationError, match="page ceiling"):
        pag.collect()
    assert pag.diagnostics.pages == 25


# -- interruption and resume ----------------------------------------------


@pytest.mark.parametrize("inclusive", [True, False])
def test_interrupted_download_resumes_without_loss_or_duplication(inclusive):
    """Stop mid-stream, persist the checkpoint, resume, and compare."""
    events = make_events(500)
    api = FakeEventApi(events, limit=100, inclusive_cursor=inclusive)

    saved: list[dict] = []
    pag = EventPaginator(
        api.fetch,
        kind="events_test",
        start_time=0,
        on_checkpoint=lambda cp: saved.append(cp.to_json()),
    )

    collected = []
    for event in pag.iter_events():
        collected.append(event)
        if len(collected) == 220:
            break  # simulate Ctrl+C / crash

    assert saved, "a checkpoint was persisted before the interruption"
    checkpoint = PaginationCheckpoint.from_json(saved[-1])

    # Resume from the last persisted checkpoint, as a fresh process would.
    api2 = FakeEventApi(events, limit=100, inclusive_cursor=inclusive)
    resumed = EventPaginator(
        api2.fetch, kind="events_test", start_time=0, checkpoint=checkpoint
    ).collect()

    # The events already yielded past the last checkpoint are re-delivered on
    # resume; ingestion is idempotent by primary key, so the requirement is
    # that the union is complete and internally duplicate-free.
    union = {e["seq"] for e in collected} | {e["seq"] for e in resumed}
    assert union == set(range(500)), "resume loses no events"
    assert len({e["seq"] for e in resumed}) == len(resumed), "resume emits no duplicates"


def test_checkpoint_roundtrip_preserves_boundary_state():
    cp = PaginationCheckpoint(
        kind="k",
        report_code="AbCd",
        variables={"dataType": "Casts"},
        next_start_time=1234,
        pages_fetched=3,
        events_emitted=300,
        boundary_timestamp=1234,
        boundary_counts={"aa": 2},
    )
    again = PaginationCheckpoint.from_json(cp.to_json())
    assert again.to_json() == cp.to_json()


def test_completed_checkpoint_yields_nothing():
    api = FakeEventApi(make_events(10))
    cp = PaginationCheckpoint(
        kind="k", report_code=None, variables={}, next_start_time=None, complete=True
    )
    assert EventPaginator(api.fetch, kind="k", start_time=0, checkpoint=cp).collect() == []
    assert api.calls == []


# -- transient failures ----------------------------------------------------


def test_transient_failure_propagates_to_caller_for_retry():
    """The paginator does not retry; the client owns retry policy.

    It must not swallow the error and return a short stream that looks whole.
    """

    class FlakyApi:
        def __init__(self):
            self.n = 0

        def fetch(self, start_time):
            self.n += 1
            if self.n == 2:
                raise TimeoutError("connection reset")
            return PageResult(
                events=make_events(100, start=self.n * 100_000),
                next_page_timestamp=self.n * 100_000 + 99 * 100,
            )

    pag = EventPaginator(FlakyApi().fetch, kind="flaky", start_time=0)
    with pytest.raises(TimeoutError):
        pag.collect()


# -- diagnostics -----------------------------------------------------------


def test_gap_warning_is_recorded():
    events = make_events(5) + make_events(5, start=5_000_000)
    api = FakeEventApi(events, limit=100)
    pag = paginator(api, gap_warn_ms=1000)
    pag.collect()
    assert pag.diagnostics.max_gap_ms > 1000
    assert any("largest gap" in w for w in pag.diagnostics.warnings)


def test_event_without_timestamp_is_kept_and_flagged():
    """Unknown shape is preserved and reported, never dropped."""
    events = [{"type": "weird", "noTimestamp": True}]
    api = FakeEventApi([], limit=10)
    api.events = events

    def fetch(_start):
        return PageResult(events=[dict(e) for e in events], next_page_timestamp=None)

    pag = EventPaginator(fetch, kind="odd", start_time=0)
    out = pag.collect()
    assert out == events
    assert any("without a usable timestamp" in w for w in pag.diagnostics.warnings)


def test_fingerprint_distinguishes_npc_instances():
    """Two copies of the same NPC must never share a fingerprint."""
    base = {"timestamp": 1000, "type": "begincast", "sourceID": 55, "abilityGameID": 9}
    a = {**base, "sourceInstance": 1}
    b = {**base, "sourceInstance": 2}
    assert event_fingerprint(a) != event_fingerprint(b)
