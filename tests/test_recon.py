"""Recon integration tests against the synthetic API simulator.

These prove the Phase 0 control flow works end to end -- introspection,
schema-safe query generation, pull/NPC identity detection, event sampling,
pagination measurement, and report writing -- without a network or
credentials. They do not prove anything about the *real* schema; only
`wclmplus recon` against the live API can do that.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from wcl_simulator import DUPLICATE_NPC_GAME_ID, REPORT_CODE, WclSimulator

from wcl_mplus.client import GraphQLClient
from wcl_mplus.configs import ProjectConfig
from wcl_mplus.ratelimit import RateLimiter
from wcl_mplus.rawcache import RawCache
from wcl_mplus.recon import Recon

TOKEN = "simulator-token-aaaaaaaaaaaaaa"


class FakeTokenProvider:
    def auth_header(self):
        return {"Authorization": f"Bearer {TOKEN}"}

    def token(self, force_refresh=False):
        from wcl_mplus.auth import Token

        return Token(value=TOKEN, expires_at=9e18)

    def close(self):
        pass


@pytest.fixture
def recon_factory(settings, tmp_path):
    def build(simulator=None, *, config=None):
        sim = simulator or WclSimulator()
        client = GraphQLClient(
            settings,
            token_provider=FakeTokenProvider(),
            rate_limiter=RateLimiter(min_points_reserve=0),
            cache=RawCache(settings.raw_cache_dir),
            http_client=sim.client(),
            sleep=lambda s: None,
        )
        recon = Recon(
            client,
            settings,
            config=config,
            output_dir=tmp_path / "recon_out",
            write_fixtures=False,
        )
        return recon, sim

    return build


# -- full run --------------------------------------------------------------


def test_full_recon_run_records_every_step(recon_factory):
    recon, _ = recon_factory()
    findings = recon.run(report_code=REPORT_CODE)
    steps = findings.steps
    for expected in (
        "auth",
        "rate_limit",
        "schema_types",
        "field_verification",
        "discovery_paths",
        "world_zones",
        "report_metadata",
        "fights",
        "master_data",
        "dungeon_pulls",
        "event_samples",
        "pagination_probe",
        "api_usage",
    ):
        assert expected in steps, f"missing recon step {expected}"
    assert steps["auth"]["status"] == "OK"
    assert steps["field_verification"]["status"] == "OK"


def test_recon_writes_both_report_files(recon_factory, tmp_path):
    recon, _ = recon_factory()
    recon.run(report_code=REPORT_CODE)
    json_path = tmp_path / "recon_out" / "recon_findings.json"
    md_path = tmp_path / "recon_out" / "RECON_REPORT.md"
    assert json_path.is_file() and md_path.is_file()
    payload = json.loads(json_path.read_text())
    assert payload["steps"]["auth"]["status"] == "OK"
    assert "Pagination semantics (measured, not assumed)" in md_path.read_text()


def test_recon_never_records_the_token(recon_factory, tmp_path):
    recon, _ = recon_factory()
    recon.run(report_code=REPORT_CODE)
    for path in (tmp_path / "recon_out").rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(), f"token leaked into {path.name}"


# -- schema-driven behaviour ----------------------------------------------


def test_mplus_fields_are_verified_against_the_schema(recon_factory):
    recon, _ = recon_factory()
    findings = recon.run(report_code=REPORT_CODE)
    fight = findings.steps["field_verification"]["ReportFight"]
    for field in ("keystoneLevel", "keystoneAffixes", "keystoneTime", "countReached"):
        assert field in fight["present"], f"{field} should be reported present"
    assert fight["absent"] == []


def test_absent_fields_are_reported_not_requested(recon_factory):
    """When the schema lacks a field, it becomes a limitation, not a query."""
    sim = WclSimulator(drop_fields={"ReportFight": {"keystoneAffixes", "rating"}})
    recon, sim = recon_factory(sim)
    findings = recon.run(report_code=REPORT_CODE)

    block = findings.steps["field_verification"]["ReportFight"]
    assert set(block["absent"]) == {"keystoneAffixes", "rating"}
    assert any("keystoneAffixes" in text for text in findings.limitations)

    fight_queries = [q for q, _ in sim.queries_seen if "ReportFights" in q]
    assert fight_queries, "a fights query was sent"
    assert "keystoneAffixes" not in fight_queries[0], "absent field must not be queried"
    assert "keystoneLevel" in fight_queries[0], "present fields are still queried"


def test_missing_type_is_reported_with_similar_candidates(recon_factory):
    sim = WclSimulator(missing_types={"ReportDungeonPull"})
    recon, _ = recon_factory(sim)
    findings = recon.run(report_code=REPORT_CODE)
    assert findings.steps["schema_types"]["status"] == "PARTIAL"
    assert "ReportDungeonPull" in findings.steps["schema_types"]["expected_missing_with_candidates"]
    assert any("ReportDungeonPull" in text for text in findings.limitations)
    assert findings.steps["dungeon_pulls"]["status"] == "SKIPPED"


def test_unsupported_event_type_is_recorded(recon_factory):
    sim = WclSimulator(event_data_types=["Casts", "Deaths"])
    recon, _ = recon_factory(sim)
    findings = recon.run(report_code=REPORT_CODE)
    enum_block = findings.steps["field_verification"]["EventDataType"]
    assert "Dispels" in enum_block["wanted_but_unsupported"]
    assert any("does not accept" in text for text in findings.limitations)


def test_config_event_types_are_checked_against_live_enum(recon_factory):
    sim = WclSimulator(event_data_types=["Casts", "Deaths"])
    recon, _ = recon_factory(sim, config=ProjectConfig.load())
    findings = recon.run(report_code=REPORT_CODE)
    problems = findings.steps["field_verification"]["config_event_type_problems"]
    assert "mechanics" in problems
    assert "Dispels" in problems["mechanics"]


# -- Mythic+ identification ------------------------------------------------


def test_mythic_plus_fight_is_identified(recon_factory):
    recon, _ = recon_factory()
    findings = recon.run(report_code=REPORT_CODE)
    fights = findings.steps["fights"]
    assert fights["total_fights"] == 2
    assert fights["mythic_plus_fights"] == 1, "the non-keystone fight is excluded"
    assert fights["keystone_levels"] == [10]
    assert fights["mplus_example"]["keystoneLevel"] == 10


# -- NPC instance identity (brief section 14) ------------------------------


def test_duplicate_npc_species_pull_is_detected(recon_factory):
    """Recon must find the pull usable as the instance-identity fixture."""
    recon, _ = recon_factory()
    findings = recon.run(report_code=REPORT_CODE)
    candidates = findings.steps["dungeon_pulls"]["duplicate_npc_species_candidates"]
    assert candidates, "the two-copies pull should be flagged"
    assert any(c["gameID"] == DUPLICATE_NPC_GAME_ID for c in candidates)
    assert any(c.get("instance_span", 0) > 1 for c in candidates)


def test_instance_identity_fields_are_requested(recon_factory):
    recon, sim = recon_factory()
    recon.run(report_code=REPORT_CODE)
    pull_queries = [q for q, _ in sim.queries_seen if "dungeonPulls" in q]
    assert pull_queries
    for field in ("minimumInstanceID", "maximumInstanceID", "minimumInstanceGroupID"):
        assert field in pull_queries[0], f"{field} is required to separate NPC copies"


def test_missing_instance_fields_raise_a_limitation(recon_factory):
    sim = WclSimulator(
        drop_fields={"ReportDungeonPullNPC": {"minimumInstanceID", "maximumInstanceID"}}
    )
    recon, _ = recon_factory(sim)
    findings = recon.run(report_code=REPORT_CODE)
    assert any("minimumInstanceID" in text for text in findings.limitations)


# -- events and pagination -------------------------------------------------


def test_event_samples_record_observed_shape(recon_factory):
    recon, _ = recon_factory()
    findings = recon.run(report_code=REPORT_CODE)
    categories = findings.steps["event_samples"]["categories"]
    assert categories["Casts"]["status"] == "OK"
    assert "sourceInstance" in categories["Casts"]["observed_field_names"]
    assert categories["Dispels"]["observed_field_names"], "dispel shape recorded"
    assert "extraAbilityGameID" in categories["Dispels"]["observed_field_names"]


def test_pagination_semantics_are_measured_as_inclusive(recon_factory):
    recon, _ = recon_factory(WclSimulator(event_page_limit=25, inclusive_cursor=True))
    findings = recon.run(report_code=REPORT_CODE)
    probe = findings.steps["pagination_probe"]
    assert probe["status"] == "OK"
    assert "inclusive" in probe["cursor_semantics"]
    assert probe["boundary_events_repeated"] >= 1, "a repeat proves dedupe is load-bearing"
    assert probe["full_traversal"] == "OK"


def test_pagination_semantics_are_measured_as_exclusive(recon_factory):
    recon, _ = recon_factory(WclSimulator(event_page_limit=25, inclusive_cursor=False))
    findings = recon.run(report_code=REPORT_CODE)
    probe = findings.steps["pagination_probe"]
    assert "exclusive" in probe["cursor_semantics"]
    assert probe["boundary_events_repeated"] == 0


def test_full_traversal_recovers_every_event_exactly_once(recon_factory):
    sim = WclSimulator(event_page_limit=10, inclusive_cursor=True, total_cast_events=60)
    recon, sim = recon_factory(sim)
    findings = recon.run(report_code=REPORT_CODE)
    probe = findings.steps["pagination_probe"]
    assert probe["total_events_deduplicated"] == len(sim.cast_events)


def test_single_page_probe_is_flagged_as_unproven(recon_factory):
    sim = WclSimulator(event_page_limit=1000, total_cast_events=3)
    recon, _ = recon_factory(sim)
    findings = recon.run(report_code=REPORT_CODE)
    assert findings.steps["pagination_probe"]["status"] == "SINGLE_PAGE"
    assert any("not exercised across pages" in t for t in findings.limitations)


# -- discovery and zones ---------------------------------------------------


def test_discovery_paths_are_probed(recon_factory):
    recon, _ = recon_factory()
    findings = recon.run(report_code=REPORT_CODE)
    probes = findings.steps["discovery_paths"]["probes"]
    assert probes["ReportData.reports"]["present"] is True
    assert "guildID" in probes["ReportData.reports"]["args"]
    assert probes["Character.recentReports"]["present"] is True


def test_zone_step_matches_dungeons_as_encounters(recon_factory):
    recon, _ = recon_factory(config=ProjectConfig.load())
    findings = recon.run(report_code=REPORT_CODE)
    zones = findings.steps["world_zones"]
    matched = {m["display_name"] for m in zones["configured_dungeon_matches"]}
    assert {"Murder Row", "Ruby Life Pools", "The Blinding Vale", "Den of Nalorakk"} <= matched
    assert zones["unmatched"] == []
    assert zones["season_zone_id"] == 44


def test_zone_step_records_the_full_inventory(recon_factory):
    """Findings must carry zone and encounter names.

    Without them, identifying a season's dungeons needs a second run or a file
    off the user's disk.
    """
    recon, _ = recon_factory(config=ProjectConfig.load())
    findings = recon.run(report_code=REPORT_CODE)
    inventory = findings.steps["world_zones"]["zone_inventory"]
    season = next(z for z in inventory if z["id"] == 44)
    names = {e["name"] for e in season["encounters"]}
    assert "Murder Row" in names
    assert len(names) == 8, "all eight season dungeons visible without a second run"


def test_newest_expansion_is_reported_not_truncated_away(recon_factory):
    """Regression: the API returns expansions newest first.

    Taking the last six entries reported the six OLDEST expansions and hid the
    current one, which made the season look absent from the API entirely.
    """
    recon, _ = recon_factory(config=ProjectConfig.load())
    findings = recon.run(report_code=REPORT_CODE)
    zones = findings.steps["world_zones"]
    assert zones["newest_expansion"]["name"] == "Midnight"
    assert zones["expansions"][0]["id"] == 7
    assert len(zones["expansions"]) == 8, "no expansion dropped from the report"


def test_unmatched_dungeon_names_become_a_limitation(recon_factory, tmp_path):
    import yaml

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "dungeons.yml").write_text(
        yaml.safe_dump(
            {
                "season": {"id": "midnight-s2"},
                "dungeons": [{"key": "nowhere", "display_name": "Nowhere At All"}],
            }
        )
    )
    for name in ("sampling.yml", "hotfix_epochs.yml"):
        (config_dir / name).write_text(
            (pathlib.Path("config") / name).read_text(encoding="utf-8"), encoding="utf-8"
        )

    recon, _ = recon_factory(config=ProjectConfig.load(config_dir))
    findings = recon.run(report_code=REPORT_CODE)
    assert findings.steps["world_zones"]["unmatched"] == ["Nowhere At All"]
    assert any("matched no live zone or encounter" in x for x in findings.limitations)


# -- failure handling ------------------------------------------------------


def test_unknown_report_is_recorded_as_a_finding(recon_factory):
    import httpx

    def handler(request):
        body = json.loads(request.content.decode())
        query = body.get("query", "")
        if "__schema" in query or "__type(" in query:
            return WclSimulator().handler(request)
        if "rateLimitData" in query or "worldData" in query:
            return WclSimulator().handler(request)
        return httpx.Response(200, json={"data": {"reportData": {"report": None}}})

    from wcl_mplus.settings import Settings

    recon, _ = recon_factory()
    recon.client._http = httpx.Client(transport=httpx.MockTransport(handler))
    findings = recon.run(report_code="NoSuchReport12345")
    assert findings.steps["report_metadata"]["status"] == "FAILED"
    assert any("may be private" in t for t in findings.limitations)
    assert isinstance(Settings, type)


def test_recon_without_report_code_still_answers_schema_questions(recon_factory):
    recon, _ = recon_factory()
    findings = recon.run(report_code=None)
    assert findings.steps["field_verification"]["status"] == "OK"
    assert findings.steps["report_probes"]["status"] == "SKIPPED"
    assert any("No report was inspected" in t for t in findings.limitations)


def test_auth_failure_short_circuits_cleanly(recon_factory):
    from wcl_mplus.auth import AuthError

    recon, _ = recon_factory()

    class BadTokens:
        def auth_header(self):
            raise AuthError("bad credentials")

        def token(self, force_refresh=False):
            raise AuthError("bad credentials")

        def close(self):
            pass

    recon.client.tokens = BadTokens()
    findings = recon.run(report_code=REPORT_CODE)
    assert findings.steps["auth"]["status"] == "FAILED"
    assert any("Authentication failed" in t for t in findings.limitations)
    assert findings.finished_at is not None, "the report is still written"


# -- composite field sub-selections (live failure regression) --------------


def test_report_metadata_succeeds_with_composite_fields(recon_factory):
    """Regression for the first live recon run.

    `archiveStatus` was selected without a sub-selection, so the live API
    rejected the whole query with `Field "archiveStatus" of type
    "ReportArchiveStatus" must have a sub selection.` That aborted recon before
    pulls, events or pagination were ever probed.
    """
    recon, sim = recon_factory()
    findings = recon.run(report_code=REPORT_CODE)

    assert findings.steps["report_metadata"]["status"] == "OK", findings.steps[
        "report_metadata"
    ].get("error")

    sent = [q for q, _ in sim.queries_seen if "ReportMetadata" in q]
    assert sent, "a metadata query was sent"
    assert "archiveStatus {" in sent[0], "composite field carries a sub-selection"
    assert "isArchived" in sent[0], "sub-selection derived from introspection"


def test_sub_selections_are_derived_not_hardcoded(recon_factory):
    """A composite field absent from any hardcoded map still gets expanded."""
    recon, sim = recon_factory()
    recon.run(report_code=REPORT_CODE)
    fight_queries = [q for q, _ in sim.queries_seen if "ReportFights" in q]
    assert fight_queries
    assert "gameZone {" in fight_queries[0]
    assert "friendlyPlayers" in fight_queries[0], "a scalar list needs no sub-selection"
    assert "friendlyPlayers {" not in fight_queries[0]


def test_simulator_rejects_a_bare_composite_field():
    """Proves the guard fires, rather than always passing."""
    import httpx

    sim = WclSimulator()
    response = httpx.Client(transport=httpx.MockTransport(sim.handler)).post(
        "https://example.test/api",
        json={"query": 'query Q { reportData { report(code: "X") { code archiveStatus } } }'},
    )
    errors = response.json()["errors"]
    assert "must have a sub selection" in errors[0]["message"]


def test_recon_continues_past_metadata_to_pulls_and_events(recon_factory):
    """The steps the live failure blocked now run."""
    recon, _ = recon_factory()
    findings = recon.run(report_code=REPORT_CODE)
    for step in ("fights", "master_data", "dungeon_pulls", "event_samples", "pagination_probe"):
        assert step in findings.steps, f"{step} should be reached"
        assert findings.steps[step]["status"] != "FAILED", findings.steps[step]


# -- report discovery capability ------------------------------------------


def test_reports_probe_records_a_working_scope(recon_factory):
    """Argument presence proves nothing; only a real call does."""
    recon, _ = recon_factory(config=ProjectConfig.load())
    findings = recon.run(report_code=REPORT_CODE)
    probe = findings.steps["reports_probe"]
    assert probe["status"] == "OK"
    assert "unscoped" in probe["usable_scopes"]
    assert probe["attempts"]["unscoped"]["returned"] == 3
    assert probe["attempts"]["zone_scoped"]["variables"]["zoneID"] == 44


def test_reports_probe_records_a_required_scope_as_a_limitation(recon_factory):
    """A rejection is a finding about sampling reach, not a crash."""
    recon, _ = recon_factory(
        WclSimulator(unscoped_reports_allowed=False), config=ProjectConfig.load()
    )
    findings = recon.run(report_code=REPORT_CODE)
    probe = findings.steps["reports_probe"]
    assert probe["attempts"]["unscoped"]["status"] == "REJECTED"
    assert "guildID" in probe["attempts"]["unscoped"]["error"]
    # A zone-scoped query still works, so sampling is not dead.
    assert probe["attempts"]["zone_scoped"]["status"] == "OK"
    assert probe["status"] == "OK"


def test_reports_probe_with_no_usable_scope_raises_a_limitation(recon_factory):
    recon, _ = recon_factory(WclSimulator(unscoped_reports_allowed=False))
    # Without config there is no season zone, so only the unscoped attempt runs.
    findings = recon.run(report_code=REPORT_CODE)
    probe = findings.steps["reports_probe"]
    assert probe["status"] == "NO_USABLE_SCOPE"
    assert probe["attempts"]["zone_scoped"]["status"] == "SKIPPED"
    assert any("without a guild or user scope" in x for x in findings.limitations)
