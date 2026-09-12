"""Duplicate-run detection.

One real Mythic+ run is often uploaded by several members of the group, so a
naive corpus counts it repeatedly and inflates every frequency statistic while
understating variance (brief section 9).

The rule here is deliberately cautious: **prefer false negatives.** Two records
of one run left separate cost a little statistical power. Two genuinely
different runs merged, or one deleted, corrupt the data irrecoverably. So
nothing is ever deleted; probable duplicates are grouped and flagged, and one
member is marked canonical for analyses that need a single row per run.

Matching uses several independent fields, never one:

* the same dungeon and key level,
* start times within a tolerance (uploads of one run do not start at the same
  millisecond -- clients begin logging at slightly different moments),
* overlapping rosters, which is the strongest signal available,
* similar durations.

A pair agreeing on dungeon, key and roster but *not* on time is not a
duplicate: it is the same group running the same key twice, which is a real
second observation.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .db import Database

logger = logging.getLogger(__name__)

#: How far apart two uploads of the same run may start. Generous: clients begin
#: logging when each player enters, which can differ by a minute or more.
DEFAULT_START_TOLERANCE_MS = 5 * 60 * 1000

#: How far apart two uploads of the same run may be in length, as a fraction.
DEFAULT_DURATION_TOLERANCE = 0.10

#: Minimum share of the roster that must match. Below 1.0 because one uploader
#: may log a run a substitute joined late.
DEFAULT_ROSTER_OVERLAP = 0.6


@dataclass
class RunFingerprint:
    run_id: str
    dungeon_key: str | None
    keystone_level: int | None
    abs_start_ms: int
    duration_ms: int
    roster: frozenset[str]
    affixes: str | None
    report_code: str

    @classmethod
    def from_row(cls, row: Any, roster: Iterable[str]) -> RunFingerprint:
        return cls(
            run_id=row["run_id"],
            dungeon_key=row["dungeon_key"],
            keystone_level=row["keystone_level"],
            abs_start_ms=int(row["abs_start_ms"] or 0),
            duration_ms=int(row["duration_ms"] or 0),
            roster=frozenset(roster),
            affixes=row["keystone_affixes"],
            report_code=row["report_code"],
        )


@dataclass
class DuplicateMatch:
    left: str
    right: str
    confidence: float
    reasons: list[str] = field(default_factory=list)


def roster_overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Share of the smaller roster present in both."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def compare(
    a: RunFingerprint,
    b: RunFingerprint,
    *,
    start_tolerance_ms: int = DEFAULT_START_TOLERANCE_MS,
    duration_tolerance: float = DEFAULT_DURATION_TOLERANCE,
    roster_threshold: float = DEFAULT_ROSTER_OVERLAP,
) -> DuplicateMatch | None:
    """Decide whether two runs are probably the same real run.

    Returns None when they are not, rather than a low-confidence match: a
    "maybe" that nothing acts on is just noise in the diagnostics.
    """
    if a.run_id == b.run_id or a.report_code == b.report_code:
        # Two fights in one report are two runs, never two uploads of one.
        return None

    reasons: list[str] = []
    score = 0.0

    if a.dungeon_key != b.dungeon_key or a.dungeon_key is None:
        return None
    if a.keystone_level != b.keystone_level:
        return None
    reasons.append(f"same dungeon ({a.dungeon_key}) and key level (+{a.keystone_level})")
    score += 0.2

    start_gap = abs(a.abs_start_ms - b.abs_start_ms)
    if start_gap > start_tolerance_ms:
        # Same group, same key, different sitting: a real second observation.
        return None
    reasons.append(f"start times {start_gap / 1000:.0f}s apart")
    score += 0.3 * (1 - start_gap / start_tolerance_ms)

    longest = max(a.duration_ms, b.duration_ms) or 1
    duration_delta = abs(a.duration_ms - b.duration_ms) / longest
    if duration_delta > duration_tolerance:
        reasons.append(f"durations differ by {duration_delta:.0%}")
    else:
        reasons.append(f"durations within {duration_delta:.1%}")
        score += 0.2

    overlap = roster_overlap(a.roster, b.roster)
    if a.roster and b.roster:
        if overlap < roster_threshold:
            # The strongest disagreement available: different people played.
            return None
        reasons.append(f"roster overlap {overlap:.0%}")
        score += 0.3 * overlap
    else:
        reasons.append("roster unavailable on at least one side")

    if a.affixes and b.affixes and a.affixes == b.affixes:
        reasons.append("identical affixes")
        score += 0.1

    confidence = min(1.0, round(score, 3))
    if confidence < 0.5:
        return None
    return DuplicateMatch(left=a.run_id, right=b.run_id, confidence=confidence, reasons=reasons)


def load_fingerprints(db: Database) -> list[RunFingerprint]:
    rosters: dict[str, set[str]] = {}
    for row in db.query("SELECT run_id, player_id FROM run_players"):
        rosters.setdefault(row["run_id"], set()).add(row["player_id"])
    return [
        RunFingerprint.from_row(row, rosters.get(row["run_id"], set()))
        for row in db.query(
            "SELECT run_id, report_code, dungeon_key, keystone_level, abs_start_ms, "
            "       duration_ms, keystone_affixes FROM dungeon_runs"
        )
    ]


def find_duplicates(db: Database, **kwargs: Any) -> list[DuplicateMatch]:
    """All probable duplicate pairs in the corpus."""
    fingerprints = load_fingerprints(db)
    matches: list[DuplicateMatch] = []
    for index, left in enumerate(fingerprints):
        for right in fingerprints[index + 1 :]:
            match = compare(left, right, **kwargs)
            if match is not None:
                matches.append(match)
    return matches


def group_duplicates(db: Database, **kwargs: Any) -> dict[str, list[str]]:
    """Assign duplicate-group IDs and mark one member of each group canonical.

    Grouping is transitive (union-find): if A matches B and B matches C, all
    three are one group even when A and C were never compared directly.

    **Nothing is deleted.** `is_canonical` marks one row per group for analyses
    that need a single observation; the others stay queryable, and an analysis
    that disagrees with the choice can make its own.
    """
    matches = find_duplicates(db, **kwargs)
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for match in matches:
        union(match.left, match.right)

    groups: dict[str, list[str]] = {}
    for run_id in parent:
        groups.setdefault(find(run_id), []).append(run_id)

    with db.transaction():
        # Clear previous grouping so a re-run is authoritative, not additive.
        db.execute("UPDATE dungeon_runs SET duplicate_group_id = NULL, is_canonical = NULL")
        for root, members in groups.items():
            members.sort()
            group_id = f"dup-{root}"
            for run_id in members:
                db.execute(
                    "UPDATE dungeon_runs SET duplicate_group_id = ?, is_canonical = ? "
                    "WHERE run_id = ?",
                    (group_id, 1 if run_id == members[0] else 0, run_id),
                )
        # A run in no group is its own canonical observation.
        db.execute("UPDATE dungeon_runs SET is_canonical = 1 WHERE duplicate_group_id IS NULL")

    for match in matches:
        db.diagnostic(
            "duplicate_candidate",
            f"{match.left} and {match.right} are probably the same run "
            f"(confidence {match.confidence:.2f}): {'; '.join(match.reasons)}. "
            "Both retained.",
            run_id=match.left,
            severity="info",
        )
    db.conn.commit()
    return {root: members for root, members in groups.items() if len(members) > 1}
