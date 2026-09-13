"""End-to-end collection tests against the synthetic API.

The properties under test are the ones a research corpus depends on:
resume must be exact, re-ingest must change nothing, events must map onto
Warcraft Logs' own pull boundaries, and two copies of one NPC must keep
separate timelines.
"""

from __future__ import annotations

import httpx
import pytest
from wcl_simulator import DUPLICATE_NPC_GAME_ID, REPORT_CODE, WclSimulator

from wcl_mplus.auth import Token
from wcl_mplus.client import GraphQLClient
from wcl_mplus.collect import Collector
from wcl_mplus.configs import ProjectConfig
from wcl_mplus.db import Database
from wcl_mplus.ratelimit import RateLimiter
from wcl_mplus.rawcache import RawCache
from wcl_mplus.reportsource import ManualReportSource


class FakeTokens:
    def auth_header(self):
        return {"Authorization": "Bearer simulator-token-aaaaaaaa"}

    def token(self, force_refresh=False):
        return Token("simulator-token-aaaaaaaa", 9e18)

    def close(self):
        pass


@pytest.fixture
def pipeline(settings, tmp_path):
    """A collector wired to a fresh database and a simulator."""

    def build(simulator=None, *, db_name="test.sqlite", page_limit=25):
        sim = simulator or WclSimulator(event_page_limit=25)
        client = GraphQLClient(
            settings,
            token_provider=FakeTokens(),
            rate_limiter=RateLimiter(min_points_reserve=0),
            cache=RawCache(settings.raw_cache_dir),
            http_client=sim.client(),
            sleep=lambda s: None,
        )
        db = Database(tmp_path / db_name)
        collector = Collector(client, db, ProjectConfig.load(), page_limit=page_limit)
        return collector, db, sim

    return build


def candidates():
    return list(ManualReportSource.from_iterable([REPORT_CODE], seed="test").discover())


# -- end to end ------------------------------------------------------------


def test_full_collection_populates_every_table(pipeline):
    collector, db, _ = pipeline()
    result = collector.collect(candidates(), event_profile="mechanics")

    assert result.reports_completed == 1
    assert result.reports_failed == 0
    counts = db.table_counts()
    for table in (
        "reports",
        "dungeon_runs",
        "pulls",
        "pull_npcs",
        "actors",
        "abilities",
        "players",
        "run_players",
        "event_pages",
        "events",
        "collection_jobs",
        "report_provenance",
    ):
        assert counts[table] > 0, f"{table} is empty"


def test_run_metadata_is_stored_with_derived_fields(pipeline):
    collector, db, _ = pipeline()
    collector.collect(candidates())
    run = db.query("SELECT * FROM dungeon_runs")[0]

    assert run["keystone_level"] == 10
    assert run["key_bracket"] == "10"
    assert run["dungeon_key"] == "murder-row"
    assert run["collection_status"] == "complete"
    assert run["timed"] == 1, "keystoneBonus > 0 means timed"
    assert run["duration_ms"] == run["rel_end_ms"] - run["rel_start_ms"]
    assert run["abs_start_ms"] > 1_700_000_000_000, "absolute time, not report-relative"
    assert run["hotfix_epoch"] == "unclassified"


def test_three_timestamp_bases_are_consistent(pipeline):
    """Report-, run- and pull-relative times must agree with each other."""
    collector, db, _ = pipeline()
    collector.collect(candidates())

    row = db.query(
        "SELECT e.rel_ms, e.abs_ms, e.run_rel_ms, e.pull_rel_ms, r.rel_start_ms AS run_start,"
        "       r.abs_start_ms AS run_abs, p.rel_start_ms AS pull_start "
        "  FROM events e "
        "  JOIN dungeon_runs r ON r.run_id = e.run_id "
        "  JOIN pulls p ON p.pull_id = e.pull_id LIMIT 1"
    )[0]
    assert row["run_rel_ms"] == row["rel_ms"] - row["run_start"]
    assert row["pull_rel_ms"] == row["rel_ms"] - row["pull_start"]
    assert row["abs_ms"] - row["rel_ms"] == row["run_abs"] - row["run_start"]


# -- the core requirement --------------------------------------------------


def test_two_copies_of_one_npc_keep_separate_timelines(pipeline):
    """The fact every recast statistic in this project rests on.

    Merging two copies of a species would invent recasts that never happened:
    two mobs casting once each would read as one mob casting twice.
    """
    collector, db, _ = pipeline()
    collector.collect(candidates())

    rows = db.query(
        "SELECT source_instance, COUNT(*) AS casts, MIN(rel_ms) AS first_cast "
        "  FROM events "
        " WHERE data_type = 'Casts' AND hostility = 'Enemies' AND type = 'cast' "
        "   AND source_id = 42 "
        " GROUP BY source_instance ORDER BY source_instance"
    )
    instances = {r["source_instance"]: r for r in rows}
    assert set(instances) >= {1, 2}, "both copies present as distinct instances"
    assert instances[1]["first_cast"] != instances[2]["first_cast"], (
        "the two copies have independent timelines"
    )
    assert instances[1]["casts"] > 0 and instances[2]["casts"] > 0


def test_pull_npc_multiplicity_is_derived_with_confidence(pipeline):
    collector, db, _ = pipeline()
    collector.collect(candidates())

    row = db.query(
        "SELECT instance_count, instance_count_confidence, min_instance_id, max_instance_id "
        "  FROM pull_npcs WHERE npc_game_id = ? AND instance_count > 1 LIMIT 1",
        (DUPLICATE_NPC_GAME_ID,),
    )[0]
    assert row["instance_count"] == row["max_instance_id"] - row["min_instance_id"] + 1
    assert row["instance_count_confidence"] == "inferred", (
        "a count derived from a range is never reported as exact"
    )


def test_missing_source_instance_is_not_defaulted(pipeline):
    """A null instance means 'the API did not say', never 'copy 1'."""
    collector, db, _ = pipeline()
    collector.collect(candidates())

    nulls = db.scalar(
        "SELECT COUNT(*) FROM events WHERE data_type='Casts' AND hostility='Friendlies' "
        "AND source_instance IS NULL"
    )
    assert nulls > 0, "player casts carry no instance and must stay NULL"


# -- hostility split -------------------------------------------------------


def test_enemy_and_friendly_casts_are_fetched_separately(pipeline):
    """Unfiltered casts are dominated by players; the split is what reveals NPCs."""
    collector, db, sim = pipeline()
    collector.collect(candidates(), event_profile="mechanics")

    hostilities = {
        r["hostility"]
        for r in db.query("SELECT DISTINCT hostility FROM events WHERE data_type = 'Casts'")
    }
    assert hostilities == {"Enemies", "Friendlies"}

    enemy_sources = {
        r["source_id"]
        for r in db.query(
            "SELECT DISTINCT source_id FROM events WHERE data_type='Casts' AND hostility='Enemies'"
        )
    }
    friendly_sources = {
        r["source_id"]
        for r in db.query(
            "SELECT DISTINCT source_id FROM events "
            "WHERE data_type='Casts' AND hostility='Friendlies'"
        )
    }
    assert enemy_sources and friendly_sources
    assert not (enemy_sources & friendly_sources), "the two streams are disjoint"


# -- pull assignment -------------------------------------------------------


def test_events_are_assigned_to_wcl_pulls(pipeline):
    collector, db, _ = pipeline()
    result = collector.collect(candidates())

    assigned = db.scalar("SELECT COUNT(*) FROM events WHERE pull_id IS NOT NULL")
    total = db.scalar("SELECT COUNT(*) FROM events")
    assert assigned > 0
    assert result.runs[0].assignment["events"] == total

    # Every assigned event must actually lie inside its pull's window.
    bad = db.scalar(
        "SELECT COUNT(*) FROM events e JOIN pulls p ON p.pull_id = e.pull_id "
        " WHERE e.rel_ms < p.rel_start_ms OR e.rel_ms > p.rel_end_ms"
    )
    assert bad == 0


def test_events_outside_pulls_are_kept_not_dropped(pipeline):
    """Between-pull events are where movement and out-of-combat deaths live."""
    sim = WclSimulator(event_page_limit=25)
    # Push one event far past the last pull's end.
    sim.cast_events.append(
        {
            "timestamp": 9_000_000,
            "type": "cast",
            "sourceID": 42,
            "sourceInstance": 1,
            "abilityGameID": 372107,
            "fight": 7,
        }
    )
    collector, db, _ = pipeline(sim)
    result = collector.collect(candidates())

    orphan = db.query("SELECT pull_id, pull_rel_ms FROM events WHERE rel_ms = 9000000")
    assert orphan, "the event was retained"
    assert orphan[0]["pull_id"] is None
    assert orphan[0]["pull_rel_ms"] is None, "no pull, no pull-relative time"
    assert result.runs[0].assignment["unassigned_after_last_pull"] >= 1

    note = db.query("SELECT detail FROM ingest_diagnostics WHERE kind = 'events_outside_pulls'")
    assert note, "unassigned events are reported, not silently tolerated"


# -- idempotency and resume ------------------------------------------------


def test_reingest_is_idempotent(pipeline):
    """Running the same job twice must change no row counts."""
    collector, db, _ = pipeline()
    collector.collect(candidates())
    before = db.table_counts()

    collector.collect(candidates())
    after = db.table_counts()

    for table, count in before.items():
        if table in ("collection_jobs", "ingest_diagnostics"):
            continue  # a second job is legitimately a second row
        assert after[table] == count, f"{table} changed on re-ingest: {count} -> {after[table]}"


def test_forced_refresh_rebuilds_events(pipeline):
    collector, db, _ = pipeline()
    collector.collect(candidates())
    run_id = db.query("SELECT run_id FROM dungeon_runs")[0]["run_id"]
    before = db.scalar("SELECT COUNT(*) FROM events")

    removed = collector.reset_run_events(run_id)
    assert removed == before
    assert db.scalar("SELECT COUNT(*) FROM events") == 0

    collector.collect(candidates())
    assert db.scalar("SELECT COUNT(*) FROM events") == before


def test_interrupted_collection_resumes_exactly(pipeline, settings, tmp_path):
    """Kill collection mid-stream, resume, and land on the same data.

    The checkpoint is the `event_pages` table: a page row is written in the
    same transaction as its events, so the last recorded page is by
    construction the last one whose events survived.
    """
    # Reference: an uninterrupted run.
    reference_collector, reference_db, _ = pipeline(db_name="reference.sqlite")
    reference_collector.collect(candidates())
    expected_events = reference_db.scalar("SELECT COUNT(*) FROM events")
    expected_pages = reference_db.scalar("SELECT COUNT(*) FROM event_pages")

    # Now a run that dies after a few event pages.
    sim = WclSimulator(event_page_limit=25)
    calls = {"events": 0}
    real_handler = sim.handler

    def flaky(request):
        import json as _json

        body = _json.loads(request.content.decode())
        if "events(" in body.get("query", ""):
            calls["events"] += 1
            if calls["events"] > 3:
                raise httpx.ConnectError("simulated network drop")
        return real_handler(request)

    client = GraphQLClient(
        settings,
        token_provider=FakeTokens(),
        rate_limiter=RateLimiter(min_points_reserve=0),
        cache=RawCache(tmp_path / "nocache"),
        http_client=httpx.Client(transport=httpx.MockTransport(flaky)),
        sleep=lambda s: None,
    )
    db = Database(tmp_path / "resumed.sqlite")
    broken = Collector(client, db, ProjectConfig.load(), page_limit=25)
    broken.collect(candidates())

    partial_pages = db.scalar("SELECT COUNT(*) FROM event_pages")
    assert 0 < partial_pages < expected_pages, "collection really was interrupted"
    assert db.query("SELECT collection_status FROM dungeon_runs")[0][0] == "collection-failed"

    # Resume with a healthy client against the same database.
    healthy, _, _ = pipeline(db_name="resumed.sqlite")
    healthy.db = db
    resumed = Collector(healthy.client, db, ProjectConfig.load(), page_limit=25)
    resumed.collect(candidates())

    assert db.scalar("SELECT COUNT(*) FROM events") == expected_events
    assert db.scalar("SELECT COUNT(*) FROM event_pages") == expected_pages
    assert db.query("SELECT collection_status FROM dungeon_runs")[0][0] == "complete"

    # And no page was written twice.
    dupes = db.scalar(
        "SELECT COUNT(*) FROM (SELECT run_id, data_type, IFNULL(hostility,''), page_index, "
        "COUNT(*) c FROM event_pages GROUP BY 1,2,3,4 HAVING c > 1)"
    )
    assert dupes == 0


# -- failure handling ------------------------------------------------------


def test_archived_report_stores_metadata_but_no_events(pipeline):
    """An archived run must never enter an event-frequency denominator."""
    sim = WclSimulator()
    original = sim._report_metadata

    def archived():
        data = original()
        data["reportData"]["report"]["archiveStatus"] = {
            "isArchived": True,
            "isAccessible": False,
            "archiveDate": 123,
        }
        return data

    sim._report_metadata = archived
    collector, db, _ = pipeline(sim)
    collector.collect(candidates())

    assert db.query("SELECT collection_status FROM reports")[0][0] == "archived-events-unavailable"
    assert db.query("SELECT collection_status FROM dungeon_runs")[0][0] == (
        "archived-events-unavailable"
    )
    assert db.scalar("SELECT COUNT(*) FROM events") == 0
    assert db.query("SELECT detail FROM ingest_diagnostics WHERE kind='archived_report'")


def test_missing_report_is_recorded_not_crashed(pipeline):
    sim = WclSimulator()
    sim._report_metadata = lambda: {"reportData": {"report": None}}
    collector, db, _ = pipeline(sim)
    result = collector.collect(candidates())

    assert result.runs == []
    assert db.query("SELECT detail FROM ingest_diagnostics WHERE kind='report_unavailable'")


def test_provenance_is_recorded_for_every_discovery(pipeline):
    collector, db, _ = pipeline()
    collector.collect(candidates())
    row = db.query("SELECT * FROM report_provenance")[0]
    assert row["source_type"] == "manual"
    assert row["seed"] == "test"
    assert row["job_id"]


def test_a_second_source_adds_a_second_provenance_row(pipeline):
    """Provenance is per discovery event, not per report.

    A report found both by a manual list and by zone-scoped discovery keeps
    both records -- which is what makes uploader and leaderboard bias
    assessable after the fact. Only an exact repeat of the same discovery is
    suppressed.
    """
    collector, db, _ = pipeline()
    collector.collect(candidates())

    other = list(
        ManualReportSource.from_iterable([REPORT_CODE], seed="a-different-seed").discover()
    )
    collector.collect(other)

    seeds = {r["seed"] for r in db.query("SELECT seed FROM report_provenance")}
    assert seeds == {"test", "a-different-seed"}


def test_job_row_carries_reproducibility_stamps(pipeline):
    collector, db, _ = pipeline()
    collector.collect(candidates())
    job = db.query("SELECT * FROM collection_jobs")[0]
    assert job["status"] == "complete"
    assert job["software_version"] and job["config_hash"]
    assert job["normalizer_version"] and job["query_version"]
    assert job["finished_at"] >= job["started_at"]


def test_player_names_never_reach_the_database(pipeline):
    collector, db, _ = pipeline()
    collector.collect(candidates())
    names = {r["name"] for r in db.query("SELECT name FROM actors WHERE is_player = 1")}
    assert names
    assert all(n.startswith("player-") for n in names), names
    assert "Tankadin" not in str(names)
    # NPC names are research data and are kept.
    npcs = {r["name"] for r in db.query("SELECT name FROM actors WHERE is_player = 0")}
    assert "Shivan Punisher" in npcs


def test_roster_roles_are_resolved(pipeline):
    collector, db, _ = pipeline()
    collector.collect(candidates())
    roles = {r["role"] for r in db.query("SELECT role FROM run_players")}
    assert "tank" in roles and "healer" in roles


def test_reingest_preserves_duplicate_classification(pipeline):
    """Re-collecting must not silently undo `wclmplus dedupe`.

    normalize_run() writes duplicate_group_id and is_canonical as NULL -- it
    sees one fight and cannot know a corpus-wide pass ever ran -- and a run row
    is replaced wholesale on re-ingest. Without the collector carrying the
    classification across that replace, every re-collect reset the corpus to
    "never deduplicated" while reporting nothing.
    """
    collector, db, _ = pipeline()
    collector.collect(candidates())

    run_id = db.query("SELECT run_id FROM dungeon_runs")[0]["run_id"]
    db.execute(
        "UPDATE dungeon_runs SET duplicate_group_id = 'dup-test', is_canonical = 0 "
        "WHERE run_id = ?",
        (run_id,),
    )
    db.conn.commit()

    collector.collect(candidates())

    row = db.execute(
        "SELECT duplicate_group_id, is_canonical FROM dungeon_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert row["duplicate_group_id"] == "dup-test"
    assert row["is_canonical"] == 0
