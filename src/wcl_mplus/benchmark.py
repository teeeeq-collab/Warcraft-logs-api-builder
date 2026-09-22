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
    source_id: int | None = None
    target_id: int | None = None
    events: int = 0
    pages: int = 0
    bytes_received: int = 0
    seconds: float = 0.0
    points_spent: float | None = None
    exhausted: bool = False
    error: str | None = None
    #: Pages served from the raw cache. Must be zero: any other value means the
    #: cost figures on this row describe the cache rather than the API.
    cache_hits: int = 0
    #: Field names actually observed, and how many events carried each. This is
    #: what answers "does this stream carry what we need?" without guessing.
    fields_seen: dict[str, int] = field(default_factory=dict)
    event_types_seen: dict[str, int] = field(default_factory=dict)
    #: Distinct actor IDs actually seen on each side. This is the evidence for
    #: what a filter *means*: a sourceID-narrowed stream whose events all carry
    #: that source is filtering by source, and one that does not is doing
    #: something else. Never assumed from the parameter name.
    source_ids_seen: dict[int, int] = field(default_factory=dict)
    target_ids_seen: dict[int, int] = field(default_factory=dict)

    @property
    def label(self) -> str:
        base = f"{self.data_type}@{self.hostility}" if self.hostility else self.data_type
        if self.source_id is not None:
            base += f"[source={self.source_id}]"
        if self.target_id is not None:
            base += f"[target={self.target_id}]"
        return base

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
            "source_id": self.source_id,
            "target_id": self.target_id,
            "distinct_sources": len(self.source_ids_seen),
            "distinct_targets": len(self.target_ids_seen),
            "exhausted": self.exhausted,
            "cache_hits": self.cache_hits,
            "cost_is_measured": self.cache_hits == 0,
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
    def filter_ignored(self) -> bool:
        """All three requests returned the same count: the filter did nothing.

        CombatantInfo behaves this way -- it is per-player data that exists once
        per fight regardless of hostility. Without this check the three equal
        counts read as "unfiltered returns friendlies only", and the sum of
        parts double-counts the very same events, so a profile would be told to
        split a stream that cannot be split and would collect it twice.
        """
        # Exhaustion is required. Three probes that each stopped at the same page
        # cap also return identical counts, and calling that an ignored filter
        # would turn a measurement artefact into a finding about the API.
        return (
            self.all_exhausted
            and self.unfiltered > 0
            and self.unfiltered == self.enemies
            and self.unfiltered == self.friendlies
        )

    @property
    def verdict(self) -> str:
        if not self.all_exhausted:
            return "inconclusive (a probe hit its page cap before finishing)"
        if self.filter_ignored:
            return "hostility filter IGNORED -- all three requests returned the same events"
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
            # Meaningless when the filter is ignored: both sides are the same rows.
            "sum_of_parts": (None if self.filter_ignored else self.enemies + self.friendlies),
            "all_exhausted": self.all_exhausted,
            "filter_ignored": self.filter_ignored,
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
        target_id: int | None = None,
        max_pages: int = MAX_PROBE_PAGES,
    ) -> StreamMeasurement:
        """Page one stream, recording cost and observed shape."""
        result = StreamMeasurement(
            data_type=data_type,
            hostility=hostility,
            report_code=report_code,
            fight_id=fight_id,
            source_id=source_id,
            target_id=target_id,
        )
        before_bytes = self.client.stats.bytes_received
        before_cache_hits = self.client.stats.cache_hits
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
                        "targetID": target_id,
                    },
                    kind=(
                        f"benchmark_{data_type}_{hostility or 'any'}"
                        f"_s{source_id or 0}_t{target_id or 0}_{int(cursor)}"
                    ),
                    report_code=report_code,
                    # A cached page costs no bytes, no points and no time, so a
                    # re-probe of a stream measured earlier reported ~98 bytes
                    # and 1 point for tens of thousands of events. That is a
                    # measurement of the cache, presented as the cost of the
                    # API, understating it -- the one direction this tool must
                    # never fail in. Benchmarks always go to the wire.
                    use_cache=False,
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
        result.cache_hits = self.client.stats.cache_hits - before_cache_hits
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
            for field_name, tally in (
                ("sourceID", result.source_ids_seen),
                ("targetID", result.target_ids_seen),
            ):
                actor = event.get(field_name)
                if isinstance(actor, int):
                    tally[actor] = tally.get(actor, 0) + 1


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
        if m.cache_hits:
            lines.append(
                f"| ↳ **{m.cache_hits} cached page(s)** — cost above is the cache, "
                f"not the API | | | | | | |"
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
            total = "—" if c.filter_ignored else f"{c.enemies + c.friendlies:,}"
            lines.append(
                f"| `{c.data_type}` | {c.unfiltered:,} | {c.enemies:,} | {c.friendlies:,} "
                f"| {total} | {c.verdict} |"
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


# ---------------------------------------------------------------------------
# Focus-filter benchmark
# ---------------------------------------------------------------------------

#: Streams the focus-player redesign depends on. Each is probed unnarrowed, by
#: source, by target, and by both, because the four answer different questions
#: and nothing in the documentation says which of them the API supports.
FOCUS_STREAMS = (
    ("DamageDone", "Friendlies"),
    ("DamageTaken", None),
    ("Healing", "Friendlies"),
    ("Buffs", "Friendlies"),
    ("Casts", "Friendlies"),
    ("Resources", "Friendlies"),
)

#: Page cap for focus probes. Four variants across six streams is 24 requests;
#: a low cap keeps the whole benchmark inside a few points. Capped figures are
#: lower bounds and are reported as such.
FOCUS_MAX_PAGES = 3


@dataclass
class FocusComparison:
    """One stream, measured four ways against a single focus actor."""

    data_type: str
    hostility: str | None
    actor_id: int
    unnarrowed: StreamMeasurement
    by_source: StreamMeasurement
    by_target: StreamMeasurement
    by_both: StreamMeasurement

    @property
    def label(self) -> str:
        return f"{self.data_type}@{self.hostility}" if self.hostility else self.data_type

    def _ratio(self, part: StreamMeasurement, attr: str) -> float | None:
        whole = getattr(self.unnarrowed, attr) or 0
        piece = getattr(part, attr) or 0
        if not whole:
            return None
        return round(piece / whole, 4)

    @property
    def both_accepted(self) -> bool:
        """Does the API accept sourceID and targetID on one request?"""
        return self.by_both.error is None

    def filter_ignored(self, part: StreamMeasurement) -> bool:
        """True when a narrowed request returned the unnarrowed stream.

        Requires exhaustion on both sides. A page cap produces identical counts
        for an unrelated reason, and reading that as "the filter does nothing"
        is exactly the false positive that made an earlier hostility verdict
        wrong.
        """
        return (
            part.error is None
            and part.events == self.unnarrowed.events
            and part.exhausted
            and self.unnarrowed.exhausted
            and self.unnarrowed.events > 0
        )

    def source_semantics(self) -> str:
        """What sourceID actually selected, read off the returned events."""
        return _narrowing_verdict(self.by_source, self.actor_id, "source")

    def target_semantics(self) -> str:
        return _narrowing_verdict(self.by_target, self.actor_id, "target")

    def summary(self) -> dict[str, Any]:
        return {
            "stream": self.label,
            "actor_id": self.actor_id,
            "unnarrowed": self.unnarrowed.summary(),
            "by_source": self.by_source.summary(),
            "by_target": self.by_target.summary(),
            "by_both": self.by_both.summary(),
            "event_share_by_source": self._ratio(self.by_source, "events"),
            "event_share_by_target": self._ratio(self.by_target, "events"),
            "point_share_by_source": self._ratio(self.by_source, "points_spent"),
            "point_share_by_target": self._ratio(self.by_target, "points_spent"),
            "byte_share_by_source": self._ratio(self.by_source, "bytes_received"),
            "both_filters_accepted": self.both_accepted,
            "both_filters_error": self.by_both.error,
            "source_filter_ignored": self.filter_ignored(self.by_source),
            "target_filter_ignored": self.filter_ignored(self.by_target),
            "source_semantics": self.source_semantics(),
            "target_semantics": self.target_semantics(),
            "capped": not self.unnarrowed.exhausted,
        }


def _narrowing_verdict(part: StreamMeasurement, actor_id: int, side: str) -> str:
    """Describe what a narrowed stream returned, from the events themselves."""
    if part.error is not None:
        return f"REFUSED: {part.error}"
    if part.events == 0:
        return "EMPTY: the filter returned no events" + (
            "" if part.exhausted else " within the page cap"
        )
    seen = part.source_ids_seen if side == "source" else part.target_ids_seen
    others = {a: n for a, n in seen.items() if a != actor_id}
    if not seen:
        return f"UNKNOWN: returned events carry no {side}ID field"
    if not others:
        return f"CONFIRMED: every event has {side}ID = {actor_id}"
    share = sum(others.values()) / max(sum(seen.values()), 1)
    return (
        f"MIXED: {round(100 * share, 1)}% of events carry a different {side}ID "
        f"({len(others)} other actor(s)) -- the filter is not selecting by {side}"
    )


class FocusFilterBenchmark:
    """Measure what actor narrowing actually costs and actually means.

    The `reference_player` profile promises full telemetry for one player and
    cheap context for the rest, but nothing has ever measured whether narrowing
    a stream to one actor reduces its **cost** -- rate limiting is per page, and
    a server that filters after paging would charge the same for a tenth of the
    data. Nor has anything established what the two filters select: "buffs the
    focus player cast" and "buffs active on the focus player" are different
    data, and one word covers both in the current configuration.

    Every verdict here is read off the returned events, never inferred from a
    parameter's name.
    """

    def __init__(self, client: GraphQLClient, *, page_limit: int = PROBE_PAGE_LIMIT) -> None:
        self.probe = StreamBenchmark(client, page_limit=page_limit)
        self.client = client

    def compare(
        self,
        *,
        report_code: str,
        fight_id: int,
        rel_start_ms: int,
        rel_end_ms: int,
        actor_id: int,
        streams: tuple[tuple[str, str | None], ...] = FOCUS_STREAMS,
        max_pages: int = FOCUS_MAX_PAGES,
    ) -> list[FocusComparison]:
        out: list[FocusComparison] = []
        for data_type, hostility in streams:

            def run(
                source: int | None,
                target: int | None,
                _dt: str = data_type,
                _host: str | None = hostility,
            ) -> StreamMeasurement:
                # The stream is bound as a default argument: a closure over the
                # loop variable would measure the last stream four times.
                return self.probe.measure(
                    report_code=report_code,
                    fight_id=fight_id,
                    rel_start_ms=rel_start_ms,
                    rel_end_ms=rel_end_ms,
                    data_type=_dt,
                    hostility=_host,
                    source_id=source,
                    target_id=target,
                    max_pages=max_pages,
                )

            out.append(
                FocusComparison(
                    data_type=data_type,
                    hostility=hostility,
                    actor_id=actor_id,
                    unnarrowed=run(None, None),
                    by_source=run(actor_id, None),
                    by_target=run(None, actor_id),
                    by_both=run(actor_id, actor_id),
                )
            )
        return out


def overlap_check(
    damage_taken: StreamMeasurement, damage_done: StreamMeasurement
) -> dict[str, Any]:
    """Do DamageTaken and DamageDone, both narrowed by target, agree?

    They are different `EventDataType` values and may or may not describe the
    same events. If they agree, one of them is redundant in a focus profile and
    the cheaper one wins; if they disagree, both are needed and the profile must
    say which question each answers.
    """
    return {
        "damage_taken_events": damage_taken.events,
        "damage_done_events": damage_done.events,
        "equal_counts": damage_taken.events == damage_done.events,
        "both_exhausted": damage_taken.exhausted and damage_done.exhausted,
        "verdict": (
            "counts match and both streams paginated to exhaustion: probably the "
            "same events under two names, so a focus profile needs only one"
            if damage_taken.events == damage_done.events
            and damage_taken.exhausted
            and damage_done.exhausted
            else "counts differ, or a stream was capped: treat them as distinct "
            "until a full comparison says otherwise"
        ),
    }


def render_focus_report(comparisons: list[FocusComparison], *, actor_label: str) -> str:
    """The table the reference_player redesign is waiting on."""
    lines = [
        "# Focus-filter benchmark",
        "",
        f"Focus actor: `{actor_label}`",
        "",
        "Four requests per stream: unnarrowed, by source, by target, and both. "
        "Every verdict below is read off the returned events rather than inferred "
        "from a parameter name.",
        "",
        "## Cost",
        "",
        "| Stream | Events (all) | By source | By target | Points (all) | Points by source |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for c in comparisons:
        s = c.summary()
        lines.append(
            "| {} | {:,} | {:,} | {:,} | {} | {} |".format(
                s["stream"],
                c.unnarrowed.events,
                c.by_source.events,
                c.by_target.events,
                "?" if c.unnarrowed.points_spent is None else c.unnarrowed.points_spent,
                "?" if c.by_source.points_spent is None else c.by_source.points_spent,
            )
        )

    lines += [
        "",
        "**Does narrowing reduce cost, or only rows?** Compare the two point "
        "columns. Equal points for far fewer events means the server filters "
        "after paging, and a focus profile saves storage but not quota.",
        "",
        "## Semantics",
        "",
        "| Stream | sourceID selects | targetID selects | Both accepted |",
        "| --- | --- | --- | --- |",
    ]
    for c in comparisons:
        lines.append(
            f"| {c.label} | {c.source_semantics()} | {c.target_semantics()} | "
            f"{'yes' if c.both_accepted else 'NO: ' + str(c.by_both.error)} |"
        )

    capped = [c.label for c in comparisons if not c.unnarrowed.exhausted]
    if capped:
        lines += [
            "",
            "## Lower bounds",
            "",
            "These streams hit the page cap, so their unnarrowed totals are lower "
            "bounds and every share computed against them understates the saving: "
            + ", ".join(capped),
        ]
    return "\n".join(lines) + "\n"


def render_focus_json(comparisons: list[FocusComparison], *, actor_label: str) -> str:
    return json.dumps(
        {
            "focus_actor": actor_label,
            "measured_at": time.time(),
            "comparisons": [c.summary() for c in comparisons],
        },
        indent=2,
    )
