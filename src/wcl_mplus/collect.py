"""Collection: report -> run -> pulls -> NPC instances -> events -> database.

Two properties this module exists to guarantee:

* **Resume is exact.** Pagination checkpoints live in the `event_pages` table,
  not in a side file. A page is written only after its events are written, in
  one transaction, so an interrupted collection resumes from the last page that
  actually landed. There is no state that can disagree with the data.

* **Re-ingest is idempotent.** Running the same job twice changes no row
  counts. Runs and pulls are replaced by primary key; a re-fetched event page
  replaces its own events rather than appending beside them.

Event fetching is split by hostility where it matters. That is a finding, not
a tuning choice: an unfiltered cast sample came back as fifty player casts and
zero NPC casts, which would have made NPC mechanic timelines invisible.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .client import ApiError, GraphQLClient
from .configs import ProjectConfig, SamplingConfig
from .db import Database, json_or_none
from .normalize import (
    is_mythic_plus,
    normalize_abilities,
    normalize_actors,
    normalize_event,
    normalize_pulls,
    normalize_report,
    normalize_run,
    normalize_run_players,
    run_id_for,
    unexpected_event_fields,
)
from .paginate import EventPaginator, PageResult
from .pullassign import PullAssigner
from .querybuild import (
    WANTED_FIGHT_FIELDS,
    WANTED_PULL_FIELDS,
    WANTED_PULL_NPC_FIELDS,
    WANTED_REPORT_FIELDS,
    load_query,
    render,
)
from .redaction import RedactedError
from .reportsource import ReportCandidate
from .sanitize import IDENTITY_SCHEME_VERSION, player_name_candidates, salt_fingerprint
from .schema import SchemaIntrospector
from .version import (
    NORMALIZER_VERSION,
    QUERY_VERSION,
    SCHEMA_VERSION,
    SOFTWARE_VERSION,
    provenance,
)

logger = logging.getLogger(__name__)

#: Events requested per page. The documented ceiling is 10,000 and untested;
#: this is deliberately below it, and page size is an ingestion detail that
#: cannot affect results because pagination is lossless either way.
EVENTS_PAGE_LIMIT = 2000


class CollectionError(RedactedError):
    """Collection failed for a reason worth stopping on."""


@dataclass
class EventRequest:
    """One event stream to fetch: a category, optionally narrowed.

    `source_id` / `target_id` restrict the stream to a single actor. A narrowed
    stream answers a different question from the same stream unnarrowed, so the
    narrowing travels with the request into `run_stream_coverage` rather than
    being applied and forgotten.
    """

    data_type: str
    hostility: str | None = None
    source_id: int | None = None
    target_id: int | None = None
    #: "all" or "focus" -- why this stream was requested, not just how.
    scope: str = "all"

    @property
    def label(self) -> str:
        base = f"{self.data_type}@{self.hostility}" if self.hostility else self.data_type
        if self.source_id is not None:
            base += f"[source={self.source_id}]"
        if self.target_id is not None:
            base += f"[target={self.target_id}]"
        return base

    @property
    def stream_key(self) -> str:
        """Identity of the stream itself, without the actor narrowing."""
        return f"{self.data_type}@{self.hostility}" if self.hostility else self.data_type


@dataclass
class RunOutcome:
    """What happened to one run."""

    run_id: str
    status: str
    events_written: int = 0
    pages_fetched: int = 0
    pulls: int = 0
    assignment: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class FocusResolution:
    """Whether a requested focus player was ever found, and where.

    A focus player legitimately misses individual runs -- nobody is in every
    report on a list -- so per-run absence is not an error. Never being found
    in *any* run is different in kind: it means the name was wrong, or the
    corpus was written under a different identity scheme, and every focus
    stream the profile promised was silently skipped. The two must not read
    alike, so they are counted separately (brief section 3.1).
    """

    requested: str | None = None
    runs_seen: int = 0
    runs_resolved: int = 0
    #: Pseudonyms tried on the most recent miss, for the operator's diagnosis.
    last_missed: list[str] = field(default_factory=list)

    @property
    def unresolved(self) -> bool:
        """True when a focus player was asked for and never matched anything."""
        return bool(self.requested) and self.runs_seen > 0 and self.runs_resolved == 0

    def summary(self) -> dict[str, Any]:
        return {
            "requested": bool(self.requested),
            "runs_seen": self.runs_seen,
            "runs_resolved": self.runs_resolved,
            "unresolved": self.unresolved,
        }


@dataclass
class CollectionResult:
    job_id: str
    reports_attempted: int = 0
    reports_completed: int = 0
    reports_failed: int = 0
    runs: list[RunOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    focus: FocusResolution = field(default_factory=FocusResolution)

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "reports_attempted": self.reports_attempted,
            "reports_completed": self.reports_completed,
            "reports_failed": self.reports_failed,
            "runs": len(self.runs),
            "runs_complete": sum(1 for r in self.runs if r.status == "complete"),
            "events_written": sum(r.events_written for r in self.runs),
            "pages_fetched": sum(r.pages_fetched for r in self.runs),
            "focus": self.focus.summary(),
            "errors": self.errors[:20],
        }


class Collector:
    """Fetches reports and writes them into the database."""

    def __init__(
        self,
        client: GraphQLClient,
        db: Database,
        config: ProjectConfig,
        *,
        job_id: str | None = None,
        page_limit: int = EVENTS_PAGE_LIMIT,
    ) -> None:
        self.client = client
        self.db = db
        self.config = config
        self.introspector = SchemaIntrospector(client)
        self.job_id = job_id or uuid.uuid4().hex[:16]
        self.page_limit = page_limit
        self._selections: dict[str, str] = {}
        #: Focus-player accounting for the current job. Lives on the collector
        #: rather than in `_collect_run` because the question it answers --
        #: "was this player ever found?" -- is only answerable across runs.
        self.focus = FocusResolution()

    # -- schema-safe selections -------------------------------------------

    def _selection(self, type_name: str, wanted: list[str]) -> str:
        """Introspection-derived selection set, cached per type."""
        if type_name not in self._selections:
            present = self.introspector.present_fields(type_name, wanted)
            if not present:
                raise CollectionError(
                    f"The live schema exposes none of the wanted {type_name} fields. "
                    "Run `wclmplus schema-check` before collecting."
                )
            selection = self.introspector.build_selection(type_name, present)
            for warning in selection.warnings:
                self.db.diagnostic("selection", warning, job_id=self.job_id, severity="info")
            self._selections[type_name] = selection.text
        return self._selections[type_name]

    # -- job lifecycle ----------------------------------------------------

    def assert_identity_scheme(self) -> None:
        """Refuse to write pseudonyms into a corpus built under another scheme.

        Player pseudonyms are the only handle the corpus has on a person, so
        two schemes in one database do not merge -- they silently stop matching,
        and the same player reads as two. The first job to touch a database
        claims it for the scheme in force; a later job under a different one
        stops here instead of appending names nothing will ever join to
        (brief section 4).
        """
        fingerprint = salt_fingerprint()
        row = self.db.execute(
            "SELECT scheme_version, salt_fingerprint FROM corpus_identity LIMIT 1"
        ).fetchone()
        if row is None:
            self.db.upsert(
                "corpus_identity",
                {
                    "scheme_version": IDENTITY_SCHEME_VERSION,
                    "salt_fingerprint": fingerprint,
                    "first_written_at": time.time(),
                    "software_version": SOFTWARE_VERSION,
                    "notes": None,
                },
                replace=False,
            )
            self.db.conn.commit()
            return
        if (
            int(row["scheme_version"]) != IDENTITY_SCHEME_VERSION
            or str(row["salt_fingerprint"]) != fingerprint
        ):
            raise CollectionError(
                f"This database was written under identity scheme "
                f"v{row['scheme_version']} (salt {row['salt_fingerprint']}), but this "
                f"code uses v{IDENTITY_SCHEME_VERSION} (salt {fingerprint}). Player "
                "pseudonyms from the two schemes will never match each other. Collect "
                "into a separate database, or re-derive the existing one from the raw "
                "cache under the current scheme."
            )

    def start_job(self, *, sample_profile: str | None, event_profile: str) -> None:
        self.assert_identity_scheme()
        prov = provenance()
        self.db.upsert(
            "collection_jobs",
            {
                "job_id": self.job_id,
                "started_at": time.time(),
                "finished_at": None,
                "status": "running",
                "sample_profile": sample_profile,
                "event_profile": event_profile,
                "config_hash": self.config.hash(),
                "software_version": SOFTWARE_VERSION,
                "normalizer_version": NORMALIZER_VERSION,
                "query_version": QUERY_VERSION,
                "schema_version": SCHEMA_VERSION,
                "identity_scheme_version": IDENTITY_SCHEME_VERSION,
                "git_commit": prov.get("git_commit"),
                "git_dirty": 1 if prov.get("git_dirty") else 0,
            },
        )
        self.db.conn.commit()

    def finish_job(self, result: CollectionResult, status: str = "complete") -> None:
        self.db.execute(
            "UPDATE collection_jobs SET finished_at = ?, status = ?, reports_attempted = ?, "
            "reports_completed = ?, reports_failed = ? WHERE job_id = ?",
            (
                time.time(),
                status,
                result.reports_attempted,
                result.reports_completed,
                result.reports_failed,
                self.job_id,
            ),
        )
        self.db.conn.commit()

    # -- event profile ----------------------------------------------------

    def event_requests(
        self, event_profile: str, *, focus_actor_id: int | None = None
    ) -> list[EventRequest]:
        """Streams this profile asks for, including any focus-player streams.

        A focus stream is only emitted when an actor was actually resolved.
        Emitting it unnarrowed instead would quietly collect five players'
        telemetry under a profile that promised one player's, which is the
        expensive mistake this whole layering exists to avoid.
        """
        profile = self.config.sampling.event_profile(event_profile)
        requests: list[EventRequest] = []
        seen: set[str] = set()
        for spec in profile.event_types:
            data_type, hostility = SamplingConfig.parse_event_type(spec)
            request = EventRequest(data_type=data_type, hostility=hostility)
            requests.append(request)
            seen.add(request.stream_key)

        # The flag has existed in every profile since the first release and was
        # never read, so no CombatantInfo was ever collected while the config
        # said otherwise. Honouring it here keeps both spellings working: the
        # flag, and naming CombatantInfo outright in event_types.
        if profile.include_combatant_info and "CombatantInfo" not in seen:
            requests.append(EventRequest(data_type="CombatantInfo"))
            seen.add("CombatantInfo")

        if focus_actor_id is not None:
            for spec in profile.focus_event_types:
                data_type, hostility = SamplingConfig.parse_event_type(spec)
                requests.append(
                    EventRequest(
                        data_type=data_type,
                        hostility=hostility,
                        source_id=focus_actor_id,
                        scope="focus",
                    )
                )
        return requests

    # -- report -----------------------------------------------------------

    def collect_report(
        self,
        candidate: ReportCandidate,
        *,
        event_profile: str = "mechanics",
        dungeon_key: str | None = None,
        max_runs: int | None = None,
        focus_player: str | None = None,
    ) -> list[RunOutcome]:
        """Ingest one report: metadata, its Mythic+ runs, and their events."""
        code = candidate.code
        logger.info("Collecting report %s", code)

        self.db.upsert(
            "report_provenance",
            {
                "report_code": code,
                "source_type": candidate.provenance.source_type,
                "seed": candidate.provenance.seed,
                "discovered_at": candidate.provenance.discovered_at,
                "rank": candidate.provenance.rank,
                "page": candidate.provenance.page,
                "job_id": self.job_id,
                "extra": json_or_none(candidate.provenance.extra),
            },
            replace=False,
        )

        report = self._fetch_report(code)
        if report is None:
            self.db.diagnostic(
                "report_unavailable",
                f"Report {code} returned no data (private, deleted, or wrong code).",
                job_id=self.job_id,
                severity="error",
            )
            self.db.conn.commit()
            return []

        report_row = normalize_report(report, retrieved_at=time.time())
        report_start_ms = int(report_row["start_time_ms"] or 0)

        if report_row.get("is_archived"):
            report_row["collection_status"] = "archived-events-unavailable"
            self.db.diagnostic(
                "archived_report",
                f"Report {code} is archived; its runs must not enter an "
                "event-frequency denominator.",
                job_id=self.job_id,
                severity="warning",
            )

        with self.db.transaction():
            self.db.upsert("reports", report_row)

        fights = self.fetch_fights(code)
        runs = [f for f in fights if is_mythic_plus(f)]
        if dungeon_key is not None:
            entry = self.config.dungeons.resolve(dungeon_key)
            wanted_encounters = set(entry.encounter_ids)
            if wanted_encounters:
                runs = [f for f in runs if f.get("encounterID") in wanted_encounters]
            else:
                runs = [
                    f
                    for f in runs
                    if str((f.get("gameZone") or {}).get("name", "")).lower()
                    == entry.display_name.lower()
                ]
        if max_runs is not None:
            runs = runs[:max_runs]

        if not runs:
            self.db.diagnostic(
                "no_matching_runs",
                f"Report {code} held no Mythic+ run matching the request "
                f"({len(fights)} fights seen).",
                job_id=self.job_id,
                severity="info",
            )
            self.db.conn.commit()
            return []

        master = self._fetch_master_data(code)
        outcomes: list[RunOutcome] = []
        for fight in runs:
            outcomes.append(
                self._collect_run(
                    fight,
                    report_code=code,
                    report_start_ms=report_start_ms,
                    master=master,
                    event_profile=event_profile,
                    archived=bool(report_row.get("is_archived")),
                    focus_player=focus_player,
                )
            )
        return outcomes

    # -- run --------------------------------------------------------------

    def _collect_run(
        self,
        fight: dict[str, Any],
        *,
        report_code: str,
        report_start_ms: int,
        master: dict[str, Any],
        event_profile: str,
        archived: bool,
        focus_player: str | None = None,
    ) -> RunOutcome:
        fight_id = int(fight.get("id") or 0)
        run_id = run_id_for(report_code, fight_id)
        outcome = RunOutcome(run_id=run_id, status="collection-failed")

        dungeon_key, wcl_zone_id = self._resolve_dungeon(fight)
        run_row = normalize_run(
            fight,
            report_code=report_code,
            report_start_ms=report_start_ms,
            hotfix_epoch=self.config.hotfixes.epoch_for(
                report_start_ms + int(fight.get("startTime") or 0)
            ),
            key_bracket=self.config.sampling.bracket_for(fight.get("keystoneLevel")),
            dungeon_key=dungeon_key,
            wcl_zone_id=wcl_zone_id,
            collection_status="metadata-only",
            job_id=self.job_id,
        )

        # A run row is replaced wholesale on re-ingest, and the normalizer has
        # no way to know a duplicate-detection pass ever ran -- it sees one
        # fight, not a corpus. Carrying the classification across the replace
        # is this layer's job: without it, every re-collect silently discards
        # `wclmplus dedupe` and the corpus goes back to counting two uploads of
        # one real run as two observations.
        prior = self.db.execute(
            "SELECT duplicate_group_id, is_canonical FROM dungeon_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if prior is not None:
            run_row["duplicate_group_id"] = prior["duplicate_group_id"]
            run_row["is_canonical"] = prior["is_canonical"]

        with self.db.transaction():
            self.db.upsert("dungeon_runs", run_row)
            self._store_master_data(master, report_code=report_code)
            self._store_roster(master, run_id=run_id, fight=fight)

        pull_rows, npc_rows = self._collect_pulls(
            report_code=report_code,
            fight_id=fight_id,
            run_id=run_id,
            report_start_ms=report_start_ms,
            run_rel_start_ms=int(run_row["rel_start_ms"]),
            dungeon_key=run_row.get("dungeon_key"),
        )
        outcome.pulls = len(pull_rows)

        if archived:
            outcome.status = "archived-events-unavailable"
            self._set_run_status(run_id, outcome.status)
            return outcome

        assigner = PullAssigner.from_rows(pull_rows)
        if assigner.overlaps:
            self.db.diagnostic(
                "overlapping_pulls",
                f"{len(assigner.overlaps)} overlapping pull interval(s) in {run_id}: "
                f"{assigner.stats.overlapping_pairs[:5]}",
                run_id=run_id,
                job_id=self.job_id,
            )

        try:
            focus_actor_id = self._resolve_focus_actor(
                focus_player, run_id=run_id, report_code=report_code
            )
            for request in self.event_requests(event_profile, focus_actor_id=focus_actor_id):
                stream_error: str | None = None
                try:
                    written, pages = self._collect_events(
                        request,
                        run_id=run_id,
                        report_code=report_code,
                        report_start_ms=report_start_ms,
                        run_rel_start_ms=int(run_row["rel_start_ms"]),
                        rel_start_ms=int(run_row["rel_start_ms"]),
                        rel_end_ms=int(run_row["rel_end_ms"]),
                        assigner=assigner,
                        pull_starts={p["pull_id"]: int(p["rel_start_ms"]) for p in pull_rows},
                    )
                    outcome.events_written += written
                    outcome.pages_fetched += pages
                finally:
                    # Written whatever happened. A stream that failed must leave
                    # a row saying so -- a missing row means "never requested",
                    # and a failure silently wearing that meaning is exactly the
                    # confusion this manifest exists to prevent.
                    self._record_coverage(
                        request,
                        run_id=run_id,
                        event_profile=event_profile,
                        rel_start_ms=int(run_row["rel_start_ms"]),
                        rel_end_ms=int(run_row["rel_end_ms"]),
                        error=stream_error,
                    )
        except ApiError as exc:
            outcome.errors.append(str(exc))
            outcome.status = "collection-failed"
            self.db.diagnostic(
                "event_collection_failed",
                str(exc),
                run_id=run_id,
                job_id=self.job_id,
                severity="error",
            )
            self._set_run_status(run_id, outcome.status)
            self.db.conn.commit()
            return outcome

        outcome.assignment = assigner.stats.summary()
        if assigner.stats.unassigned:
            self.db.diagnostic(
                "events_outside_pulls",
                f"{assigner.stats.unassigned} of {assigner.stats.total} events in {run_id} "
                f"fell outside every pull "
                f"({assigner.stats.summary()['assigned_pct']}% assigned). Retained, not dropped.",
                run_id=run_id,
                job_id=self.job_id,
                severity="info",
            )

        outcome.status = "complete"
        self._set_run_status(run_id, outcome.status)
        self.db.conn.commit()
        return outcome

    def _resolve_dungeon(self, fight: dict[str, Any]) -> tuple[str | None, int | None]:
        """Match a fight to a configured dungeon by encounter ID, then by name."""
        encounter_id = fight.get("encounterID")
        for entry in self.config.dungeons.dungeons:
            if encounter_id is not None and encounter_id in entry.encounter_ids:
                return entry.key, entry.wcl_zone_id
        zone_name = str((fight.get("gameZone") or {}).get("name") or "")
        if zone_name:
            for entry in self.config.dungeons.dungeons:
                if entry.matches(zone_name):
                    return entry.key, entry.wcl_zone_id
        return None, None

    def _set_run_status(self, run_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE dungeon_runs SET collection_status = ? WHERE run_id = ?", (status, run_id)
        )

    # -- sub-fetches ------------------------------------------------------

    def _fetch_report(self, code: str) -> dict[str, Any] | None:
        data = self.client.execute(
            render(
                "report_metadata",
                {"REPORT_FIELDS": self._selection("Report", WANTED_REPORT_FIELDS)},
            ),
            {"code": code},
            kind="report_metadata",
            report_code=code,
        )
        return ((data.get("reportData") or {}).get("report")) or None

    def fetch_fights(self, code: str) -> list[dict[str, Any]]:
        data = self.client.execute(
            render(
                "report_fights",
                {"FIGHT_FIELDS": self._selection("ReportFight", WANTED_FIGHT_FIELDS)},
            ),
            {"code": code},
            kind="report_fights",
            report_code=code,
        )
        report = ((data.get("reportData") or {}).get("report")) or {}
        return [f for f in (report.get("fights") or []) if isinstance(f, dict)]

    def _fetch_master_data(self, code: str) -> dict[str, Any]:
        data = self.client.execute(
            load_query("report_master_data"),
            {"code": code},
            kind="report_master_data",
            report_code=code,
        )
        return (((data.get("reportData") or {}).get("report")) or {}).get("masterData") or {}

    def _store_master_data(self, master: dict[str, Any], *, report_code: str) -> None:
        self.db.upsert_many("actors", normalize_actors(master, report_code=report_code))
        self.db.upsert_many("abilities", normalize_abilities(master, seen_at=time.time()))

    def _resolve_focus_actor(
        self, focus_player: str | None, *, run_id: str, report_code: str
    ) -> int | None:
        """Find the actor ID for a named focus player in this run.

        The corpus stores no clear character names -- `normalize_actors` writes
        `pseudonym(name, "player")` -- so a name typed on the command line is
        put through the same function before it is matched. Comparing the clear
        name against `actors.name` directly, as this did until now, could never
        match anything: every focus stream was skipped on every run, and the
        only trace was a diagnostic that read like an ordinary absence.

        Returns None when no focus player was asked for, and also when one was
        asked for but is not in this run's roster -- a player does not appear in
        every report on a list. The diagnostic matters: without it the run would
        silently collect only its unnarrowed streams and look, later, exactly
        like a run where the focus streams returned nothing.

        Whether an unmatched name is an absence or a mistake cannot be decided
        from one run, so that verdict is deferred to the end of the job; see
        `FocusResolution`.
        """
        if not focus_player:
            return None
        candidates = player_name_candidates(focus_player)
        self.focus.requested = focus_player
        self.focus.runs_seen += 1
        row = None
        for stored_name in candidates:
            row = self.db.execute(
                "SELECT a.actor_id FROM actors a "
                "  JOIN run_players rp ON rp.run_id = ? AND rp.actor_id = a.actor_id "
                " WHERE a.report_code = ? AND a.name = ? LIMIT 1",
                (run_id, report_code, stored_name),
            ).fetchone()
            if row is not None:
                break
        if row is None:
            # The pseudonym, never the name the user typed: a diagnostic is a
            # stored row, and putting a real character name in one would defeat
            # the pseudonymization the rest of the pipeline maintains. The
            # pseudonym is enough to check the lookup by hand.
            self.focus.last_missed = candidates
            self.db.diagnostic(
                "focus_player_absent",
                f"Focus player {candidates[0] if candidates else '?'} is not in the roster "
                f"of {run_id}; focus streams were not collected for this run.",
                run_id=run_id,
                job_id=self.job_id,
                severity="warning",
            )
            return None
        self.focus.runs_resolved += 1
        return int(row["actor_id"])

    def _record_focus_resolution(self, result: CollectionResult) -> None:
        """Fail loudly if a requested focus player matched nothing anywhere.

        Every run that was asked for a focus player and did not find one has
        already recorded its own absence. What no single run can say is that
        *none* of them found the player, which is not an absence but a failed
        request: a misspelling, a realm suffix WCL does not store, or a corpus
        written under a different identity scheme. Left implicit it produces a
        corpus that looks collected and contains no focus data at all.
        """
        result.focus = self.focus
        if not self.focus.unresolved:
            return
        candidates = player_name_candidates(self.focus.requested or "")
        message = (
            f"Focus player was requested but matched no actor in any of "
            f"{self.focus.runs_seen} run(s). Looked for pseudonym(s): "
            f"{', '.join(candidates)}. No focus stream was collected. Check the "
            "spelling against the run's roster, or pass the pseudonym itself."
        )
        result.errors.append(message)
        self.db.diagnostic(
            "focus_player_unresolved",
            message,
            job_id=self.job_id,
            severity="error",
        )
        self.db.conn.commit()

    def _record_coverage(
        self,
        request: EventRequest,
        *,
        run_id: str,
        event_profile: str,
        rel_start_ms: int,
        rel_end_ms: int,
        error: str | None,
    ) -> None:
        """Record that this stream was requested, and how it ended.

        Totals are read back from `event_pages` rather than accumulated in
        memory, so a resumed collection reports the whole stream rather than
        only the part this process fetched. `status` is 'ok' only when the
        stream paginated to exhaustion -- which is what licenses reading its
        zero as a real zero.
        """
        stream = self._stream_key(run_id, request)
        row = self.db.execute(
            "SELECT COUNT(*) AS pages, IFNULL(SUM(event_count), 0) AS events, "
            "       SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS bad "
            "  FROM event_pages "
            " WHERE run_id = ? AND data_type = ? AND IFNULL(hostility, '') = ? "
            "   AND IFNULL(source_id,-1) = ? AND IFNULL(target_id,-1) = ?",
            stream,
        ).fetchone()
        # Exhaustion is a property of the LAST page only. Every earlier page
        # carries a forward cursor by definition, so counting pages with a
        # cursor marks every multi-page stream partial.
        last = self.db.execute(
            "SELECT next_cursor_ms FROM event_pages "
            " WHERE run_id = ? AND data_type = ? AND IFNULL(hostility, '') = ? "
            "   AND IFNULL(source_id,-1) = ? AND IFNULL(target_id,-1) = ? "
            " ORDER BY page_index DESC LIMIT 1",
            stream,
        ).fetchone()

        pages = int(row["pages"] or 0)
        if error is not None or int(row["bad"] or 0):
            status = "failed"
        elif pages == 0 or last is None or last["next_cursor_ms"] is not None:
            status = "partial"
        else:
            status = "ok"

        self.db.upsert(
            "run_stream_coverage",
            {
                "run_id": run_id,
                "data_type": request.data_type,
                "hostility": request.hostility,
                "collection_profile": event_profile,
                "scope": request.scope,
                "source_id": request.source_id,
                "target_id": request.target_id,
                "requested_start_ms": rel_start_ms,
                "requested_end_ms": rel_end_ms,
                "pages": pages,
                "events": int(row["events"] or 0),
                "status": status,
                "error": error,
                "job_id": self.job_id,
                "query_version": QUERY_VERSION,
                "normalizer_version": NORMALIZER_VERSION,
                "software_version": SOFTWARE_VERSION,
                "collected_at": time.time(),
            },
        )

    def _store_roster(self, master: dict[str, Any], *, run_id: str, fight: dict[str, Any]) -> None:
        friendly = [int(p) for p in (fight.get("friendlyPlayers") or []) if isinstance(p, int)]
        run_rows, player_rows = normalize_run_players(
            master,
            run_id=run_id,
            friendly_player_ids=friendly,
            role_for=self.config.roles.role_for,
        )
        # Insert players without clobbering an existing first_seen.
        self.db.upsert_many("players", player_rows, replace=False)
        self.db.upsert_many("run_players", run_rows)
        if friendly and not run_rows:
            self.db.diagnostic(
                "empty_roster",
                f"{run_id} listed {len(friendly)} friendly players but none resolved "
                "to a player actor in master data.",
                run_id=run_id,
                job_id=self.job_id,
            )

    def _collect_pulls(
        self,
        *,
        dungeon_key: str | None = None,
        report_code: str,
        fight_id: int,
        run_id: str,
        report_start_ms: int,
        run_rel_start_ms: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        data = self.client.execute(
            render(
                "report_dungeon_pulls",
                {
                    "PULL_FIELDS": self._selection("ReportDungeonPull", WANTED_PULL_FIELDS),
                    "PULL_NPC_FIELDS": self._selection(
                        "ReportDungeonPullNPC", WANTED_PULL_NPC_FIELDS
                    ),
                },
            ),
            {"code": report_code, "fightIDs": [fight_id]},
            kind="report_dungeon_pulls",
            report_code=report_code,
        )
        fights = (((data.get("reportData") or {}).get("report")) or {}).get("fights") or []
        raw_pulls: list[dict[str, Any]] = []
        for fight in fights:
            if isinstance(fight, dict) and fight.get("id") == fight_id:
                raw_pulls.extend(
                    [p for p in (fight.get("dungeonPulls") or []) if isinstance(p, dict)]
                )

        pull_rows, npc_rows = normalize_pulls(
            raw_pulls,
            run_id=run_id,
            report_start_ms=report_start_ms,
            run_rel_start_ms=run_rel_start_ms,
            dungeon_key=dungeon_key,
        )
        with self.db.transaction():
            # Replace wholesale: a re-ingest must not leave stale pulls behind.
            self.db.execute(
                "DELETE FROM pull_npcs WHERE pull_id IN "
                "(SELECT pull_id FROM pulls WHERE run_id = ?)",
                (run_id,),
            )
            self.db.execute("DELETE FROM pulls WHERE run_id = ?", (run_id,))
            self.db.upsert_many("pulls", pull_rows)
            self.db.upsert_many("pull_npcs", npc_rows)
        if not pull_rows:
            self.db.diagnostic(
                "no_pulls",
                f"{run_id} returned no dungeonPulls; events cannot be assigned to pulls.",
                run_id=run_id,
                job_id=self.job_id,
                severity="warning",
            )
        return pull_rows, npc_rows

    # -- events -----------------------------------------------------------

    @staticmethod
    def _stream_key(run_id: str, request: EventRequest) -> tuple[Any, ...]:
        """Bind parameters identifying one stream, narrowing included.

        A stream narrowed to one actor answers a different question from the
        same stream unnarrowed, so it is a different stream everywhere: in the
        coverage manifest, in the page checkpoints, and in the provenance of
        every event row. Sharing one key between them corrupts resume, doubles
        the coverage counts, and makes a focus subset indistinguishable from the
        party-wide superset it sits inside.
        """
        return (
            run_id,
            request.data_type,
            request.hostility or "",
            -1 if request.source_id is None else request.source_id,
            -1 if request.target_id is None else request.target_id,
        )

    def _resume_point(self, run_id: str, request: EventRequest) -> tuple[int | None, int]:
        """Where to resume this stream, and the next page index to use.

        The checkpoint is the `event_pages` table itself. A page row is written
        in the same transaction as its events, so the last recorded page is by
        construction the last one whose events actually landed.

        Returns (cursor, next_page_index); a cursor of None with index > 0 means
        the stream already finished.
        """
        row = self.db.execute(
            "SELECT page_index, next_cursor_ms FROM event_pages "
            " WHERE run_id = ? AND data_type = ? AND IFNULL(hostility,'') = ? "
            "   AND IFNULL(source_id,-1) = ? AND IFNULL(target_id,-1) = ? "
            "   AND status = 'ok' "
            " ORDER BY page_index DESC LIMIT 1",
            (*self._stream_key(run_id, request),),
        ).fetchone()
        if row is None:
            return None, 0
        return row["next_cursor_ms"], int(row["page_index"]) + 1

    def _collect_events(
        self,
        request: EventRequest,
        *,
        run_id: str,
        report_code: str,
        report_start_ms: int,
        run_rel_start_ms: int,
        rel_start_ms: int,
        rel_end_ms: int,
        assigner: PullAssigner,
        pull_starts: dict[str, int],
    ) -> tuple[int, int]:
        """Fetch one event stream for a run, resuming if it was interrupted."""
        resume_cursor, next_index = self._resume_point(run_id, request)
        if next_index > 0 and resume_cursor is None:
            logger.debug("%s %s already complete", run_id, request.label)
            return 0, 0

        start_cursor = rel_start_ms if resume_cursor is None else int(resume_cursor)
        if next_index > 0:
            logger.info(
                "Resuming %s %s from page %d (cursor %s)",
                run_id,
                request.label,
                next_index,
                start_cursor,
            )

        page_counter = {"index": next_index}
        totals = {"events": 0, "pages": 0}

        def fetch_page(cursor: int | float) -> PageResult:
            data = self.client.execute(
                load_query("report_events"),
                {
                    "code": report_code,
                    "startTime": float(cursor),
                    "endTime": float(rel_end_ms),
                    "dataType": request.data_type,
                    "hostilityType": request.hostility,
                    "limit": self.page_limit,
                    "fightIDs": [int(run_id.split(":")[-1])],
                    "sourceID": request.source_id,
                    "targetID": request.target_id,
                },
                kind=f"events_{request.label}_{int(cursor)}",
                report_code=report_code,
            )
            block = (((data.get("reportData") or {}).get("report")) or {}).get("events") or {}
            events = [e for e in (block.get("data") or []) if isinstance(e, dict)]
            next_cursor = block.get("nextPageTimestamp")

            index = page_counter["index"]
            page_counter["index"] += 1

            # Events and their page row are written together: the checkpoint
            # can never claim a page whose events did not land.
            with self.db.transaction():
                cursor_row = {
                    "run_id": run_id,
                    "data_type": request.data_type,
                    "hostility": request.hostility,
                    "source_id": request.source_id,
                    "target_id": request.target_id,
                    "page_index": index,
                    "requested_start_ms": int(cursor),
                    "requested_end_ms": rel_end_ms,
                    "cursor_ms": int(cursor),
                    "next_cursor_ms": None if next_cursor is None else int(next_cursor),
                    "event_count": len(events),
                    # The link from a normalized row back to the exact payload
                    # it came from. Written as None until now, which quietly
                    # broke the provenance chain the whole corpus rests on:
                    # the payloads were cached, but nothing recorded where.
                    "raw_cache_path": self.client.last_cache_path,
                    "status": "ok",
                    "error": None,
                    "fetched_at": time.time(),
                }
                # A re-fetched page replaces itself: its events first (a page
                # row with events attached cannot be deleted while foreign keys
                # are enforced), then the row. `event_pages` has no unique
                # constraint on the natural key -- an unguarded insert would
                # leave two rows claiming one page index, and the lookup that
                # followed would pick between them arbitrarily.
                prior = self.db.execute(
                    "SELECT page_id FROM event_pages "
                    " WHERE run_id = ? AND data_type = ? AND IFNULL(hostility,'') = ? "
                    "   AND IFNULL(source_id,-1) = ? AND IFNULL(target_id,-1) = ? "
                    "   AND page_index = ?",
                    (*self._stream_key(run_id, request), index),
                ).fetchall()
                for old_page in prior:
                    self.db.execute("DELETE FROM events WHERE page_id = ?", (old_page["page_id"],))
                    self.db.execute(
                        "DELETE FROM event_pages WHERE page_id = ?", (old_page["page_id"],)
                    )

                inserted = self.db.execute(
                    "INSERT INTO event_pages "
                    f"({', '.join(cursor_row)}) "
                    f"VALUES ({', '.join('?' for _ in cursor_row)})",
                    tuple(cursor_row.values()),
                ).lastrowid
                if inserted is None:  # pragma: no cover - sqlite always sets it
                    raise CollectionError("event_pages insert returned no row id")
                page_id = int(inserted)

                rows = []
                for seq, event in enumerate(events):
                    rel_ms = event.get("timestamp")
                    pull = (
                        assigner.assign(int(rel_ms)) if isinstance(rel_ms, (int, float)) else None
                    )
                    rows.append(
                        normalize_event(
                            event,
                            page_id=page_id,
                            seq_in_page=seq,
                            run_id=run_id,
                            report_code=report_code,
                            report_start_ms=report_start_ms,
                            run_rel_start_ms=run_rel_start_ms,
                            data_type=request.data_type,
                            hostility=request.hostility,
                            pull_id=pull.pull_id if pull else None,
                            pull_rel_start_ms=(pull_starts.get(pull.pull_id) if pull else None),
                        )
                    )
                self.db.upsert_many("events", rows)

            totals["events"] += len(events)
            totals["pages"] += 1

            unexpected = unexpected_event_fields(events)
            if unexpected:
                self.db.diagnostic(
                    "unexpected_event_fields",
                    f"{request.label} in {run_id} carried unmodelled field(s): "
                    f"{sorted(unexpected)}. Preserved in events.extra.",
                    run_id=run_id,
                    job_id=self.job_id,
                    severity="info",
                )

            return PageResult(events=events, next_page_timestamp=next_cursor, raw=block)

        paginator = EventPaginator(
            fetch_page,
            kind=f"{run_id}:{request.label}",
            start_time=start_cursor,
            end_time=rel_end_ms,
            report_code=report_code,
        )
        # Rows are written inside fetch_page; draining the iterator is what
        # drives pagination, and it keeps peak memory to one page.
        for _ in paginator.iter_events():
            pass

        for warning in paginator.diagnostics.warnings:
            self.db.diagnostic(
                "pagination", warning, run_id=run_id, job_id=self.job_id, severity="info"
            )
        return totals["events"], totals["pages"]

    def reset_run_events(self, run_id: str) -> int:
        """Drop a run's event pages and events so they are fetched again.

        Needed when the normalizer changes: the cached raw responses stay, but
        the derived rows must be rebuilt. Without this, `_resume_point` would
        see a completed stream and skip it.
        """
        with self.db.transaction():
            removed = int(
                self.db.scalar("SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)) or 0
            )
            self.db.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
            self.db.execute("DELETE FROM event_pages WHERE run_id = ?", (run_id,))
        return removed

    # -- top level --------------------------------------------------------

    def collect(
        self,
        candidates: list[ReportCandidate],
        *,
        event_profile: str = "mechanics",
        dungeon_key: str | None = None,
        max_runs_per_report: int | None = None,
        focus_player: str | None = None,
    ) -> CollectionResult:
        result = CollectionResult(job_id=self.job_id)
        self.focus = FocusResolution(requested=focus_player)
        self.start_job(sample_profile=None, event_profile=event_profile)
        status = "complete"
        # Read the budget before any work, not after. stats.points_spent_observed
        # is None until the first request lands, so sampling it here without
        # asking recorded "unknown" for every first job -- which is exactly the
        # job whose cost we most need.
        points_before: float | None = None
        try:
            points_before = self.client.fetch_rate_limit().points_spent
        except (ApiError, RedactedError) as exc:  # pragma: no cover - network only
            logger.debug("Could not read the point budget at job start: %s", exc)
        requests_before = self.client.stats.requests
        try:
            for candidate in candidates:
                result.reports_attempted += 1
                try:
                    outcomes = self.collect_report(
                        candidate,
                        event_profile=event_profile,
                        dungeon_key=dungeon_key,
                        max_runs=max_runs_per_report,
                        focus_player=focus_player,
                    )
                except ApiError as exc:
                    result.reports_failed += 1
                    result.errors.append(f"{candidate.code}: {exc}")
                    self.db.diagnostic(
                        "report_failed",
                        f"{candidate.code}: {exc}",
                        job_id=self.job_id,
                        severity="error",
                    )
                    self.db.conn.commit()
                    continue
                result.runs.extend(outcomes)
                if outcomes and all(o.status == "complete" for o in outcomes) or not outcomes:
                    result.reports_completed += 1
                else:
                    result.reports_failed += 1
        except KeyboardInterrupt:
            # Everything written so far is durable and resumable: page rows and
            # their events commit together.
            status = "interrupted"
            logger.warning("Interrupted. Progress is saved; resume with the same job.")
            raise
        finally:
            self._record_focus_resolution(result)
            self._record_api_cost(result, points_before, requests_before)
            self.finish_job(result, status=status)
        return result

    def _record_api_cost(
        self,
        result: CollectionResult,
        points_before: float | None,
        requests_before: int,
    ) -> None:
        """Write what this job spent of the hourly budget.

        Wall clock says how long a corpus takes to build; points say whether the
        API will allow it at all, and the two bind at different corpus sizes.
        Only the API knows the cost of a query, so it is observed rather than
        modelled.

        The hourly counter resets on its own schedule, so a job spanning a reset
        yields a negative delta. That is recorded as unknown rather than as a
        suspiciously cheap job -- an underestimate here would license a corpus
        size the quota cannot actually support.
        """
        points_after: float | None = None
        try:
            points_after = self.client.fetch_rate_limit().points_spent
        except (ApiError, RedactedError) as exc:  # pragma: no cover - network only
            logger.debug("Could not read the point budget at job end: %s", exc)
        if points_after is None:
            points_after = self.client.stats.points_spent_observed
        spent: float | None = None
        if points_before is not None and points_after is not None:
            delta = points_after - points_before
            spent = delta if delta >= 0 else None

        runs = len([o for o in result.runs if o.status == "complete"])
        self.db.diagnostic(
            "api_cost",
            json_or_none(
                {
                    "points_spent": spent,
                    "points_unknown_reason": (
                        None if spent is not None else "hourly counter reset mid-job or unavailable"
                    ),
                    "requests": self.client.stats.requests - requests_before,
                    "runs_completed": runs,
                    "points_per_run": (
                        round(spent / runs, 2) if spent is not None and runs else None
                    ),
                }
            )
            or "{}",
            job_id=self.job_id,
            severity="info",
        )
        self.db.conn.commit()
