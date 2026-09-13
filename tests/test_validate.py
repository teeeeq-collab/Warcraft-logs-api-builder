"""Validation-report tests.

A validation report is only useful if it reports what could *not* be done as
loudly as what could, and if its central claim -- that two copies of one NPC
stay separate -- is demonstrated from the data rather than asserted.
"""

from __future__ import annotations

import json

import pytest
from wcl_simulator import REPORT_CODE, WclSimulator

from wcl_mplus.auth import Token
from wcl_mplus.client import GraphQLClient
from wcl_mplus.collect import Collector
from wcl_mplus.configs import ProjectConfig
from wcl_mplus.db import Database
from wcl_mplus.ratelimit import RateLimiter
from wcl_mplus.rawcache import RawCache
from wcl_mplus.reportsource import ManualReportSource
from wcl_mplus.validate import (
    collect_validation,
    npc_instance_evidence,
    render_markdown,
    write_reports,
)


class FakeTokens:
    def auth_header(self):
        return {"Authorization": "Bearer simulator-token-aaaaaaaa"}

    def token(self, force_refresh=False):
        return Token("simulator-token-aaaaaaaa", 9e18)

    def close(self):
        pass


@pytest.fixture
def populated(settings, tmp_path):
    """A database with one collected run."""

    def build(simulator=None):
        sim = simulator or WclSimulator(event_page_limit=25)
        client = GraphQLClient(
            settings,
            token_provider=FakeTokens(),
            rate_limiter=RateLimiter(min_points_reserve=0),
            cache=RawCache(settings.raw_cache_dir),
            http_client=sim.client(),
            sleep=lambda s: None,
        )
        db = Database(tmp_path / "validate.sqlite")
        Collector(client, db, ProjectConfig.load(), page_limit=25).collect(
            list(ManualReportSource.from_iterable([REPORT_CODE], seed="t").discover())
        )
        return db

    return build


def test_report_covers_corpus_events_and_pages(populated):
    report = collect_validation(populated())
    assert report["runs"]["analysable"] >= 1
    assert report["events"]["total"] > 0
    assert report["events"]["assigned_pct"] is not None
    assert report["pages"]["total"] > 0
    assert report["pages"]["failed"] == 0
    assert report["pages"]["incomplete_streams"] == 0


def test_npc_instance_identity_is_demonstrated_not_asserted(populated):
    """The report must show two copies of one NPC as separate rows."""
    report = collect_validation(populated())
    evidence = report["npc_instance_evidence"]
    assert evidence, "no per-copy timeline was reconstructed"

    item = evidence[0]
    assert item["distinct_copies"] >= 2
    instances = {c["instance"] for c in item["per_copy"]}
    assert len(instances) >= 2
    first_casts = {c["first_cast_ms_into_pull"] for c in item["per_copy"]}
    assert len(first_casts) >= 2, "the copies have independent timelines"


def test_markdown_says_so_when_no_duplicate_npc_pull_exists(populated):
    """Absent evidence must read as absent, not as success."""
    db = populated()
    db.execute("UPDATE events SET source_instance = NULL")
    db.conn.commit()
    report = collect_validation(db)
    assert report["npc_instance_evidence"] == []
    text = render_markdown(report)
    assert "No pull in this corpus contained two copies" in text
    assert "untested here" in text


def test_limitations_are_reported(populated):
    report = collect_validation(populated())
    text = " ".join(report["limitations"])
    assert "unclassified" in text, "undeclared hotfix epochs must be flagged"
    assert "realm" in text, "the player-identity collision limitation is stated"


def test_unassigned_events_appear_as_a_limitation(populated):
    sim = WclSimulator(event_page_limit=25)
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
    report = collect_validation(populated(sim))
    assert report["events"]["unassigned"] >= 1
    assert any("outside every Warcraft Logs pull" in text for text in report["limitations"])


def test_null_instance_events_are_flagged_not_assumed(populated):
    db = populated()
    db.execute(
        "UPDATE events SET source_instance = NULL "
        "WHERE hostility='Enemies' AND type='cast' AND rowid IN "
        "(SELECT rowid FROM events WHERE hostility='Enemies' AND type='cast' LIMIT 3)"
    )
    db.conn.commit()
    report = collect_validation(db)
    assert any("never defaulted" in text for text in report["limitations"])


def test_incomplete_runs_are_flagged_for_exclusion(populated):
    db = populated()
    db.execute("UPDATE dungeon_runs SET collection_status = 'partial-run'")
    db.conn.commit()
    report = collect_validation(db)
    assert report["runs"]["analysable"] == 0
    assert any("event-frequency denominator" in text for text in report["limitations"])


def test_unknown_actors_and_abilities_are_counted(populated):
    """Master data and the event stream disagreeing is worth knowing."""
    db = populated()
    db.execute("DELETE FROM abilities")
    db.conn.commit()
    report = collect_validation(db)
    assert report["events"]["unknown_abilities"] > 0


def test_both_report_files_are_written(populated, tmp_path):
    report = collect_validation(populated())
    json_path, md_path = write_reports(report, tmp_path / "out")
    assert json_path.is_file() and md_path.is_file()
    assert json.loads(json_path.read_text())["events"]["total"] > 0
    text = md_path.read_text()
    for heading in (
        "# Validation report",
        "## Corpus",
        "## Events",
        "## Pagination",
        "## NPC instance identity",
        "## Known limitations",
    ):
        assert heading in text


def test_report_contains_no_player_names(populated, tmp_path):
    report = collect_validation(populated())
    _, md_path = write_reports(report, tmp_path / "out")
    blob = json.dumps(report) + md_path.read_text()
    for name in ("Tankadin", "Brewhealz", "SomeUploader"):
        assert name not in blob


def test_dungeon_filter_narrows_the_report(populated):
    db = populated()
    assert collect_validation(db, dungeon_key="murder-row")["runs"]["analysable"] >= 1
    assert collect_validation(db, dungeon_key="ruby-life-pools")["runs"]["analysable"] == 0


def test_cost_profile_is_measured_per_stream(populated):
    """Cost must be reported per event stream, so the expensive one is visible."""
    report = collect_validation(populated())
    cost = report["cost"]

    assert cost["total_pages"] > 0
    assert cost["by_stream"], "no stream cost rows"
    # Pages must reconcile with the pagination section rather than be recomputed
    # from a different rule.
    assert sum(int(r["pages"]) for r in cost["by_stream"]) == cost["total_pages"]
    for row in cost["by_stream"]:
        assert row["pages_per_run"] is not None
        assert int(row["events"] or 0) >= 0
    # Every number here is biased in a known direction; the report must say so.
    assert len(cost["measurement_caveats"]) >= 3


def test_cost_profile_survives_a_corpus_with_no_pagination(settings, tmp_path):
    """A run whose streams each fit in one page must not produce a bogus rate."""
    from wcl_mplus.db import Database, migrate

    db = Database(tmp_path / "empty.sqlite")
    migrate(db)
    cost = collect_validation(db)["cost"]
    assert cost["total_pages"] == 0
    assert cost["mean_seconds_per_run"] is None
    assert cost["mean_seconds_per_page"] is None


def test_markdown_reports_cost(populated):
    text = render_markdown(collect_validation(populated()))
    assert "## Cost and throughput" in text
    assert "Seconds per page (mean)" in text


def test_evidence_never_shows_events_outside_a_pull(populated):
    """Regression: unassigned events must not crowd out the real demonstration.

    `pull_id` sorts NULL-first in SQLite, so ordering the evidence query by it
    handed the report the ~2% of events that fall outside every pull -- the one
    slice that has no pull-relative clock. The demonstration the corpus rests on
    was showing null timings for that reason alone, not because instance
    identity had failed.
    """
    db = populated()
    row = db.execute(
        "SELECT page_id, run_id, report_code, source_id, ability_game_id "
        "  FROM events WHERE type = 'cast' AND hostility = 'Enemies' LIMIT 1"
    ).fetchone()
    assert row is not None, "fixture has no enemy casts to build on"

    start = db.scalar("SELECT MAX(seq_in_page) FROM events WHERE page_id = ?", (row["page_id"],))
    # Two copies of one NPC casting outside every pull: unassigned, untimed,
    # and -- before the fix -- first in the ordering.
    for offset, instance in enumerate((41, 42), start=1):
        db.execute(
            "INSERT INTO events (page_id, seq_in_page, run_id, report_code, pull_id, "
            " data_type, hostility, rel_ms, abs_ms, run_rel_ms, pull_rel_ms, type, "
            " source_id, source_instance, ability_game_id, normalizer_version) "
            "VALUES (?, ?, ?, ?, NULL, 'Casts', 'Enemies', 1, 1, 1, NULL, 'cast', ?, ?, ?, 1)",
            (
                row["page_id"],
                int(start) + offset,
                row["run_id"],
                row["report_code"],
                row["source_id"],
                instance,
                row["ability_game_id"],
            ),
        )
    db.conn.commit()

    evidence = npc_instance_evidence(db)
    assert evidence, "evidence disappeared entirely"
    for example in evidence:
        assert example["pull_id"] is not None
        for copy in example["per_copy"]:
            assert copy["first_cast_ms_into_pull"] is not None
            assert copy["last_cast_ms_into_pull"] is not None


def test_evidence_prefers_examples_that_show_a_recast(populated):
    """A copy casting twice proves more than ten copies casting once."""
    db = populated()
    evidence = npc_instance_evidence(db)
    if len(evidence) > 1:
        peaks = [max(int(c["casts"]) for c in ev["per_copy"]) for ev in evidence]
        assert peaks == sorted(peaks, reverse=True)
