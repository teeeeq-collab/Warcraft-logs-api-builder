"""The query boundary's three rules, as properties rather than conventions.

A dedupe policy cannot be acquired by accident; a stream that was never
requested cannot produce a zero; and every result carries evidence depth rather
than one N.
"""

from __future__ import annotations

import pytest
from conftest import FakeTokens
from wcl_simulator import REPORT_CODE, WclSimulator

from wcl_mplus.analytics import AnalyticsStore, default_analytics_path
from wcl_mplus.client import GraphQLClient
from wcl_mplus.collect import Collector
from wcl_mplus.configs import ProjectConfig
from wcl_mplus.db import Database
from wcl_mplus.ratelimit import RateLimiter
from wcl_mplus.rawcache import RawCache
from wcl_mplus.reportsource import ManualReportSource
from wcl_mplus.repository import (
    DedupePolicy,
    IndependentUnit,
    Repository,
    RepositoryError,
    parse_stream,
)


@pytest.fixture
def repo(settings, tmp_path):
    sim = WclSimulator(event_page_limit=25)
    client = GraphQLClient(
        settings,
        token_provider=FakeTokens(),
        rate_limiter=RateLimiter(min_points_reserve=0),
        cache=RawCache(settings.raw_cache_dir),
        http_client=sim.client(),
        sleep=lambda s: None,
    )
    db = Database(tmp_path / "repo.sqlite")
    Collector(client, db, ProjectConfig.load(), page_limit=25).collect(
        list(ManualReportSource.from_iterable([REPORT_CODE], seed="t").discover())
    )
    yield Repository(db), db
    db.close()


# -- policy ----------------------------------------------------------------


def test_a_research_read_without_a_policy_is_refused(repo):
    """There is deliberately no default. Neither rule may be acquired silently."""
    repository, _ = repo
    with pytest.raises(RepositoryError, match="dedupe policy is required"):
        repository.runs("strict")  # a bare string is not a policy
    with pytest.raises(RepositoryError):
        repository.runs(None)


def test_strict_excludes_unclassified_runs_and_says_so(repo):
    """An undeduplicated corpus must not quietly pass for a canonical one."""
    repository, db = repo
    assert db.scalar("SELECT COUNT(*) FROM dungeon_runs WHERE is_canonical IS NULL") > 0

    permissive = repository.runs(DedupePolicy.PERMISSIVE)
    strict = repository.runs(DedupePolicy.STRICT)

    assert len(permissive) > 0
    assert permissive.provisional, "permissive must flag that dedupe never ran"
    assert len(strict) == 0
    assert strict.excluded_for("unclassified"), "strict excluded runs without saying why"
    assert not strict.provisional


def test_strict_admits_classified_runs(repo):
    repository, db = repo
    db.execute("UPDATE dungeon_runs SET is_canonical = 1")
    db.conn.commit()
    strict = repository.runs(DedupePolicy.STRICT)
    assert len(strict) > 0
    assert not strict.provisional
    assert strict.exclusions == []


def test_duplicates_are_excluded_under_both_policies(repo):
    repository, db = repo
    run_id = db.scalar("SELECT run_id FROM dungeon_runs LIMIT 1")
    db.execute(
        "UPDATE dungeon_runs SET is_canonical = 0, duplicate_group_id = 'g1' WHERE run_id = ?",
        (run_id,),
    )
    db.conn.commit()
    for policy in (DedupePolicy.PERMISSIVE, DedupePolicy.STRICT):
        run_set = repository.runs(policy)
        assert run_id not in run_set.run_ids
        assert run_id in run_set.excluded_for("duplicate")


# -- coverage --------------------------------------------------------------


def test_a_stream_never_requested_cannot_produce_a_zero(repo):
    """The failure this boundary exists to prevent, stated as a test."""
    repository, db = repo
    collected = repository.runs(DedupePolicy.PERMISSIVE, require_streams=["Casts@Enemies"])
    assert len(collected) > 0, "the simulator corpus collects enemy casts"

    # Healing is not in the `mechanics` profile, so no run ever asked for it.
    healing = repository.runs(DedupePolicy.PERMISSIVE, require_streams=["Healing@Friendlies"])
    assert len(healing) == 0, "a run contributed to a stream it never requested"
    assert healing.excluded_for("missing_stream")
    assert healing.required_streams == ["Healing@Friendlies"]


def test_a_partial_stream_does_not_count_as_covered(repo):
    """Only an exhausted stream licenses reading its zero as a real zero."""
    repository, db = repo
    db.execute("UPDATE run_stream_coverage SET status = 'partial' WHERE data_type = 'Casts'")
    db.conn.commit()
    run_set = repository.runs(DedupePolicy.PERMISSIVE, require_streams=["Casts@Enemies"])
    assert len(run_set) == 0
    assert run_set.excluded_for("missing_stream")


def test_coverage_report_counts_runs_with_each_stream(repo):
    repository, _ = repo
    run_set = repository.runs(DedupePolicy.PERMISSIVE)
    report = repository.coverage_report(run_set, ["Casts@Enemies", "Healing@Friendlies"])
    assert report["Casts@Enemies"]["with_stream"] == len(run_set)
    assert report["Healing@Friendlies"]["with_stream"] == 0


def test_parse_stream_handles_both_spellings():
    assert parse_stream("Casts@Enemies") == ("Casts", "Enemies")
    assert parse_stream("Deaths") == ("Deaths", None)


# -- evidence --------------------------------------------------------------


def test_evidence_reports_every_level_not_one_number(repo):
    repository, _ = repo
    run_set = repository.runs(DedupePolicy.PERMISSIVE)
    evidence = repository.evidence(run_set).as_dict()

    for key in (
        "n_events",
        "n_states",
        "n_pulls",
        "n_runs",
        "n_players",
        "n_reports",
        "n_parties",
    ):
        assert key in evidence, f"{key} missing from the evidence block"
    assert evidence["n_runs"] == len(run_set)
    assert evidence["n_events"] > 0
    assert evidence["n_players"] > 0
    assert evidence["dedupe_policy"] == "permissive"
    assert evidence["provisional"] is True


def test_the_independent_unit_decides_the_reported_n(repo):
    """A player-behaviour claim must not borrow an event count for its N."""
    repository, _ = repo
    run_set = repository.runs(DedupePolicy.PERMISSIVE)

    by_event = repository.evidence(run_set, independent_unit=IndependentUnit.OBSERVATION)
    by_player = repository.evidence(run_set, independent_unit=IndependentUnit.PLAYER)
    by_run = repository.evidence(run_set, independent_unit=IndependentUnit.RUN)

    assert by_event.n_independent == by_event.n_events
    assert by_player.n_independent == by_player.n_players
    assert by_run.n_independent == len(run_set)
    assert by_player.n_independent < by_event.n_independent, (
        "players must be scarcer than events, or the corpus is not what it claims"
    )


def test_concentration_is_reported(repo):
    """N=100,000 from one player is a lie of composition; show the composition."""
    repository, _ = repo
    run_set = repository.runs(DedupePolicy.PERMISSIVE)
    evidence = repository.evidence(run_set).as_dict()
    concentration = evidence["concentration"]
    assert concentration["top_run_share"] is not None
    assert 0 < concentration["top_run_share"] <= 1.0
    # One run in the simulator corpus: it must report that it supplies all of it.
    if len(run_set) == 1:
        assert concentration["top_run_share"] == 1.0


def test_evidence_carries_the_exclusions_forward(repo):
    repository, _ = repo
    run_set = repository.runs(DedupePolicy.PERMISSIVE, require_streams=["Healing@Friendlies"])
    evidence = repository.evidence(run_set).as_dict()
    assert evidence["coverage"]["required"] == ["Healing@Friendlies"]
    assert evidence["coverage"]["runs_excluded"] > 0
    assert evidence["n_runs"] == 0


# -- events ----------------------------------------------------------------


def test_events_stream_rather_than_materialise(repo):
    repository, _ = repo
    run_set = repository.runs(DedupePolicy.PERMISSIVE)
    stream = repository.iter_events(run_set, data_type="Casts", hostility="Enemies")
    first = next(stream, None)
    assert first is not None
    assert first["data_type"] == "Casts"
    assert first["hostility"] == "Enemies"
    assert sum(1 for _ in stream) >= 0


def test_an_empty_run_set_yields_no_events(repo):
    repository, _ = repo
    run_set = repository.runs(DedupePolicy.STRICT)  # everything unclassified
    assert list(repository.iter_events(run_set)) == []


# -- packs, relocated from the CLI ----------------------------------------


def test_packs_and_npc_lookup_moved_out_of_the_cli(repo):
    repository, db = repo
    packs = repository.packs(limit=5)
    assert packs, "no packs found in the simulator corpus"
    assert {"dungeon_key", "sig", "occurrences", "runs"} <= set(packs[0])

    npc_id = db.scalar("SELECT npc_game_id FROM pull_npcs LIMIT 1")
    pulls = repository.npc_pulls(int(npc_id), limit=5)
    assert pulls and pulls[0]["pull_id"]


# -- analytical store ------------------------------------------------------


def test_the_analytical_store_is_a_separate_file(repo, tmp_path):
    _, db = repo
    store = AnalyticsStore(default_analytics_path(db.path), db)
    assert store.path != db.path
    assert store.path.exists()
    store.close()


def test_a_derivation_records_what_it_read(repo, tmp_path):
    repository, db = repo
    store = AnalyticsStore(tmp_path / "a.sqlite", db)
    run_set = repository.runs(DedupePolicy.PERMISSIVE)

    record = store.begin(
        "archetypes", run_set.run_ids, dedupe_policy=run_set.policy.value, provisional=True
    )
    store.exclude(record, "someone", "missing_stream", "Healing@Friendlies")
    store.finish(record)

    row = store.conn.execute(
        "SELECT * FROM derivations WHERE derivation_id = ?", (record.derivation_id,)
    ).fetchone()
    assert row["status"] == "complete"
    assert row["corpus_fingerprint"]
    assert row["fingerprint_grade"] == "page"
    assert row["provisional"] == 1
    assert row["dedupe_policy"] == "permissive"
    runs = store.conn.execute(
        "SELECT COUNT(*) AS n FROM derivation_runs WHERE derivation_id = ?",
        (record.derivation_id,),
    ).fetchone()
    assert runs["n"] == len(run_set)
    store.close()


def test_verify_detects_drift_and_names_the_run(repo, tmp_path):
    repository, db = repo
    store = AnalyticsStore(tmp_path / "b.sqlite", db)
    run_set = repository.runs(DedupePolicy.PERMISSIVE)
    record = store.begin("states", run_set.run_ids, dedupe_policy="permissive", grade="deep")
    store.finish(record)

    assert all(r.intact for r in store.verify())

    run_id = db.scalar("SELECT run_id FROM events ORDER BY event_id LIMIT 1")
    db.execute(
        "UPDATE events SET amount = IFNULL(amount,0) + 1 "
        " WHERE event_id = (SELECT MIN(event_id) FROM events WHERE run_id = ?)",
        (run_id,),
    )
    db.conn.commit()

    results = store.verify()
    assert not results[0].intact
    assert results[0].drifted_runs == [run_id]
    store.close()


def test_the_store_refuses_a_corpus_from_another_identity_scheme(repo, tmp_path):
    _, db = repo
    store = AnalyticsStore(tmp_path / "c.sqlite", db)
    store.conn.execute("UPDATE analytics_source SET identity_salt_fingerprint = 'deadbeef'")
    store.conn.commit()
    store.close()

    from wcl_mplus.analytics import AnalyticsError

    with pytest.raises(AnalyticsError, match="identity scheme"):
        AnalyticsStore(tmp_path / "c.sqlite", db)
