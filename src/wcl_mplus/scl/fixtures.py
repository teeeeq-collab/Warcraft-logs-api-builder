"""Selecting real pulls to benchmark against.

Real data, never synthetic: the compression hypothesis is about the redundancy
structure of actual combat, and synthetic events do not have it. A format tuned
against generated data would be tuned against the generator.

Fixtures are grouped by **stream coverage**. A pull from a `mechanics_research`
run contains no Healing, so comparing a format measured on it against one
measured on a `forensic_full` pull compares coverage, not format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..repository import DedupePolicy, Repository
from .model import TimelineEvent


@dataclass
class Fixture:
    """One pull, its events, and everything needed to name what it is."""

    pull_id: str
    run_id: str
    dungeon_key: str | None
    key_level: int | None
    is_boss: bool
    duration_ms: int
    events: list[TimelineEvent] = field(default_factory=list)
    player_roles: dict[int, str] = field(default_factory=dict)
    ability_names: dict[int, str] = field(default_factory=dict)
    actor_names: dict[int, str] = field(default_factory=dict)
    streams: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.dungeon_key or '?'}/{self.pull_id}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pull_id": self.pull_id,
            "run_id": self.run_id,
            "dungeon_key": self.dungeon_key,
            "key_level": self.key_level,
            "is_boss": self.is_boss,
            "duration_ms": self.duration_ms,
            "events": len(self.events),
            "streams": list(self.streams),
        }


def select(
    repository: Repository,
    *,
    dungeon_key: str | None = None,
    limit: int = 30,
    min_events: int = 20,
) -> list[Fixture]:
    """Pick pulls spanning the case list in the experiment plan.

    Browsing policy: these are examples to encode, not a statistic. Nothing
    measured here is a claim about the population.
    """
    run_set = repository.runs(DedupePolicy.PERMISSIVE, dungeon_key=dungeon_key)
    if not run_set.run_ids:
        return []

    placeholders = ",".join("?" for _ in run_set.run_ids)
    pulls = repository.db.query(
        "SELECT p.pull_id, p.run_id, p.dungeon_key, p.is_boss, p.duration_ms, "
        "       r.keystone_level, COUNT(e.event_id) AS events "
        "  FROM pulls p "
        "  JOIN dungeon_runs r ON r.run_id = p.run_id "
        "  LEFT JOIN events e ON e.pull_id = p.pull_id "
        f" WHERE p.run_id IN ({placeholders}) "
        " GROUP BY p.pull_id HAVING events >= ? "
        # Boss pulls first, then the densest trash: the case list wants both a
        # high-density period and an ordinary one, and this orders for variety
        # rather than taking the first N pulls of one run.
        " ORDER BY p.is_boss DESC, events DESC LIMIT ?",
        (*run_set.run_ids, min_events, limit),
    )

    fixtures: list[Fixture] = []
    for pull in pulls:
        events = [
            TimelineEvent.from_row(dict(row))
            for row in repository.db.query(
                "SELECT rel_ms, type, source_id, source_instance, target_id, "
                "       target_instance, ability_game_id, amount "
                "  FROM events WHERE pull_id = ? ORDER BY rel_ms, event_id",
                (pull["pull_id"],),
            )
        ]
        if not events:
            continue
        fixtures.append(
            Fixture(
                pull_id=str(pull["pull_id"]),
                run_id=str(pull["run_id"]),
                dungeon_key=pull["dungeon_key"],
                key_level=pull["keystone_level"],
                is_boss=bool(pull["is_boss"]),
                duration_ms=int(pull["duration_ms"] or 0),
                events=events,
                player_roles=_roles(repository, str(pull["run_id"])),
                ability_names=_ability_names(repository, events),
                actor_names=_actor_names(repository, str(pull["run_id"])),
                streams=_streams(repository, str(pull["run_id"])),
            )
        )
    return fixtures


def group_by_coverage(fixtures: list[Fixture]) -> dict[tuple[str, ...], list[Fixture]]:
    """Group fixtures by the streams their run collected.

    Formats are only compared within a group. Across groups the comparison
    measures which streams were requested, which is not a property of any
    encoding.
    """
    grouped: dict[tuple[str, ...], list[Fixture]] = {}
    for fixture in fixtures:
        grouped.setdefault(fixture.streams, []).append(fixture)
    return grouped


def _roles(repository: Repository, run_id: str) -> dict[int, str]:
    return {
        int(r["actor_id"]): str(r["role"] or "dps")
        for r in repository.db.query(
            "SELECT actor_id, role FROM run_players WHERE run_id = ?", (run_id,)
        )
    }


def _actor_names(repository: Repository, run_id: str) -> dict[int, str]:
    report_code = repository.db.scalar(
        "SELECT report_code FROM dungeon_runs WHERE run_id = ?", (run_id,)
    )
    return {
        int(r["actor_id"]): str(r["name"] or f"actor-{r['actor_id']}")
        for r in repository.db.query(
            "SELECT actor_id, name FROM actors WHERE report_code = ?", (report_code,)
        )
    }


def _ability_names(repository: Repository, events: list[TimelineEvent]) -> dict[int, str]:
    ids = sorted({e.ability_game_id for e in events if e.ability_game_id is not None})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    return {
        int(r["game_id"]): str(r["name"] or f"ability-{r['game_id']}")
        for r in repository.db.query(
            f"SELECT game_id, name FROM abilities WHERE game_id IN ({placeholders})",
            tuple(ids),
        )
    }


def _streams(repository: Repository, run_id: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(r["data_type"]) + (f"@{r['hostility']}" if r["hostility"] else "")
            for r in repository.db.query(
                "SELECT data_type, hostility FROM run_stream_coverage "
                " WHERE run_id = ? AND status = 'ok'",
                (run_id,),
            )
        )
    )
