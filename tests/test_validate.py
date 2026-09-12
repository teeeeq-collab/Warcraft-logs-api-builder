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
from wcl_mplus.validate import collect_validation, render_markdown, write_reports


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
