"""Measure what a stream costs, before it becomes a default.

Every figure the project has trusted so far came from a corpus that had already
been collected -- which is a fine way to learn what you already paid, and no way
at all to decide what to pay next. Five stream categories remain unmeasured, and
two of them are plausibly larger than everything currently stored.

This module answers that with a probe rather than an estimate: request one
stream, for one fight, and report what came back. It writes to a database the
caller names, never to the corpus, because a measurement that contaminates the
thing being measured is worse than no measurement.

It also settles the hostility question empirically. `Buffs` and `Casts` both
returned only the friendly side when requested unfiltered, so for every stream
the probe can request all three forms -- unfiltered, Enemies, Friendlies -- and
report whether the parts sum to the whole. That comparison is the only way to
know whether an unfiltered request is silently dropping half the data.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .client import ApiError, GraphQLClient
from .querybuild import load_query

logger = logging.getLogger(__name__)

#: Page size for probes. Smaller than collection's, because a probe measures
#: rate and shape rather than racing to exhaust a stream.
PROBE_PAGE_LIMIT = 2000

#: How many pages a probe will fetch before extrapolating. A stream that has not
#: finished by then is reported as a lower bound, never as a total.
MAX_PROBE_PAGES = 12


@dataclass
class StreamMeasurement:
    """What one stream cost on one fight."""

    data_type: str
    hostility: str | None
    report_code: str
    fight_id: int
    events: int = 0
    pages: int = 0
    bytes_received: int = 0
    seconds: float = 0.0
    points_spent: float | None = None
    exhausted: bool = False
    error: str | None = None
    #: Field names actually observed, and how many events carried each. This is
    #: what answers "does this stream carry what we need?" without guessing.
    fields_seen: dict[str, int] = field(default_factory=dict)
    event_types_seen: dict[str, int] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.data_type}@{self.hostility}" if self.hostility else self.data_type

    def summary(self) -> dict[str, Any]:
        return {
            "stream": self.label,
            "data_type": self.data_type,
            "hostility": self.hostility,
            "report_code": self.report_code,
            "fight_id": self.fight_id,
            "events": self.events,
            "pages": self.pages,
            "bytes_received": self.bytes_received,
            "seconds": round(self.seconds, 2),
            "points_spent": self.points_spent,
            "exhausted": self.exhausted,
            "bytes_per_event": (
                round(self.bytes_received / self.events, 1) if self.events else None
            ),
            "error": self.error,
            "fields_seen": dict(sorted(self.fields_seen.items(), key=lambda kv: -kv[1])),
            "event_types_seen": dict(sorted(self.event_types_seen.items(), key=lambda kv: -kv[1])),
        }


@dataclass
class HostilityComparison:
    """Whether an unfiltered request really returns both sides."""

    data_type: str
    unfiltered: int
    enemies: int
    friendlies: int
    all_exhausted: bool

    @property
    def verdict(self) -> str:
        if not self.all_exhausted:
            return "inconclusive (a probe hit its page cap before finishing)"
        total = self.enemies + self.friendlies
        if self.unfiltered == total:
            return "unfiltered = Enemies + Friendlies"
        if self.unfiltered == self.friendlies and self.enemies:
            return "unfiltered returns FRIENDLIES ONLY -- must be split"
        if self.unfiltered == self.enemies and self.friendlies:
            return "unfiltered returns ENEMIES ONLY -- must be split"
        return f"unfiltered ({self.unfiltered}) != parts ({total}) -- split and investigate"

    def summary(self) -> dict[str, Any]:
        return {
            "data_type": self.data_type,
            "unfiltered": self.unfiltered,
            "enemies": self.enemies,
            "friendlies": self.friendlies,
            "sum_of_parts": self.enemies + self.friendlies,
            "all_exhausted": self.all_exhausted,
            "verdict": self.verdict,
        }


class StreamBenchmark:
    """Probe streams against real fights without touching the corpus."""

    def __init__(self, client: GraphQLClient, *, page_limit: int = PROBE_PAGE_LIMIT) -> None:
        self.client = client
        self.page_limit = page_limit

    def measure(
        self,
        *,
        report_code: str,
        fight_id: int,
        rel_start_ms: int,
        rel_end_ms: int,
        data_type: str,
        hostility: str | None = None,
        source_id: int | None = None,
        max_pages: int = MAX_PROBE_PAGES,
    ) -> StreamMeasurement:
        """Page one stream, recording cost and observed shape."""
        result = StreamMeasurement(
            data_type=data_type,
            hostility=hostility,
            report_code=report_code,
            fight_id=fight_id,
        )
        before_bytes = self.client.stats.bytes_received
        points_before = self._points()
        started = time.monotonic()
        cursor: float = float(rel_start_ms)

        try:
            for _ in range(max_pages):
                data = self.client.execute(
                    load_query("report_events"),
                    {
                        "code": report_code,
                        "startTime": cursor,
                        "endTime": float(rel_end_ms),
                        "dataType": data_type,
                        "hostilityType": hostility,
                        "limit": self.page_limit,
                        "fightIDs": [fight_id],
                        "sourceID": source_id,
                        "targetID": None,
                    },
                    kind=f"benchmark_{data_type}_{hostility or 'any'}_{int(cursor)}",
                    report_code=report_code,
                )
                block = (((data.get("reportData") or {}).get("report")) or {}).get("events") or {}
                events = block.get("data") or []
                result.events += len(events)
                result.pages += 1
                self._observe(result, events)

                nxt = block.get("nextPageTimestamp")
                if nxt is None:
                    result.exhausted = True
                    break
                cursor = float(nxt)
        except ApiError as exc:
            # A stream the API refuses is a finding, not a crash: the probe
            # exists precisely to discover which requests are not permitted.
            result.error = str(exc)

        result.seconds = time.monotonic() - started
        result.bytes_received = self.client.stats.bytes_received - before_bytes
        points_after = self._points()
        if points_before is not None and points_after is not None:
            delta = points_after - points_before
            result.points_spent = delta if delta >= 0 else None
        return result

    def compare_hostility(
        self,
        *,
        report_code: str,
        fight_id: int,
        rel_start_ms: int,
        rel_end_ms: int,
        data_type: str,
        max_pages: int = MAX_PROBE_PAGES,
    ) -> tuple[HostilityComparison, list[StreamMeasurement]]:
        """Request one stream three ways and check the parts against the whole."""
        runs = [
            self.measure(
                report_code=report_code,
                fight_id=fight_id,
                rel_start_ms=rel_start_ms,
                rel_end_ms=rel_end_ms,
                data_type=data_type,
                hostility=hostility,
                max_pages=max_pages,
            )
            for hostility in (None, "Enemies", "Friendlies")
        ]
        unfiltered, enemies, friendlies = runs
        comparison = HostilityComparison(
            data_type=data_type,
            unfiltered=unfiltered.events,
            enemies=enemies.events,
            friendlies=friendlies.events,
            all_exhausted=all(r.exhausted and r.error is None for r in runs),
        )
        return comparison, runs

    # -- internals --------------------------------------------------------

    def _points(self) -> float | None:
        try:
            return self.client.fetch_rate_limit().points_spent
        except Exception as exc:  # pragma: no cover - network only
            logger.debug("Could not read the point budget: %s", exc)
            return None

    @staticmethod
    def _observe(result: StreamMeasurement, events: list[Any]) -> None:
        """Tally field names and event types, so shape is reported not assumed."""
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type") or "?")
            result.event_types_seen[kind] = result.event_types_seen.get(kind, 0) + 1
            for key in event:
                result.fields_seen[key] = result.fields_seen.get(key, 0) + 1


def render_report(
    measurements: list[StreamMeasurement],
    comparisons: list[HostilityComparison],
    *,
    runs_probed: int,
) -> str:
    """The table section 19 of the expanded brief asks for."""
    lines = [
        "# Stream cost benchmark",
        "",
        f"Fights probed: {runs_probed}. Page cap per stream: {MAX_PROBE_PAGES} "
        f"x {PROBE_PAGE_LIMIT} events.",
        "",
        "A stream marked **not exhausted** hit the page cap, so its figures are a "
        "LOWER BOUND, not a total. Extrapolating from it would understate the cost, "
        "which is the dangerous direction.",
        "",
        "| Stream | Events | Pages | KB | Seconds | Points | Exhausted |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for m in sorted(measurements, key=lambda x: -x.events):
        lines.append(
            f"| `{m.label}` | {m.events:,} | {m.pages} | {m.bytes_received / 1024:,.0f} "
            f"| {m.seconds:.1f} | {'—' if m.points_spent is None else m.points_spent:} "
            f"| {'yes' if m.exhausted else '**no — lower bound**'} |"
        )
        if m.error:
            lines.append(f"| ↳ error | colspan | | | | | {m.error} |")

    if comparisons:
        lines += [
            "",
            "## Does unfiltered mean both sides?",
            "",
            "`Buffs` and `Casts` were both found to return only friendlies when "
            "requested unfiltered. Every stream is checked rather than assumed.",
            "",
            "| Stream | Unfiltered | Enemies | Friendlies | Sum | Verdict |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
        for c in comparisons:
            lines.append(
                f"| `{c.data_type}` | {c.unfiltered:,} | {c.enemies:,} | {c.friendlies:,} "
                f"| {c.enemies + c.friendlies:,} | {c.verdict} |"
            )

    lines += ["", "## Observed fields per stream", ""]
    for m in sorted(measurements, key=lambda x: x.label):
        if not m.fields_seen:
            continue
        types = ", ".join(f"`{k}` ({v:,})" for k, v in list(m.event_types_seen.items())[:6])
        fields = ", ".join(f"`{k}`" for k in m.fields_seen)
        lines += [f"### {m.label}", "", f"Event types: {types}", "", f"Fields: {fields}", ""]

    return "\n".join(lines) + "\n"


def render_json(
    measurements: list[StreamMeasurement],
    comparisons: list[HostilityComparison],
    *,
    runs_probed: int,
) -> str:
    return json.dumps(
        {
            "runs_probed": runs_probed,
            "page_limit": PROBE_PAGE_LIMIT,
            "max_pages_per_stream": MAX_PROBE_PAGES,
            "measurements": [m.summary() for m in measurements],
            "hostility_comparisons": [c.summary() for c in comparisons],
        },
        indent=2,
        sort_keys=True,
    )
