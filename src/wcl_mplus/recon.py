"""Phase 0: live API reconnaissance.

This module answers Gate A (brief section 56) with evidence rather than
assertion. It introspects the live schema, verifies every field the research
design wants, measures what a query costs, **empirically determines the event
pagination semantics**, samples each event category, and writes sanitized
fixtures plus a machine-readable findings file.

Nothing here assumes a field exists. Queries are generated from the fields
introspection confirmed, so a schema change is recorded as an absence instead
of crashing a collection run.

Output:

* ``data/exports/recon/recon_findings.json`` -- machine-readable evidence
* ``data/exports/recon/RECON_REPORT.md``     -- human summary
* ``tests/fixtures/*.json``                  -- sanitized real responses

The report is written even when steps fail, because a recorded failure is a
Phase 0 finding.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import ApiError, GraphQLClient, GraphQLError
from .configs import ProjectConfig
from .paginate import EventPaginator, PageResult, event_timestamp
from .querybuild import (
    WANTED_EVENT_DATA_TYPES,
    WANTED_FIGHT_FIELDS,
    WANTED_PULL_FIELDS,
    WANTED_PULL_NPC_FIELDS,
    WANTED_REPORT_FIELDS,
    load_query,
    render,
)
from .sanitize import sanitize_payload
from .schema import SchemaIntrospector, Selection
from .settings import Settings
from .version import provenance

logger = logging.getLogger(__name__)

#: Types whose shape the collector depends on. Names are candidates: if one is
#: absent, recon searches for a renamed equivalent and records what it found.
CORE_TYPES: dict[str, tuple[str, ...]] = {
    "Query": ("query root",),
    "RateLimitData": ("ratelimit",),
    "ReportData": ("report",),
    "Report": ("report",),
    "ReportFight": ("fight",),
    "ReportDungeonPull": ("dungeonpull", "pull"),
    "ReportDungeonPullNPC": ("pullnpc", "npc"),
    "ReportMasterData": ("masterdata",),
    "ReportActor": ("actor",),
    "ReportAbility": ("ability",),
    "ReportEventPaginator": ("event",),
    "EventDataType": ("eventdatatype", "datatype"),
    "HostilityType": ("hostility",),
    "WorldData": ("world",),
    "Zone": ("zone",),
    "Encounter": ("encounter",),
    "CharacterData": ("character",),
    "Character": ("character",),
    "GuildData": ("guild",),
}

#: Small sample size for event probes: enough to see the shape, cheap in points.
EVENT_SAMPLE_LIMIT = 50

#: Deliberately tiny page size for the pagination probe, so multiple pages are
#: forced even inside one short fight.
PAGINATION_PROBE_LIMIT = 25


@dataclass
class ReconFindings:
    """Accumulated Phase 0 evidence."""

    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    provenance: dict[str, Any] = field(default_factory=provenance)
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)

    def record(self, name: str, status: str, **detail: Any) -> None:
        self.steps[name] = {"status": status, **detail}
        logger.info("recon step %-28s %s", name, status)

    def limitation(self, text: str) -> None:
        if text not in self.limitations:
            self.limitations.append(text)

    def to_json(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "provenance": self.provenance,
            "steps": self.steps,
            "limitations": self.limitations,
            "fixtures": self.fixtures,
        }


class Recon:
    """Runs the Phase 0 checks and writes the evidence files."""

    def __init__(
        self,
        client: GraphQLClient,
        settings: Settings,
        *,
        config: ProjectConfig | None = None,
        output_dir: Path | None = None,
        write_fixtures: bool = True,
    ) -> None:
        self.client = client
        self.settings = settings
        self.config = config
        self.introspector = SchemaIntrospector(client)
        self.output_dir = output_dir or (settings.exports_dir / "recon")
        self.write_fixtures = write_fixtures
        self.findings = ReconFindings()

    # -- helpers ----------------------------------------------------------

    def _selection(
        self,
        type_name: str,
        wanted: list[str],
        *,
        exclude_leaves: frozenset[str] = frozenset(),
    ) -> Selection | None:
        """Introspection-derived selection, or None if nothing is usable.

        Warnings (a composite field with no selectable leaves, a field that
        vanished from the schema) become recorded limitations rather than an
        invalid query.
        """
        present = self.introspector.present_fields(type_name, wanted)
        if not present:
            return None
        selection = self.introspector.build_selection(
            type_name, present, exclude_leaves=exclude_leaves
        )
        for warning in selection.warnings:
            self.findings.limitation(warning)
        return selection if selection.text else None

    def _execute_with_leaf_retry(
        self,
        *,
        template: str,
        placeholder: str,
        type_name: str,
        wanted: list[str],
        variables: dict[str, Any],
        kind: str,
        report_code: str | None = None,
        max_attempts: int = 3,
    ) -> tuple[dict[str, Any] | None, list[str], str | None]:
        """Run a query, dropping any nested field the server objects to.

        Some fields are permission-gated in a way introspection does not
        reveal. Selecting `User.avatar` made the live API answer
        `You do not have permission to view the avatar for this user.` and
        fail the entire report query -- costing a whole round trip to discover
        one unusable field.

        So a field-specific complaint is treated as information: any leaf name
        mentioned in the error is dropped and the query retried once more.
        Dropped fields are returned and recorded, never silently swallowed.

        Returns (data, dropped_leaf_names, error). `data` is None on failure.
        """
        excluded: set[str] = set()
        last_error: str | None = None

        for _ in range(max_attempts):
            selection = self._selection(type_name, wanted, exclude_leaves=frozenset(excluded))
            if selection is None:
                return None, sorted(excluded), "no usable fields remain after exclusions"
            try:
                data = self.client.execute(
                    render(template, {placeholder: selection.text}),
                    variables,
                    kind=kind,
                    report_code=report_code,
                )
            except GraphQLError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                message = str(exc)
                # Any leaf we asked for that the server names is a candidate
                # to drop. Word-boundary match: the complaint is prose, not a
                # structured field reference.
                blamed = {
                    leaf
                    for leaf in selection.leaves
                    if leaf not in excluded
                    and re.search(rf"\b{re.escape(leaf)}\b", message, re.IGNORECASE)
                }
                if not blamed:
                    return None, sorted(excluded), last_error
                excluded |= blamed
                logger.warning(
                    "%s: server objected to %s; dropping and retrying",
                    kind,
                    sorted(blamed),
                )
                continue
            except Exception as exc:  # noqa: BLE001
                return None, sorted(excluded), f"{type(exc).__name__}: {exc}"
            else:
                return data, sorted(excluded), None

        return None, sorted(excluded), last_error

    def _save_fixture(self, name: str, payload: Any) -> None:
        """Write a sanitized fixture for offline tests."""
        if not self.write_fixtures:
            return
        target = self.settings.fixtures_dir / f"{name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(sanitize_payload(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        rel = f"tests/fixtures/{target.name}"
        if rel not in self.findings.fixtures:
            self.findings.fixtures.append(rel)

    # -- steps ------------------------------------------------------------

    def step_auth(self) -> bool:
        try:
            token = self.client.tokens.token()
        except ApiError as exc:
            self.findings.record("auth", "FAILED", error=str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            self.findings.record("auth", "FAILED", error=f"{type(exc).__name__}: {exc}")
            return False
        self.findings.record(
            "auth",
            "OK",
            token_type=token.token_type,
            seconds_remaining=token.seconds_remaining,
            note="Token value is never recorded.",
        )
        return True

    def step_rate_limit(self) -> None:
        try:
            state = self.client.fetch_rate_limit()
        except Exception as exc:  # noqa: BLE001
            self.findings.record("rate_limit", "FAILED", error=f"{type(exc).__name__}: {exc}")
            self.findings.limitation(
                "Rate-limit state could not be read; budget enforcement will be blind."
            )
            return
        self.findings.record(
            "rate_limit",
            "OK" if state.known else "SHAPE_UNKNOWN",
            **state.summary(),
            observed_payload_keys=sorted(state.raw),
        )
        if not state.known:
            self.findings.limitation(
                "The rate-limit type did not expose the expected fields; update the "
                "candidate names in ratelimit.py."
            )

    def step_schema_types(self) -> None:
        try:
            type_list = self.introspector.type_list()
        except Exception as exc:  # noqa: BLE001
            self.findings.record("schema_types", "FAILED", error=f"{type(exc).__name__}: {exc}")
            return
        present: dict[str, str] = {}
        missing: dict[str, dict[str, str]] = {}
        for name, needles in CORE_TYPES.items():
            if name in type_list:
                present[name] = type_list[name]
            else:
                # Look for a renamed equivalent so the report says what to use.
                missing[name] = self.introspector.find_types(*needles)
        self.findings.record(
            "schema_types",
            "OK" if not missing else "PARTIAL",
            total_types_in_schema=len(type_list),
            expected_present=present,
            expected_missing_with_candidates=missing,
        )
        for name, candidates in missing.items():
            self.findings.limitation(
                f"Type {name!r} is not in the live schema. Similar names: "
                f"{sorted(candidates)[:8] or 'none found'}."
            )
        self._save_fixture("schema_type_list", {"types": type_list})

    def step_field_verification(self) -> None:
        """Check every field the research design wants, type by type."""
        wanted: dict[str, list[str]] = {
            "Report": WANTED_REPORT_FIELDS,
            "ReportFight": WANTED_FIGHT_FIELDS,
            "ReportDungeonPull": WANTED_PULL_FIELDS,
            "ReportDungeonPullNPC": WANTED_PULL_NPC_FIELDS,
        }
        results: dict[str, Any] = {}
        for type_name, fields in wanted.items():
            checks = self.introspector.check_fields(type_name, fields)
            present = [c.field_name for c in checks if c.present]
            absent = [c.field_name for c in checks if not c.present]
            results[type_name] = {
                "present": {c.field_name: c.resolved_type for c in checks if c.present},
                "absent": absent,
                "deprecated": [c.field_name for c in checks if c.deprecated],
            }
            if absent:
                self.findings.limitation(
                    f"{type_name}: requested field(s) absent from the live schema: {absent}. "
                    "Any analysis depending on them is not supported by the API."
                )
            logger.info("%s: %d/%d wanted fields present", type_name, len(present), len(fields))

        # Argument names on the fields the collector calls.
        for type_name, field_name in (
            ("Report", "events"),
            ("Report", "fights"),
            ("ReportData", "report"),
        ):
            info = self.introspector.type_info(type_name)
            if info and info.has(field_name):
                results.setdefault("arguments", {})[f"{type_name}.{field_name}"] = info.arg_names(
                    field_name
                )

        # Event category enum -- the single most important enum for this project.
        enum_values = self.introspector.enum_values("EventDataType")
        unsupported = [t for t in WANTED_EVENT_DATA_TYPES if t not in enum_values]
        results["EventDataType"] = {
            "live_values": enum_values,
            "wanted_but_unsupported": unsupported,
        }
        if enum_values and unsupported:
            self.findings.limitation(
                f"EventDataType does not accept {unsupported}; those event categories "
                "cannot be requested."
            )
        results["HostilityType"] = {"live_values": self.introspector.enum_values("HostilityType")}

        if self.config is not None and enum_values:
            problems = self.config.sampling.validate_event_types(enum_values)
            results["config_event_type_problems"] = problems
            for profile, bad in problems.items():
                self.findings.limitation(
                    f"config/sampling.yml event profile {profile!r} lists unsupported "
                    f"event type(s) {bad}."
                )

        self.findings.record("field_verification", "OK", **results)
        self._save_fixture("schema_field_verification", results)

    def step_report_metadata(self, report_code: str) -> dict[str, Any] | None:
        fields = self.introspector.present_fields("Report", WANTED_REPORT_FIELDS)
        if not fields:
            self.findings.record("report_metadata", "SKIPPED", reason="no Report fields verified")
            return None

        data, dropped, error = self._execute_with_leaf_retry(
            template="report_metadata",
            placeholder="REPORT_FIELDS",
            type_name="Report",
            wanted=WANTED_REPORT_FIELDS,
            variables={"code": report_code},
            kind="report_metadata",
            report_code=report_code,
        )
        if dropped:
            self.findings.limitation(
                f"Report metadata: nested field(s) {dropped} were rejected by the API "
                "(typically permission-gated) and dropped from the query. They are not "
                "available to this client."
            )
        if data is None:
            self.findings.record("report_metadata", "FAILED", error=error, dropped_fields=dropped)
            return None

        report = ((data.get("reportData") or {}).get("report")) or {}
        if not report:
            self.findings.record(
                "report_metadata",
                "FAILED",
                error="Report not found or not public.",
                report_code=report_code,
            )
            self.findings.limitation(
                f"Report {report_code} returned no data. It may be private, deleted, or "
                "the code may be wrong. Private reports are out of scope for v1."
            )
            return None

        self.findings.record(
            "report_metadata",
            "OK",
            requested_fields=fields,
            dropped_fields=dropped,
            returned_keys=sorted(report),
            archive_status=report.get("archiveStatus"),
            visibility=report.get("visibility"),
            zone=report.get("zone"),
            start_time=report.get("startTime"),
            end_time=report.get("endTime"),
        )
        archive = report.get("archiveStatus")
        if isinstance(archive, dict) and archive.get("isArchived"):
            self.findings.limitation(
                "This report is archived; event data may be unavailable. Archived "
                "reports must never enter an event-frequency denominator."
            )
        self._save_fixture("report_metadata", data)
        return report

    def step_fights(self, report_code: str) -> list[dict[str, Any]]:
        fields = self.introspector.present_fields("ReportFight", WANTED_FIGHT_FIELDS)
        selection = self._selection("ReportFight", WANTED_FIGHT_FIELDS)
        if selection is None:
            self.findings.record("fights", "SKIPPED", reason="no ReportFight fields verified")
            return []
        try:
            data = self.client.execute(
                render("report_fights", {"FIGHT_FIELDS": selection.text}),
                {"code": report_code},
                kind="report_fights",
                report_code=report_code,
            )
        except Exception as exc:  # noqa: BLE001
            self.findings.record("fights", "FAILED", error=f"{type(exc).__name__}: {exc}")
            return []
        report = ((data.get("reportData") or {}).get("report")) or {}
        fights = [f for f in (report.get("fights") or []) if isinstance(f, dict)]
        keystone = [f for f in fights if f.get("keystoneLevel") is not None]
        self.findings.record(
            "fights",
            "OK" if fights else "EMPTY",
            requested_fields=fields,
            total_fights=len(fights),
            mythic_plus_fights=len(keystone),
            keystone_levels=sorted({f.get("keystoneLevel") for f in keystone}),
            example_keys=sorted(fights[0]) if fights else [],
            mplus_example={k: keystone[0].get(k) for k in sorted(keystone[0])}
            if keystone
            else None,
        )
        if fights and not keystone:
            self.findings.limitation(
                "No fight in this report carried a keystoneLevel, so it is not a "
                "Mythic+ report. Supply a Mythic+ report code to complete Gate A."
            )
        self._save_fixture("report_fights", data)
        return keystone or fights

    def step_dungeon_pulls(self, report_code: str, fight_id: int) -> dict[str, Any] | None:
        pull_fields = self.introspector.present_fields("ReportDungeonPull", WANTED_PULL_FIELDS)
        npc_fields = self.introspector.present_fields(
            "ReportDungeonPullNPC", WANTED_PULL_NPC_FIELDS
        )
        pull_selection = self._selection("ReportDungeonPull", WANTED_PULL_FIELDS)
        npc_selection = self._selection("ReportDungeonPullNPC", WANTED_PULL_NPC_FIELDS)
        if pull_selection is None or npc_selection is None:
            self.findings.record(
                "dungeon_pulls",
                "SKIPPED",
                reason="pull or pull-NPC fields unverified",
                pull_fields=pull_fields,
                npc_fields=npc_fields,
            )
            return None
        try:
            data = self.client.execute(
                render(
                    "report_dungeon_pulls",
                    {
                        "PULL_FIELDS": pull_selection.text,
                        "PULL_NPC_FIELDS": npc_selection.text,
                    },
                ),
                {"code": report_code, "fightIDs": [fight_id]},
                kind="report_dungeon_pulls",
                report_code=report_code,
            )
        except Exception as exc:  # noqa: BLE001
            self.findings.record("dungeon_pulls", "FAILED", error=f"{type(exc).__name__}: {exc}")
            return None

        fights = (((data.get("reportData") or {}).get("report")) or {}).get("fights") or []
        pulls = []
        for fight in fights:
            if isinstance(fight, dict):
                pulls.extend([p for p in (fight.get("dungeonPulls") or []) if isinstance(p, dict)])

        # Does any pull contain two copies of one NPC species? That is the
        # fixture the instance-identity validation needs (brief section 14).
        duplicate_species = []
        for pull in pulls:
            counts: dict[Any, int] = {}
            for npc in pull.get("enemyNPCs") or []:
                if isinstance(npc, dict):
                    game_id = npc.get("gameID")
                    counts[game_id] = counts.get(game_id, 0) + 1
                    span = None
                    lo, hi = npc.get("minimumInstanceID"), npc.get("maximumInstanceID")
                    if isinstance(lo, int) and isinstance(hi, int):
                        span = hi - lo + 1
                    if span and span > 1:
                        duplicate_species.append(
                            {
                                "pull_id": pull.get("id"),
                                "gameID": game_id,
                                "instance_span": span,
                                "reason": "instance ID range covers multiple copies",
                            }
                        )
            for game_id, count in counts.items():
                if count > 1:
                    duplicate_species.append(
                        {
                            "pull_id": pull.get("id"),
                            "gameID": game_id,
                            "npc_rows": count,
                            "reason": "same gameID listed more than once",
                        }
                    )

        self.findings.record(
            "dungeon_pulls",
            "OK" if pulls else "EMPTY",
            fight_id=fight_id,
            requested_pull_fields=pull_fields,
            requested_npc_fields=npc_fields,
            pull_count=len(pulls),
            example_pull_keys=sorted(pulls[0]) if pulls else [],
            example_npc_keys=(
                sorted((pulls[0].get("enemyNPCs") or [{}])[0])
                if pulls and pulls[0].get("enemyNPCs")
                else []
            ),
            duplicate_npc_species_candidates=duplicate_species[:20],
            note=(
                "duplicate_npc_species_candidates identifies pulls usable as the "
                "two-identical-NPC validation fixture."
            ),
        )
        if not pulls:
            self.findings.limitation(
                "This fight returned no dungeonPulls. WCL pull boundaries are the "
                "intended source of truth, so a Mythic+ fight with pulls is required."
            )
        if not duplicate_species:
            self.findings.limitation(
                "No pull in this fight contained two copies of one NPC species. Another "
                "report is needed for the NPC-instance-identity validation fixture."
            )
        self._save_fixture("report_dungeon_pulls", data)
        return data

    def step_master_data(self, report_code: str) -> dict[str, Any] | None:
        try:
            data = self.client.execute(
                load_query("report_master_data"),
                {"code": report_code},
                kind="report_master_data",
                report_code=report_code,
            )
        except Exception as exc:  # noqa: BLE001
            self.findings.record("master_data", "FAILED", error=f"{type(exc).__name__}: {exc}")
            return None
        master = (((data.get("reportData") or {}).get("report")) or {}).get("masterData") or {}
        actors = [a for a in (master.get("actors") or []) if isinstance(a, dict)]
        abilities = [a for a in (master.get("abilities") or []) if isinstance(a, dict)]
        by_type: dict[str, int] = {}
        for actor in actors:
            by_type[str(actor.get("type"))] = by_type.get(str(actor.get("type")), 0) + 1
        self.findings.record(
            "master_data",
            "OK" if actors else "EMPTY",
            log_version=master.get("logVersion"),
            game_version=master.get("gameVersion"),
            actor_count=len(actors),
            actors_by_type=dict(sorted(by_type.items())),
            ability_count=len(abilities),
            actor_keys=sorted(actors[0]) if actors else [],
            ability_keys=sorted(abilities[0]) if abilities else [],
            note="Player names are pseudonymized in the saved fixture.",
        )
        self._save_fixture("report_master_data", data)
        return data

    def step_event_samples(
        self, report_code: str, fight: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Sample each event category and record its real shape."""
        enum_values = self.introspector.enum_values("EventDataType")
        categories = [
            c
            for c in (
                "Casts",
                "Debuffs",
                "Buffs",
                "Interrupts",
                "Dispels",
                "Deaths",
                "DamageTaken",
                "Summons",
            )
            if not enum_values or c in enum_values
        ]
        start = fight.get("startTime")
        end = fight.get("endTime")
        if start is None or end is None:
            self.findings.record("event_samples", "SKIPPED", reason="fight has no time range")
            return {}

        results: dict[str, dict[str, Any]] = {}
        for category in categories:
            variables = {
                "code": report_code,
                "startTime": float(start),
                "endTime": float(end),
                "dataType": category,
                "limit": EVENT_SAMPLE_LIMIT,
                "fightIDs": [fight.get("id")],
                "hostilityType": None,
            }
            try:
                data = self.client.execute(
                    load_query("report_events"),
                    variables,
                    kind=f"events_sample_{category}",
                    report_code=report_code,
                )
            except Exception as exc:  # noqa: BLE001
                results[category] = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
                continue
            block = (((data.get("reportData") or {}).get("report")) or {}).get("events") or {}
            events = block.get("data") or []
            event_types: dict[str, int] = {}
            keys: set[str] = set()
            for event in events:
                if isinstance(event, dict):
                    event_types[str(event.get("type"))] = (
                        event_types.get(str(event.get("type")), 0) + 1
                    )
                    keys.update(event)
            results[category] = {
                "status": "OK",
                "returned": len(events),
                "next_page_timestamp": block.get("nextPageTimestamp"),
                "event_type_counts": dict(sorted(event_types.items())),
                "observed_field_names": sorted(keys),
                "first_event": events[0] if events else None,
            }
            self._save_fixture(f"events_sample_{category.lower()}", data)

        self.findings.record("event_samples", "OK", categories=results)

        # The fields that later analysis depends on most.
        all_keys = {k for r in results.values() for k in r.get("observed_field_names", [])}
        for critical in ("timestamp", "type", "sourceID", "targetID", "abilityGameID"):
            if all_keys and critical not in all_keys:
                self.findings.limitation(
                    f"No sampled event exposed a {critical!r} field; event normalization "
                    "assumptions need revisiting."
                )
        if all_keys and not ({"sourceInstance", "targetInstance"} & all_keys):
            self.findings.limitation(
                "No sampled event carried sourceInstance/targetInstance. Without these, "
                "two copies of the same NPC cannot be separated from events alone -- "
                "the core requirement of brief section 14. Re-check with a pull known "
                "to contain duplicate NPCs before trusting per-instance timelines."
            )
        return results

    def step_pagination_probe(self, report_code: str, fight: dict[str, Any]) -> None:
        """Determine the pagination cursor semantics empirically.

        The brief forbids guessing this (section 16). A deliberately tiny page
        limit forces several pages inside one fight; comparing page 2's head
        against page 1's tail shows whether `nextPageTimestamp` is inclusive.
        """
        start, end = fight.get("startTime"), fight.get("endTime")
        if start is None or end is None:
            self.findings.record("pagination_probe", "SKIPPED", reason="fight has no time range")
            return

        def fetch(cursor: float) -> PageResult:
            data = self.client.execute(
                load_query("report_events"),
                {
                    "code": report_code,
                    "startTime": float(cursor),
                    "endTime": float(end),
                    "dataType": "Casts",
                    "limit": PAGINATION_PROBE_LIMIT,
                    "fightIDs": [fight.get("id")],
                    "hostilityType": None,
                },
                kind=f"pagination_probe_{int(cursor)}",
                report_code=report_code,
            )
            block = (((data.get("reportData") or {}).get("report")) or {}).get("events") or {}
            return PageResult(
                events=[e for e in (block.get("data") or []) if isinstance(e, dict)],
                next_page_timestamp=block.get("nextPageTimestamp"),
                raw=block,
            )

        try:
            page1 = fetch(float(start))
        except Exception as exc:  # noqa: BLE001
            self.findings.record("pagination_probe", "FAILED", error=f"{type(exc).__name__}: {exc}")
            return

        if page1.next_page_timestamp is None:
            self.findings.record(
                "pagination_probe",
                "SINGLE_PAGE",
                page_limit=PAGINATION_PROBE_LIMIT,
                events=len(page1.events),
                note=(
                    "One page covered the whole fight even at a tiny limit, so multi-page "
                    "behaviour is unproven. Re-probe on a longer fight or a busier event type."
                ),
            )
            self.findings.limitation(
                "Pagination was not exercised across pages; multi-page correctness is "
                "still unverified against the live API."
            )
            return

        last_ts_page1 = max(
            (t for t in (event_timestamp(e) for e in page1.events) if t is not None), default=None
        )
        page2 = fetch(float(page1.next_page_timestamp))
        first_ts_page2 = next(
            (t for t in (event_timestamp(e) for e in page2.events) if t is not None), None
        )

        tail = [e for e in page1.events if event_timestamp(e) == last_ts_page1]
        head = [e for e in page2.events if event_timestamp(e) == last_ts_page1]
        repeated = [e for e in head if e in tail]

        if page1.next_page_timestamp == last_ts_page1:
            semantics = "inclusive (cursor equals the last timestamp of the previous page)"
        elif last_ts_page1 is not None and page1.next_page_timestamp > last_ts_page1:
            semantics = "exclusive (cursor is past the last timestamp of the previous page)"
        else:
            semantics = "unexpected (cursor is before the previous page's last timestamp)"

        # Full traversal with the real paginator, to confirm it terminates.
        try:
            paginator = EventPaginator(
                fetch,
                kind="pagination_probe_full",
                start_time=float(start),
                end_time=float(end),
                report_code=report_code,
            )
            total = len(paginator.collect())
            diagnostics = paginator.diagnostics.summary()
            traversal = "OK"
        except Exception as exc:  # noqa: BLE001
            total, diagnostics, traversal = None, {}, f"FAILED: {type(exc).__name__}: {exc}"

        self.findings.record(
            "pagination_probe",
            "OK",
            page_limit=PAGINATION_PROBE_LIMIT,
            page1_events=len(page1.events),
            page1_last_timestamp=last_ts_page1,
            next_page_timestamp=page1.next_page_timestamp,
            page2_events=len(page2.events),
            page2_first_timestamp=first_ts_page2,
            cursor_semantics=semantics,
            boundary_events_repeated=len(repeated),
            full_traversal=traversal,
            total_events_deduplicated=total,
            diagnostics=diagnostics,
            note=(
                "boundary_events_repeated > 0 confirms the multiset boundary matching in "
                "paginate.py is load-bearing; 0 means the cursor is already exclusive."
            ),
        )

    def step_query_cost(self, report_code: str) -> None:
        """Measure the point cost of a representative query."""
        selection = self._selection("ReportFight", WANTED_FIGHT_FIELDS)
        if selection is None:
            self.findings.record("query_cost", "SKIPPED", reason="fight fields unverified")
            return
        try:
            measurement = self.client.measure_query_cost(
                render("report_fights", {"FIGHT_FIELDS": selection.text}),
                {"code": report_code},
                kind="cost_probe_report_fights",
            )
        except Exception as exc:  # noqa: BLE001
            self.findings.record("query_cost", "FAILED", error=f"{type(exc).__name__}: {exc}")
            return
        self.findings.record("query_cost", "OK", **measurement)

    def step_discovery(self) -> None:
        """Record which supported report-discovery paths actually exist."""
        probes: dict[str, Any] = {}
        for type_name, field_name in (
            ("ReportData", "reports"),
            ("ReportData", "report"),
            ("Character", "recentReports"),
            ("Guild", "attendance"),
            ("GuildData", "guild"),
            ("WorldData", "zones"),
            ("WorldData", "encounter"),
            ("Zone", "encounters"),
            ("Encounter", "characterRankings"),
            ("Encounter", "fightRankings"),
        ):
            info = self.introspector.type_info(type_name)
            if info is None:
                probes[f"{type_name}.{field_name}"] = {"present": False, "reason": "type absent"}
            else:
                probes[f"{type_name}.{field_name}"] = {
                    "present": info.has(field_name),
                    "type": info.field_type(field_name),
                    "args": info.arg_names(field_name),
                }
        self.findings.record(
            "discovery_paths",
            "OK",
            probes=probes,
            note=(
                "Presence of a rankings field does not imply it returns report codes. "
                "Inspect the recorded return type before relying on ranking-derived "
                "discovery, and never scrape as a substitute."
            ),
        )
        reports_probe = probes.get("ReportData.reports", {})
        if not reports_probe.get("present"):
            self.findings.limitation(
                "ReportData.reports is absent, so list-style report discovery is "
                "unavailable; use --report-list with manually collected codes."
            )
        elif reports_probe.get("args"):
            self.findings.record(
                "discovery_reports_arguments",
                "OK",
                args=reports_probe["args"],
                note=(
                    "These arguments define what scoped discovery is possible. There is "
                    "no documented global 'random public +10 logs' query; sampling must "
                    "work within these scopes and record the resulting bias."
                ),
            )

    def step_zones(self) -> None:
        """Fetch the zone/encounter registry so dungeon IDs are never guessed.

        Matching is delegated to `discover.match_zones` so recon and
        `discover-dungeons` cannot disagree. That matters: the first live run
        of this step matched dungeon names against *zone* names and found
        nothing, because Warcraft Logs models a Mythic+ season as one zone
        whose **encounters** are the dungeons.
        """
        from .discover import match_zones

        try:
            data = self.client.execute(
                load_query("world_zones"), {"expansionID": None}, kind="world_zones"
            )
        except Exception as exc:  # noqa: BLE001
            self.findings.record("world_zones", "FAILED", error=f"{type(exc).__name__}: {exc}")
            return

        world = data.get("worldData") or {}
        zones = [z for z in (world.get("zones") or []) if isinstance(z, dict)]
        expansions = [e for e in (world.get("expansions") or []) if isinstance(e, dict)]
        # Newest first, by ID: the API's own ordering is not guaranteed, and
        # slicing it cost us the current expansion once already.
        ordered_expansions = sorted(
            ({"id": e.get("id"), "name": e.get("name")} for e in expansions),
            key=lambda e: (e["id"] is None, e["id"]),
            reverse=True,
        )

        result = match_zones(self.config.dungeons, zones) if self.config is not None else {}

        # The full zone/encounter inventory, so the season's real composition
        # can be read straight out of the findings without a second run.
        inventory = [
            {
                "id": zone.get("id"),
                "name": zone.get("name"),
                "frozen": zone.get("frozen"),
                "expansion": zone.get("expansion"),
                "encounters": [
                    {"id": e.get("id"), "name": e.get("name")}
                    for e in (zone.get("encounters") or [])
                    if isinstance(e, dict)
                ],
                "partitions": zone.get("partitions"),
            }
            for zone in zones
        ]

        self.findings.record(
            "world_zones",
            "OK" if zones else "EMPTY",
            expansions=ordered_expansions,
            newest_expansion=ordered_expansions[0] if ordered_expansions else None,
            zone_count=len(zones),
            configured_dungeon_matches=list((result.get("matched") or {}).values()),
            ambiguous=result.get("ambiguous") or {},
            unmatched=result.get("unmatched") or [],
            season_zone_id=result.get("season_zone_id"),
            zone_inventory=inventory,
            note=(
                "zone_inventory lists every zone with its encounter names. In WCL a "
                "Mythic+ season is one zone whose encounters are the dungeons, so the "
                "season's dungeon list is an encounter list, not a zone list. Persist "
                "verified IDs with `wclmplus discover-dungeons --write`."
            ),
        )
        self._save_fixture("world_zones", data)

        if self.config is not None:
            unmatched = result.get("unmatched") or []
            if unmatched:
                self.findings.limitation(
                    f"{len(unmatched)} of {len(self.config.dungeons.dungeons)} configured "
                    f"dungeon name(s) matched no live zone or encounter: {unmatched}. "
                    "Check them against zone_inventory in this report and correct "
                    "config/dungeons.yml."
                )
            if result.get("ambiguous"):
                self.findings.limitation(
                    f"Ambiguous dungeon name(s) {sorted(result['ambiguous'])}: the same "
                    "name appears in more than one zone (usually an earlier season). "
                    "Disambiguate by hand before collecting."
                )

    def step_reports_probe(self, season_zone_id: int | None = None) -> None:
        """Test whether unscoped report discovery actually works.

        Introspection shows `ReportData.reports` takes `zoneID`, `gameZoneID`,
        `startTime`, `endTime`, `limit` and `page`, with `guildID` and
        `userID` **optional**. Whether the API honours a query with no guild
        or user scope decides how representative any sample can be: if it
        does, runs can be drawn broadly; if it does not, sampling is confined
        to seeded scopes and every bias that implies (brief section 8).

        Presence of the arguments proves nothing -- only a real call does.
        """
        info = self.introspector.type_info("ReportData")
        if info is None or not info.has("reports"):
            self.findings.record("reports_probe", "SKIPPED", reason="ReportData.reports is absent")
            return

        attempts: dict[str, Any] = {}
        for label, variables in (
            ("unscoped", {"zoneID": None, "gameZoneID": None, "limit": 3, "page": 1}),
            (
                "zone_scoped",
                {"zoneID": season_zone_id, "gameZoneID": None, "limit": 3, "page": 1},
            ),
        ):
            if label == "zone_scoped" and season_zone_id is None:
                attempts[label] = {
                    "status": "SKIPPED",
                    "reason": "no season zone ID resolved yet",
                }
                continue
            try:
                data = self.client.execute(
                    load_query("discover_reports_probe"),
                    variables,
                    kind=f"reports_probe_{label}",
                )
            except Exception as exc:  # noqa: BLE001
                # A rejection here is a finding, not a crash: it tells us the
                # scope is required.
                attempts[label] = {
                    "status": "REJECTED",
                    "error": f"{type(exc).__name__}: {exc}",
                    "variables": variables,
                }
                continue
            block = ((data.get("reportData") or {}).get("reports")) or {}
            rows = [r for r in (block.get("data") or []) if isinstance(r, dict)]
            attempts[label] = {
                "status": "OK",
                "variables": variables,
                "total": block.get("total"),
                "per_page": block.get("per_page"),
                "has_more_pages": block.get("has_more_pages"),
                "returned": len(rows),
                "example_codes": [r.get("code") for r in rows[:3]],
                "example_zones": [r.get("zone") for r in rows[:3]],
            }

        usable = [k for k, v in attempts.items() if v.get("status") == "OK" and v.get("returned")]
        self.findings.record(
            "reports_probe",
            "OK" if usable else "NO_USABLE_SCOPE",
            attempts=attempts,
            usable_scopes=usable,
            note=(
                "A working unscoped or zone-scoped query means broad sampling is "
                "possible. If both are rejected, discovery is limited to seeded "
                "scopes (character, guild) or a manual report list, and the "
                "resulting bias must be recorded with every sample."
            ),
        )
        if not usable:
            self.findings.limitation(
                "Report discovery returned nothing usable without a guild or user "
                "scope. Representative season-wide sampling is therefore not "
                "available through this path; use --report-list or seeded sources, "
                "and record uploader/guild bias."
            )

    # -- orchestration ----------------------------------------------------

    def run(self, report_code: str | None = None) -> ReconFindings:
        if not self.step_auth():
            self.findings.limitation(
                "Authentication failed, so no live schema evidence could be gathered."
            )
            self.finish()
            return self.findings

        self.step_rate_limit()
        self.step_schema_types()
        self.step_field_verification()
        self.step_discovery()
        self.step_zones()
        self.step_reports_probe(
            (self.findings.steps.get("world_zones") or {}).get("season_zone_id")
        )

        if report_code is None:
            self.findings.record(
                "report_probes",
                "SKIPPED",
                reason="no report code supplied",
                note=(
                    "Schema-level Gate A questions are answered, but pulls, NPC identity, "
                    "event shape and pagination need one public Mythic+ report: "
                    "`wclmplus recon --report <CODE>`."
                ),
            )
            self.findings.limitation(
                "No report was inspected: pull boundaries, NPC instance identity, event "
                "shape and pagination semantics remain unverified."
            )
            self.finish()
            return self.findings

        report = self.step_report_metadata(report_code)
        if report is None:
            self.finish()
            return self.findings

        fights = self.step_fights(report_code)
        self.step_master_data(report_code)
        self.step_query_cost(report_code)

        if not fights:
            self.findings.limitation("No fight was available to probe pulls or events.")
            self.finish()
            return self.findings

        fight = fights[0]
        fight_id = fight.get("id")
        if isinstance(fight_id, int):
            self.step_dungeon_pulls(report_code, fight_id)
        self.step_event_samples(report_code, fight)
        self.step_pagination_probe(report_code, fight)

        self.finish()
        return self.findings

    def finish(self) -> None:
        self.findings.finished_at = time.time()
        self.findings.steps["api_usage"] = {
            "status": "OK",
            **self.client.stats.summary(),
            "rate_limit_after": self.client.rate_limiter.state.summary(),
        }
        self.write_outputs()

    # -- output -----------------------------------------------------------

    def write_outputs(self) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.output_dir / "recon_findings.json"
        json_path.write_text(
            json.dumps(self.findings.to_json(), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        md_path = self.output_dir / "RECON_REPORT.md"
        md_path.write_text(self.render_markdown(), encoding="utf-8")
        logger.info("Wrote %s and %s", json_path, md_path)
        return json_path, md_path

    def render_markdown(self) -> str:
        f = self.findings
        lines: list[str] = [
            "# Phase 0 recon report",
            "",
            f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(f.started_at))}",
            f"- Software: {f.provenance.get('software_version')} "
            f"(commit {f.provenance.get('git_commit') or 'unknown'}"
            f"{', dirty tree' if f.provenance.get('git_dirty') else ''})",
            f"- Query version: {f.provenance.get('query_version')}",
            "",
            "## Step results",
            "",
            "| Step | Status |",
            "| --- | --- |",
        ]
        for name, detail in f.steps.items():
            lines.append(f"| `{name}` | {detail.get('status')} |")

        usage = f.steps.get("api_usage", {})
        lines += [
            "",
            "## API usage for this run",
            "",
            f"- Requests: {usage.get('requests')}",
            f"- Cache hits: {usage.get('cache_hits')}",
            f"- Retries: {usage.get('retries')}",
            f"- Bytes received: {usage.get('bytes_received')}",
            f"- Seconds spent: {usage.get('seconds_spent')}",
        ]

        pagination = f.steps.get("pagination_probe", {})
        if pagination:
            lines += [
                "",
                "## Pagination semantics (measured, not assumed)",
                "",
                f"- Status: {pagination.get('status')}",
                f"- Cursor semantics: {pagination.get('cursor_semantics', 'not determined')}",
                f"- Boundary events repeated across pages: "
                f"{pagination.get('boundary_events_repeated', 'n/a')}",
                f"- Full traversal: {pagination.get('full_traversal', 'not attempted')}",
                "- Events after de-duplication: "
                f"{pagination.get('total_events_deduplicated', 'n/a')}",
            ]

        fields = f.steps.get("field_verification", {})
        if fields:
            lines += ["", "## Field verification", ""]
            for type_name in ("Report", "ReportFight", "ReportDungeonPull", "ReportDungeonPullNPC"):
                block = fields.get(type_name)
                if isinstance(block, dict):
                    present = block.get("present") or {}
                    absent = block.get("absent") or []
                    lines.append(f"### {type_name}")
                    lines.append("")
                    lines.append(
                        f"- Present ({len(present)}): {', '.join(sorted(present)) or 'none'}"
                    )
                    lines.append(f"- Absent ({len(absent)}): {', '.join(absent) or 'none'}")
                    lines.append("")
            enum_block = fields.get("EventDataType") or {}
            if enum_block:
                lines += [
                    "### EventDataType",
                    "",
                    f"- Live values: {', '.join(enum_block.get('live_values') or []) or 'none'}",
                    f"- Wanted but unsupported: "
                    f"{', '.join(enum_block.get('wanted_but_unsupported') or []) or 'none'}",
                    "",
                ]

        lines += ["", "## Limitations and unresolved questions", ""]
        if f.limitations:
            lines += [f"{i}. {text}" for i, text in enumerate(f.limitations, start=1)]
        else:
            lines.append("None recorded.")

        lines += [
            "",
            "## Fixtures written",
            "",
        ]
        lines += [f"- `{path}`" for path in f.fixtures] or ["None."]
        lines += [
            "",
            "---",
            "",
            "Full machine-readable evidence: `recon_findings.json` in this directory.",
            "Copy the confirmed behaviour into `API_NOTES.md`; that file is the "
            "authoritative record for later phases.",
            "",
        ]
        return "\n".join(lines)
