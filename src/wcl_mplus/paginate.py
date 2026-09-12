"""Event pagination.

Pagination is treated as a correctness problem, not a convenience (brief
section 16). Two failure modes would quietly corrupt every downstream
statistic:

* **Missing events** at a page boundary -> undercounted casts;
* **Double-counted events** at a page boundary -> phantom mechanics.

The API returns a page of events plus a `nextPageTimestamp` cursor. This module
makes no assumption about whether that cursor is inclusive or exclusive,
because either choice would be a guess. Instead it is correct under both:

    Events at the previous page's final timestamp are matched against what was
    already emitted at that exact timestamp, as a **multiset**. A repeat is
    dropped only if an identical event was already emitted at the same
    timestamp and has not yet been accounted for.

Multiset subtraction rather than a set matters: two genuinely distinct events
can be byte-identical at the same millisecond (for instance two identical
periodic ticks). Deduplicating with a plain set would silently delete real
observations; matching counts removes exactly the re-sent copies and keeps the
rest.

Progress is verified on every page. A cursor that does not advance, or a page
whose every event is a boundary repeat, raises `PaginationStall` rather than
looping forever or returning a truncated stream that looks complete.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .redaction import RedactedError

logger = logging.getLogger(__name__)

#: Hard ceiling on pages per query, so a server-side cursor bug cannot spin
#: forever. 10k events/page x 2000 pages = 20M events, far beyond any one fight.
DEFAULT_MAX_PAGES = 2000


class PaginationError(RedactedError):
    """Pagination could not be completed correctly."""


class PaginationStall(PaginationError):
    """The cursor stopped advancing. Raised instead of looping or truncating."""


def event_fingerprint(event: dict[str, Any]) -> str:
    """Stable fingerprint of an event, used only for boundary matching.

    Hashes the whole event, so two events differing in any field -- including
    `sourceInstance` / `targetInstance`, which is how two copies of the same
    NPC are told apart -- are never treated as the same observation.
    """
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


def event_timestamp(event: dict[str, Any]) -> int | float | None:
    """Extract an event timestamp, tolerating naming differences."""
    for key in ("timestamp", "time", "ts"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


@dataclass
class PageResult:
    """One page as returned by the API."""

    events: list[dict[str, Any]]
    next_page_timestamp: int | float | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaginationCheckpoint:
    """Resumable pagination state.

    Persisted after every page so an interrupted download continues from the
    correct cursor -- including the boundary-duplicate bookkeeping, without
    which a resume would re-emit the events at the cursor timestamp.
    """

    kind: str
    report_code: str | None
    variables: dict[str, Any]
    next_start_time: int | float | None
    pages_fetched: int = 0
    events_emitted: int = 0
    boundary_timestamp: int | float | None = None
    boundary_counts: dict[str, int] = field(default_factory=dict)
    complete: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "report_code": self.report_code,
            "variables": self.variables,
            "next_start_time": self.next_start_time,
            "pages_fetched": self.pages_fetched,
            "events_emitted": self.events_emitted,
            "boundary_timestamp": self.boundary_timestamp,
            "boundary_counts": self.boundary_counts,
            "complete": self.complete,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PaginationCheckpoint:
        return cls(
            kind=data["kind"],
            report_code=data.get("report_code"),
            variables=data.get("variables", {}),
            next_start_time=data.get("next_start_time"),
            pages_fetched=int(data.get("pages_fetched", 0)),
            events_emitted=int(data.get("events_emitted", 0)),
            boundary_timestamp=data.get("boundary_timestamp"),
            boundary_counts=dict(data.get("boundary_counts", {})),
            complete=bool(data.get("complete", False)),
        )


@dataclass
class PaginationDiagnostics:
    """Per-query evidence for the validation report."""

    pages: int = 0
    events_returned: int = 0
    events_emitted: int = 0
    boundary_repeats_dropped: int = 0
    empty_pages: int = 0
    first_timestamp: int | float | None = None
    last_timestamp: int | float | None = None
    max_gap_ms: int | float = 0
    out_of_order_pages: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "events_returned_by_api": self.events_returned,
            "events_emitted": self.events_emitted,
            "boundary_repeats_dropped": self.boundary_repeats_dropped,
            "empty_pages": self.empty_pages,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "max_gap_ms": self.max_gap_ms,
            "out_of_order_pages": self.out_of_order_pages,
            "warnings": self.warnings,
        }


class EventPaginator:
    """Iterate all events for one query, exactly once each.

    `fetch_page(start_time)` returns a `PageResult`. Keeping fetching abstract
    means the whole paginator is testable against fixtures with no network.
    """

    def __init__(
        self,
        fetch_page: Callable[[int | float], PageResult],
        *,
        kind: str,
        start_time: int | float,
        end_time: int | float | None = None,
        report_code: str | None = None,
        variables: dict[str, Any] | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        gap_warn_ms: int | float = 60_000,
        on_checkpoint: Callable[[PaginationCheckpoint], None] | None = None,
        checkpoint: PaginationCheckpoint | None = None,
    ) -> None:
        self._fetch_page = fetch_page
        self.kind = kind
        self.start_time = start_time
        self.end_time = end_time
        self.report_code = report_code
        self.variables = variables or {}
        self.max_pages = max_pages
        self.gap_warn_ms = gap_warn_ms
        self._on_checkpoint = on_checkpoint
        self.diagnostics = PaginationDiagnostics()

        if checkpoint is not None:
            self.checkpoint = checkpoint
            self._boundary_ts = checkpoint.boundary_timestamp
            self._boundary_counts: Counter[str] = Counter(checkpoint.boundary_counts)
            self.diagnostics.pages = checkpoint.pages_fetched
            self.diagnostics.events_emitted = checkpoint.events_emitted
        else:
            self.checkpoint = PaginationCheckpoint(
                kind=kind,
                report_code=report_code,
                variables=self.variables,
                next_start_time=start_time,
            )
            self._boundary_ts = None
            self._boundary_counts = Counter()

    # -- iteration --------------------------------------------------------

    def iter_events(self) -> Iterator[dict[str, Any]]:
        """Yield every event exactly once, following all pages."""
        if self.checkpoint.complete:
            return

        cursor = self.checkpoint.next_start_time
        if cursor is None:
            cursor = self.start_time

        pages_this_run = 0
        while True:
            if pages_this_run >= self.max_pages:
                raise PaginationError(
                    f"{self.kind}: hit the {self.max_pages}-page ceiling at cursor "
                    f"{cursor}. Refusing to continue; the cursor is probably looping."
                )

            page = self._fetch_page(cursor)
            pages_this_run += 1
            self.diagnostics.pages += 1
            self.checkpoint.pages_fetched += 1

            raw_events = page.events or []
            self.diagnostics.events_returned += len(raw_events)
            if not raw_events:
                self.diagnostics.empty_pages += 1

            emitted_this_page = 0
            newly_emitted_at_boundary: Counter[str] = Counter()
            page_max_ts: int | float | None = None
            prev_ts: int | float | None = None

            for event in raw_events:
                ts = event_timestamp(event)
                if ts is None:
                    self._warn(f"event without a usable timestamp on page {self.diagnostics.pages}")
                else:
                    if prev_ts is not None and ts < prev_ts:
                        self.diagnostics.out_of_order_pages += 1
                    prev_ts = ts
                    page_max_ts = ts if page_max_ts is None else max(page_max_ts, ts)

                # Boundary repeat suppression: only at the exact cursor
                # timestamp, and only as many copies as were already emitted.
                if (
                    self._boundary_ts is not None
                    and ts == self._boundary_ts
                    and self._boundary_counts
                ):
                    fingerprint = event_fingerprint(event)
                    if self._boundary_counts.get(fingerprint, 0) > 0:
                        self._boundary_counts[fingerprint] -= 1
                        if self._boundary_counts[fingerprint] == 0:
                            del self._boundary_counts[fingerprint]
                        self.diagnostics.boundary_repeats_dropped += 1
                        continue

                # Gap diagnostic on the emitted stream.
                if ts is not None:
                    if self.diagnostics.first_timestamp is None:
                        self.diagnostics.first_timestamp = ts
                    if self.diagnostics.last_timestamp is not None:
                        gap = ts - self.diagnostics.last_timestamp
                        if gap > self.diagnostics.max_gap_ms:
                            self.diagnostics.max_gap_ms = gap
                    self.diagnostics.last_timestamp = ts

                if ts is not None and page_max_ts is not None and ts == page_max_ts:
                    newly_emitted_at_boundary[event_fingerprint(event)] += 1

                emitted_this_page += 1
                self.diagnostics.events_emitted += 1
                self.checkpoint.events_emitted += 1
                yield event

            next_cursor = page.next_page_timestamp

            # -- progress verification ------------------------------------
            if next_cursor is not None:
                if emitted_this_page == 0 and not raw_events:
                    # An empty page that still claims a next cursor: allow it
                    # once the cursor actually moves, otherwise it is a stall.
                    if next_cursor == cursor:
                        raise PaginationStall(
                            f"{self.kind}: empty page and unchanged cursor {cursor}."
                        )
                elif emitted_this_page == 0 and raw_events:
                    raise PaginationStall(
                        f"{self.kind}: page {self.diagnostics.pages} at cursor {cursor} "
                        f"returned {len(raw_events)} events but all were boundary "
                        "repeats, so no progress was made. This usually means a single "
                        "timestamp holds more events than one page can carry."
                    )
                if next_cursor < cursor:
                    raise PaginationStall(
                        f"{self.kind}: cursor went backwards ({cursor} -> {next_cursor})."
                    )

            # -- boundary bookkeeping -------------------------------------
            # Carry counts forward when consecutive pages share a boundary
            # timestamp; otherwise start fresh at the new boundary.
            if page_max_ts is not None:
                if self._boundary_ts == page_max_ts:
                    self._boundary_counts.update(newly_emitted_at_boundary)
                else:
                    self._boundary_ts = page_max_ts
                    self._boundary_counts = newly_emitted_at_boundary
            # An empty page leaves the previous boundary state untouched.

            if next_cursor is None:
                self.checkpoint.complete = True
                self.checkpoint.next_start_time = None
                self._save_checkpoint()
                break

            cursor = next_cursor
            self.checkpoint.next_start_time = cursor
            self.checkpoint.boundary_timestamp = self._boundary_ts
            self.checkpoint.boundary_counts = dict(self._boundary_counts)
            self._save_checkpoint()

        self._finalize_diagnostics()

    def collect(self) -> list[dict[str, Any]]:
        """Materialize all events. Convenience for small queries and tests."""
        return list(self.iter_events())

    # -- helpers ----------------------------------------------------------

    def _warn(self, message: str) -> None:
        text = f"{self.kind}: {message}"
        if text not in self.diagnostics.warnings:
            self.diagnostics.warnings.append(text)
        logger.warning(text)

    def _save_checkpoint(self) -> None:
        self.checkpoint.boundary_timestamp = self._boundary_ts
        self.checkpoint.boundary_counts = dict(self._boundary_counts)
        if self._on_checkpoint is not None:
            self._on_checkpoint(self.checkpoint)

    def _finalize_diagnostics(self) -> None:
        if self.diagnostics.max_gap_ms > self.gap_warn_ms:
            self._warn(
                f"largest gap between consecutive events was "
                f"{self.diagnostics.max_gap_ms}ms (> {self.gap_warn_ms}ms). "
                "Expected during downtime between pulls; investigate if it spans a pull."
            )
        if self.diagnostics.out_of_order_pages:
            self._warn(
                f"{self.diagnostics.out_of_order_pages} out-of-order timestamps observed; "
                "event ordering within a page is not guaranteed monotonic."
            )
