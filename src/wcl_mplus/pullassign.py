"""Assigning events to Warcraft Logs' own pull boundaries.

WCL already models dungeon pulls and this project treats those boundaries as
authoritative (brief section 18). No combat-gap detector is implemented.

Two things matter more than the assignment itself:

* **Events outside every pull are kept**, never dropped. Between-pull events
  are where movement, drinking, and out-of-combat deaths live, and a corpus
  that silently discards them cannot answer why a pull started badly.
* **Overlapping pull intervals are reported, not resolved silently.** If two
  pulls claim the same millisecond, assigning to the first is a choice that
  must be visible in the diagnostics.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PullInterval:
    """One pull's time window, in report-relative milliseconds."""

    pull_id: str
    start_ms: int
    end_ms: int
    index: int


#: How two pull intervals can collide. The distinction matters because only one
#: of these is a real ambiguity about what happened in the dungeon.
#:
#: * `touching`  -- the later pull starts on the exact millisecond the earlier
#:   one ends. Intervals are closed at both ends, so exactly one millisecond is
#:   claimed twice. This is a boundary convention, not an overlap: no event can
#:   be in doubt beyond that single tick.
#: * `nested`    -- the later pull lies entirely inside the earlier one.
#: * `partial`   -- genuine temporal overlap, which is what a chained pull looks
#:   like: the tank engaged the next pack while the previous one was alive.
OVERLAP_KINDS = ("touching", "nested", "partial")


def classify_overlap(earlier: PullInterval, later: PullInterval) -> dict[str, Any]:
    """Describe how two intervals collide, and by how much."""
    overlap_ms = min(earlier.end_ms, later.end_ms) - later.start_ms
    if later.start_ms == earlier.end_ms:
        kind = "touching"
    elif later.end_ms <= earlier.end_ms:
        kind = "nested"
    else:
        kind = "partial"
    return {
        "earlier_pull_id": earlier.pull_id,
        "later_pull_id": later.pull_id,
        "kind": kind,
        "overlap_ms": max(overlap_ms, 0),
        "window_start_ms": later.start_ms,
        "window_end_ms": min(earlier.end_ms, later.end_ms),
    }


@dataclass
class AssignmentStats:
    """Evidence about how well events mapped onto pulls."""

    assigned: int = 0
    unassigned: int = 0
    overlapping_pairs: list[tuple[str, str]] = field(default_factory=list)
    #: Classified collisions: kind and magnitude, not just "these two touched".
    overlaps: list[dict[str, Any]] = field(default_factory=list)
    unassigned_before_first: int = 0
    unassigned_after_last: int = 0
    unassigned_between: int = 0

    @property
    def total(self) -> int:
        return self.assigned + self.unassigned

    @property
    def assigned_fraction(self) -> float | None:
        return (self.assigned / self.total) if self.total else None

    def summary(self) -> dict[str, Any]:
        return {
            "events": self.total,
            "assigned": self.assigned,
            "unassigned": self.unassigned,
            "assigned_pct": (
                None if self.assigned_fraction is None else round(100 * self.assigned_fraction, 2)
            ),
            "unassigned_before_first_pull": self.unassigned_before_first,
            "unassigned_between_pulls": self.unassigned_between,
            "unassigned_after_last_pull": self.unassigned_after_last,
            "overlapping_pull_pairs": self.overlapping_pairs[:20],
            "overlaps": self.overlaps[:20],
        }


class PullAssigner:
    """Maps a report-relative timestamp to the pull containing it.

    Intervals are treated as **closed** at both ends: a pull runs
    `[start, end]`. An event exactly on a boundary belongs to that pull rather
    than to the gap beside it.
    """

    def __init__(self, intervals: list[PullInterval]) -> None:
        self.intervals = sorted(intervals, key=lambda i: (i.start_ms, i.end_ms))
        self._starts = [i.start_ms for i in self.intervals]
        self.stats = AssignmentStats()
        self.overlaps = self._find_overlaps()
        self.stats.overlapping_pairs = [(a.pull_id, b.pull_id) for a, b in self.overlaps]
        self.stats.overlaps = [classify_overlap(a, b) for a, b in self.overlaps]

    def _find_overlaps(self) -> list[tuple[PullInterval, PullInterval]]:
        found: list[tuple[PullInterval, PullInterval]] = []
        for earlier, later in zip(self.intervals, self.intervals[1:], strict=False):
            if later.start_ms <= earlier.end_ms:
                found.append((earlier, later))
        return found

    def find(self, rel_ms: int) -> PullInterval | None:
        """The pull containing `rel_ms`, or None.

        Binary search on pull starts, then a short backward scan: with
        overlapping pulls the containing interval is not always the one whose
        start is nearest, and a handful of pulls per run makes the scan free.
        """
        if not self.intervals:
            return None
        position = bisect.bisect_right(self._starts, rel_ms) - 1
        while position >= 0:
            candidate = self.intervals[position]
            if candidate.start_ms <= rel_ms <= candidate.end_ms:
                return candidate
            # Only a pull that started earlier can still be running; once a
            # candidate ends before our timestamp and none overlaps, stop.
            if not self.overlaps:
                break
            position -= 1
        return None

    def assign(self, rel_ms: int) -> PullInterval | None:
        """`find`, while recording where unassigned events fall."""
        hit = self.find(rel_ms)
        if hit is not None:
            self.stats.assigned += 1
            return hit

        self.stats.unassigned += 1
        if not self.intervals:
            pass
        elif rel_ms < self.intervals[0].start_ms:
            self.stats.unassigned_before_first += 1
        elif rel_ms > self.intervals[-1].end_ms:
            self.stats.unassigned_after_last += 1
        else:
            self.stats.unassigned_between += 1
        return None

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> PullAssigner:
        """Build from `pulls` rows."""
        return cls(
            [
                PullInterval(
                    pull_id=str(row["pull_id"]),
                    start_ms=int(row["rel_start_ms"]),
                    end_ms=int(row["rel_end_ms"]),
                    index=int(row["pull_index"]),
                )
                for row in rows
            ]
        )
