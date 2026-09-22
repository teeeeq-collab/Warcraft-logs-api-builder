"""The query boundary: every analytical read of the corpus goes through here.

Three rules are enforced by construction rather than by remembering them.

**Dedupe policy is a required argument on research reads.** Not a default. A
default of `permissive` silently hands a statistics path the lax rule; a default
of `strict` fails an interactive query for no reason. Neither should be acquired
by accident, so the caller states which kind of question it is asking. Browsing
reads may default, because nothing downstream of them is published.

**A stream is checked before it is read.** `run_stream_coverage` distinguishes
"asked, none" from "never asked", and a statistic that ignores it is wrong in
the direction of "this never happens" -- the most believable kind of wrong. A
read that needs a stream names it, and runs that never requested it are excluded
and counted rather than silently contributing a zero.

**Evidence depth travels with every result.** Not one `N`. Events, states,
pulls, runs, players, reports and parties, plus which unit is independent for
the claim and how concentrated the observations are. `N=100,000` from ten
players is a lie of composition rather than of arithmetic, and the only defence
is to report the composition.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .db import Database
from .redaction import RedactedError

logger = logging.getLogger(__name__)


class RepositoryError(RedactedError):
    """A query was refused, or asked for something the corpus cannot support."""


class DedupePolicy(StrEnum):
    """How a read treats runs whose duplicate status was never established."""

    #: `is_canonical IS NULL` is allowed through and reported. For browsing,
    #: retrieval and debugging, where nothing is published.
    PERMISSIVE = "permissive"

    #: Every included run must carry a dedupe classification. Unclassified runs
    #: are excluded and the result is marked provisional; publication-quality
    #: output refuses a provisional result outright.
    STRICT = "strict"


class IndependentUnit(StrEnum):
    """Which unit is independent for a claim.

    The count that belongs beside a statistic is the count of *this*, not the
    number of rows that happened to contribute. Enemy-behaviour claims are cheap
    -- hundreds of NPC instances per run. Player-behaviour claims are expensive
    -- five people per run, often the same people across a report.
    """

    OBSERVATION = "observation"
    CAST = "cast"
    NPC_INSTANCE = "npc_instance"
    PULL = "pull"
    RUN = "run"
    PLAYER = "player"
    PARTY = "party"


@dataclass
class Exclusion:
    run_id: str
    reason: str
    detail: str | None = None


@dataclass
class RunSet:
    """A set of runs, with everything that was left out and why."""

    run_ids: list[str]
    policy: DedupePolicy
    exclusions: list[Exclusion] = field(default_factory=list)
    #: True when the policy allowed runs whose duplicate status is unknown.
    provisional: bool = False
    required_streams: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.run_ids)

    def excluded_for(self, reason: str) -> list[str]:
        return [e.run_id for e in self.exclusions if e.reason == reason]

    def as_dict(self) -> dict[str, Any]:
        by_reason: dict[str, int] = {}
        for exclusion in self.exclusions:
            by_reason[exclusion.reason] = by_reason.get(exclusion.reason, 0) + 1
        return {
            "runs": len(self.run_ids),
            "dedupe_policy": self.policy.value,
            "provisional": self.provisional,
            "required_streams": list(self.required_streams),
            "excluded": by_reason,
        }


@dataclass
class Evidence:
    """Evidence depth for one statistic. Never a single N."""

    n_events: int = 0
    n_states: int = 0
    n_pulls: int = 0
    n_runs: int = 0
    n_players: int = 0
    n_reports: int = 0
    n_parties: int = 0
    independent_unit: str = IndependentUnit.OBSERVATION.value
    n_independent: int = 0
    top_player_share: float | None = None
    top_run_share: float | None = None
    required_streams: list[str] = field(default_factory=list)
    runs_excluded: int = 0
    dedupe_policy: str = DedupePolicy.PERMISSIVE.value
    provisional: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_events": self.n_events,
            "n_states": self.n_states,
            "n_pulls": self.n_pulls,
            "n_runs": self.n_runs,
            "n_players": self.n_players,
            "n_reports": self.n_reports,
            "n_parties": self.n_parties,
            "independent_unit": self.independent_unit,
            "n_independent": self.n_independent,
            "concentration": {
                "top_player_share": self.top_player_share,
                "top_run_share": self.top_run_share,
            },
            "coverage": {
                "required": list(self.required_streams),
                "runs_excluded": self.runs_excluded,
            },
            "dedupe_policy": self.dedupe_policy,
            "provisional": self.provisional,
        }


def parse_stream(spec: str) -> tuple[str, str | None]:
    """`"Casts@Enemies"` -> `("Casts", "Enemies")`; `"Deaths"` -> `("Deaths", None)`."""
    data_type, _, hostility = spec.partition("@")
    return data_type, hostility or None


class Repository:
    """Coverage- and dedupe-aware reads over the ingest store.

    Reads only. Nothing here writes to collection truth -- derived rows belong
    in the analytical store.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- run selection ----------------------------------------------------

    def runs(
        self,
        policy: DedupePolicy,
        *,
        dungeon_key: str | None = None,
        key_bracket: str | None = None,
        hotfix_epoch: str | None = None,
        require_streams: list[str] | None = None,
        run_ids: list[str] | None = None,
    ) -> RunSet:
        """Select runs for an analysis.

        `policy` is required. The whole point of this boundary is that a caller
        cannot get a dedupe rule by accident.
        """
        if not isinstance(policy, DedupePolicy):
            raise RepositoryError(
                "A dedupe policy is required: DedupePolicy.PERMISSIVE for browsing "
                "and retrieval, DedupePolicy.STRICT for anything that becomes a "
                "statistic. There is deliberately no default."
            )

        where = ["1 = 1"]
        params: list[Any] = []
        if dungeon_key is not None:
            where.append("dungeon_key = ?")
            params.append(dungeon_key)
        if key_bracket is not None:
            where.append("key_bracket = ?")
            params.append(key_bracket)
        if hotfix_epoch is not None:
            where.append("hotfix_epoch = ?")
            params.append(hotfix_epoch)
        if run_ids is not None:
            if not run_ids:
                return RunSet([], policy, required_streams=require_streams or [])
            where.append(f"run_id IN ({','.join('?' for _ in run_ids)})")
            params.extend(run_ids)

        rows = self.db.query(
            f"SELECT run_id, is_canonical, duplicate_group_id FROM dungeon_runs "
            f" WHERE {' AND '.join(where)} ORDER BY run_id",
            tuple(params),
        )

        selected: list[str] = []
        exclusions: list[Exclusion] = []
        provisional = False

        for row in rows:
            run_id = str(row["run_id"])
            canonical = row["is_canonical"]
            if canonical is None:
                # Never deduplicated. Under STRICT this run cannot contribute to
                # a statistic, because two uploads of one real run would count
                # twice and the count is the claim.
                if policy is DedupePolicy.STRICT:
                    exclusions.append(
                        Exclusion(run_id, "unclassified", "dedupe has not been run for this run")
                    )
                    continue
                provisional = True
                selected.append(run_id)
                continue
            if int(canonical) == 0:
                exclusions.append(
                    Exclusion(run_id, "duplicate", f"group {row['duplicate_group_id']}")
                )
                continue
            selected.append(run_id)

        run_set = RunSet(
            run_ids=selected,
            policy=policy,
            exclusions=exclusions,
            provisional=provisional,
            required_streams=list(require_streams or []),
        )
        if require_streams:
            run_set = self.require_streams(run_set, require_streams)
        return run_set

    def require_streams(self, run_set: RunSet, streams: list[str]) -> RunSet:
        """Drop runs that never requested one of these streams.

        "Never asked" and "asked, got nothing" are different facts, and only the
        second licenses reading a zero as a zero. A run that never requested
        Healing contributes no evidence about healing -- including no zero.
        """
        kept: list[str] = []
        exclusions = list(run_set.exclusions)
        for run_id in run_set.run_ids:
            missing = [s for s in streams if not self._has_stream(run_id, s)]
            if missing:
                exclusions.append(Exclusion(run_id, "missing_stream", ", ".join(sorted(missing))))
                continue
            kept.append(run_id)
        return RunSet(
            run_ids=kept,
            policy=run_set.policy,
            exclusions=exclusions,
            provisional=run_set.provisional,
            required_streams=sorted({*run_set.required_streams, *streams}),
        )

    def _has_stream(self, run_id: str, spec: str) -> bool:
        """True when this run asked for the stream and the ask completed.

        `status = 'ok'` is what makes a zero trustworthy: it means the stream
        paginated to exhaustion. A `partial` stream's zero is an artefact of
        stopping early, and a `failed` stream's is an artefact of the API.
        """
        data_type, hostility = parse_stream(spec)
        sql = (
            "SELECT 1 FROM run_stream_coverage "
            " WHERE run_id = ? AND data_type = ? AND status = 'ok' "
        )
        params: list[Any] = [run_id, data_type]
        if hostility is not None:
            sql += " AND hostility = ? "
            params.append(hostility)
        return self.db.execute(sql + " LIMIT 1", tuple(params)).fetchone() is not None

    def coverage_report(self, run_set: RunSet, streams: list[str]) -> dict[str, dict[str, int]]:
        """Per-stream: how many of these runs asked, and how many completed."""
        out: dict[str, dict[str, int]] = {}
        for spec in streams:
            asked = sum(1 for r in run_set.run_ids if self._has_stream(r, spec))
            out[spec] = {"runs": len(run_set), "with_stream": asked}
        return out

    # -- events -----------------------------------------------------------

    def iter_events(
        self,
        run_set: RunSet,
        *,
        data_type: str | None = None,
        hostility: str | None = None,
        event_type: str | None = None,
        ability_game_id: int | None = None,
        pull_id: str | None = None,
        batch_size: int = 5_000,
    ) -> Iterator[dict[str, Any]]:
        """Stream events, never materialising them.

        A corpus is expected to reach hundreds of millions of rows, so a read
        that returns a list is a read that eventually fails on a machine the
        operator already owns.
        """
        if not run_set.run_ids:
            return
        for chunk in _chunks(run_set.run_ids, 400):
            where = [f"run_id IN ({','.join('?' for _ in chunk)})"]
            params: list[Any] = list(chunk)
            for column, value in (
                ("data_type", data_type),
                ("hostility", hostility),
                ("type", event_type),
                ("ability_game_id", ability_game_id),
                ("pull_id", pull_id),
            ):
                if value is not None:
                    where.append(f"{column} = ?")
                    params.append(value)
            cursor = self.db.execute(
                f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY run_id, rel_ms",
                tuple(params),
            )
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    yield dict(row)

    # -- evidence ---------------------------------------------------------

    def evidence(
        self,
        run_set: RunSet,
        *,
        independent_unit: IndependentUnit = IndependentUnit.OBSERVATION,
        n_events: int | None = None,
        n_states: int = 0,
        event_filter: dict[str, Any] | None = None,
    ) -> Evidence:
        """Evidence depth for a claim over this run set.

        `independent_unit` is the caller's declaration of what one independent
        observation is for the claim being made. `n_independent` is the count of
        that unit -- the number any interpretation should use.
        """
        if not run_set.run_ids:
            return Evidence(
                independent_unit=independent_unit.value,
                required_streams=list(run_set.required_streams),
                runs_excluded=len(run_set.exclusions),
                dedupe_policy=run_set.policy.value,
                provisional=run_set.provisional,
            )

        placeholders = ",".join("?" for _ in run_set.run_ids)
        params = tuple(run_set.run_ids)

        counted_events = n_events
        if counted_events is None:
            where = [f"run_id IN ({placeholders})"]
            ev_params: list[Any] = list(run_set.run_ids)
            for column, value in (event_filter or {}).items():
                where.append(f"{column} = ?")
                ev_params.append(value)
            counted_events = int(
                self.db.scalar(
                    f"SELECT COUNT(*) FROM events WHERE {' AND '.join(where)}", tuple(ev_params)
                )
                or 0
            )

        n_pulls = int(
            self.db.scalar(f"SELECT COUNT(*) FROM pulls WHERE run_id IN ({placeholders})", params)
            or 0
        )
        n_reports = int(
            self.db.scalar(
                f"SELECT COUNT(DISTINCT report_code) FROM dungeon_runs "
                f" WHERE run_id IN ({placeholders})",
                params,
            )
            or 0
        )
        n_players = int(
            self.db.scalar(
                f"SELECT COUNT(DISTINCT player_id) FROM run_players "
                f" WHERE run_id IN ({placeholders})",
                params,
            )
            or 0
        )
        # A party is a roster, not a run: five people who run six keys together
        # are one party, and treating them as six is the pseudoreplication this
        # block exists to make visible.
        n_parties = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM (SELECT run_id, GROUP_CONCAT(player_id) AS roster "
                f"  FROM (SELECT run_id, player_id FROM run_players "
                f"         WHERE run_id IN ({placeholders}) ORDER BY run_id, player_id) "
                "   GROUP BY run_id) GROUP BY roster",
                params,
            )
            or 0
        )

        evidence = Evidence(
            n_events=counted_events,
            n_states=n_states,
            n_pulls=n_pulls,
            n_runs=len(run_set),
            n_players=n_players,
            n_reports=n_reports,
            n_parties=n_parties,
            independent_unit=independent_unit.value,
            required_streams=list(run_set.required_streams),
            runs_excluded=len(run_set.exclusions),
            dedupe_policy=run_set.policy.value,
            provisional=run_set.provisional,
        )
        evidence.n_independent = self._independent_count(run_set, independent_unit, evidence)
        self._concentration(run_set, evidence, event_filter)
        return evidence

    def _independent_count(self, run_set: RunSet, unit: IndependentUnit, evidence: Evidence) -> int:
        if unit is IndependentUnit.PLAYER:
            return evidence.n_players
        if unit is IndependentUnit.PARTY:
            return evidence.n_parties
        if unit is IndependentUnit.RUN:
            return evidence.n_runs
        if unit is IndependentUnit.PULL:
            return evidence.n_pulls
        if unit is IndependentUnit.NPC_INSTANCE:
            placeholders = ",".join("?" for _ in run_set.run_ids)
            return int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM (SELECT DISTINCT run_id, source_id, source_instance "
                    f"  FROM events WHERE run_id IN ({placeholders}) "
                    "     AND hostility = 'Enemies' AND source_instance IS NOT NULL)",
                    tuple(run_set.run_ids),
                )
                or 0
            )
        return evidence.n_events

    def _concentration(
        self, run_set: RunSet, evidence: Evidence, event_filter: dict[str, Any] | None
    ) -> None:
        """Largest share contributed by any one run, and by any one player.

        A distribution where one player supplies most of the observations is not
        a population distribution, however large the event count.
        """
        if not run_set.run_ids or evidence.n_events == 0:
            return
        placeholders = ",".join("?" for _ in run_set.run_ids)
        where = [f"run_id IN ({placeholders})"]
        params: list[Any] = list(run_set.run_ids)
        for column, value in (event_filter or {}).items():
            where.append(f"{column} = ?")
            params.append(value)

        top_run = self.db.scalar(
            f"SELECT COUNT(*) AS n FROM events WHERE {' AND '.join(where)} "
            " GROUP BY run_id ORDER BY n DESC LIMIT 1",
            tuple(params),
        )
        if top_run:
            evidence.top_run_share = round(int(top_run) / evidence.n_events, 4)

        if evidence.n_players:
            # Attribution is per run, not per event: an event's actor is a
            # report-scoped id, while a player is stable across reports.
            top_player = self.db.scalar(
                "SELECT COUNT(*) AS n FROM run_players "
                f" WHERE run_id IN ({placeholders}) GROUP BY player_id ORDER BY n DESC LIMIT 1",
                tuple(run_set.run_ids),
            )
            if top_player:
                evidence.top_player_share = round(int(top_player) / len(run_set), 4)

    # -- packs (moved out of the CLI) -------------------------------------

    def packs(
        self,
        *,
        policy: DedupePolicy = DedupePolicy.PERMISSIVE,
        dungeon_key: str | None = None,
        exact: bool = False,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Recurring packs across every collected log.

        Retrieval, not analysis: it says where a pack is and how often it
        occurred, nothing about what any of it means. Hence a browsing default
        for the policy -- no number here is published.
        """
        column = "composition_signature" if exact else "species_signature"
        run_set = self.runs(policy, dungeon_key=dungeon_key)
        if not run_set.run_ids:
            return []
        placeholders = ",".join("?" for _ in run_set.run_ids)
        rows = self.db.query(
            f"SELECT dungeon_key, {column} AS sig, COUNT(*) AS occurrences, "
            "       COUNT(DISTINCT run_id) AS runs, "
            "       ROUND(AVG(duration_ms) / 1000.0, 1) AS mean_s, "
            "       MAX(is_boss) AS boss, MIN(name) AS a_name "
            f"  FROM pulls WHERE run_id IN ({placeholders}) "
            f"   AND {column} IS NOT NULL AND {column} != '' "
            f" GROUP BY dungeon_key, {column} "
            " ORDER BY occurrences DESC LIMIT ?",
            (*run_set.run_ids, limit),
        )
        return [dict(r) for r in rows]

    def npc_pulls(
        self,
        npc_game_id: int,
        *,
        policy: DedupePolicy = DedupePolicy.PERMISSIVE,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Every collected pull containing one NPC species."""
        run_set = self.runs(policy)
        if not run_set.run_ids:
            return []
        placeholders = ",".join("?" for _ in run_set.run_ids)
        rows = self.db.query(
            "SELECT p.dungeon_key, p.pull_id, p.name, p.is_boss, p.duration_ms, "
            "       n.instance_count, n.instance_count_confidence "
            "  FROM pull_npcs n JOIN pulls p ON p.pull_id = n.pull_id "
            f" WHERE n.npc_game_id = ? AND p.run_id IN ({placeholders}) "
            " ORDER BY p.dungeon_key, p.pull_id LIMIT ?",
            (npc_game_id, *run_set.run_ids, limit),
        )
        return [dict(r) for r in rows]


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    """SQLite caps bound parameters, so a large run set is read in slices."""
    for start in range(0, len(items), size):
        yield items[start : start + size]
