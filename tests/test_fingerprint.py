"""The fingerprint must change when the evidence changes, and not otherwise.

The point of these tests is the review's requirement, stated as a property:
changing the actual evidence must change the fingerprint. Each test mutates one
thing a corpus could plausibly differ by and asserts the fingerprint notices --
or, for the deliberate exclusions, that it does not.
"""

from __future__ import annotations

import pytest
from conftest import FakeTokens
from wcl_simulator import REPORT_CODE, WclSimulator

from wcl_mplus.client import GraphQLClient
from wcl_mplus.collect import Collector
from wcl_mplus.configs import ProjectConfig
from wcl_mplus.db import Database
from wcl_mplus.fingerprint import corpus_fingerprint, run_digest
from wcl_mplus.ratelimit import RateLimiter
from wcl_mplus.rawcache import RawCache
from wcl_mplus.reportsource import ManualReportSource


@pytest.fixture
def corpus(settings, tmp_path):
    sim = WclSimulator(event_page_limit=25)
    client = GraphQLClient(
        settings,
        token_provider=FakeTokens(),
        rate_limiter=RateLimiter(min_points_reserve=0),
        cache=RawCache(settings.raw_cache_dir),
        http_client=sim.client(),
        sleep=lambda s: None,
    )
    db = Database(tmp_path / "fp.sqlite")
    collector = Collector(client, db, ProjectConfig.load(), page_limit=25)
    collector.collect(list(ManualReportSource.from_iterable([REPORT_CODE], seed="t").discover()))
    yield db
    db.close()


def test_a_fingerprint_is_stable_across_recomputation(corpus):
    assert corpus_fingerprint(corpus).value == corpus_fingerprint(corpus).value
    assert corpus_fingerprint(corpus, grade="deep").value == (
        corpus_fingerprint(corpus, grade="deep").value
    )


def test_grades_are_different_fingerprints(corpus):
    page = corpus_fingerprint(corpus)
    deep = corpus_fingerprint(corpus, grade="deep")
    assert page.value != deep.value
    with pytest.raises(ValueError, match="grade"):
        page.matches(deep)


def test_run_order_cannot_change_the_fingerprint(corpus):
    """Naming the same runs in a different order must give the same answer."""
    first = corpus.scalar("SELECT run_id FROM dungeon_runs LIMIT 1")
    # A second run, so there is an order to vary. Its own evidence is empty,
    # which is irrelevant here: what is under test is the combination step.
    cols = [r[1] for r in corpus.query("PRAGMA table_info(dungeon_runs)")]
    selected = ", ".join(
        "run_id || ':clone'" if c == "run_id" else ("fight_id + 1000" if c == "fight_id" else c)
        for c in cols
    )
    corpus.execute(
        f"INSERT INTO dungeon_runs ({', '.join(cols)}) SELECT {selected} "
        "  FROM dungeon_runs WHERE run_id = ?",
        (first,),
    )
    corpus.conn.commit()
    runs = [r["run_id"] for r in corpus.query("SELECT run_id FROM dungeon_runs")]
    assert len(runs) > 1

    assert (
        corpus_fingerprint(corpus, runs).value
        == corpus_fingerprint(corpus, list(reversed(runs))).value
    )
    # And a differently-populated run set must not collide with the first.
    assert corpus_fingerprint(corpus, runs).value != corpus_fingerprint(corpus, [first]).value


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("event_count", 999),  # a page returned a different amount
        ("status", "failed"),  # a page that succeeded now has not
        ("next_cursor_ms", 123456),  # a stream that ended now continues
        ("raw_cache_path", "moved/elsewhere.json.gz"),  # a different payload
    ],
)
def test_changing_a_page_changes_the_fingerprint(corpus, column, value):
    before = corpus_fingerprint(corpus)
    page_id = corpus.scalar("SELECT page_id FROM event_pages ORDER BY page_id LIMIT 1")
    corpus.execute(f"UPDATE event_pages SET {column} = ? WHERE page_id = ?", (value, page_id))
    corpus.conn.commit()
    after = corpus_fingerprint(corpus)
    assert not before.matches(after), f"changing {column} left the fingerprint unchanged"
    assert before.drifted(after), "drift was not attributed to any run"


def test_changing_coverage_changes_the_fingerprint(corpus):
    """'asked, none' and 'never asked' are different evidence and must differ."""
    before = corpus_fingerprint(corpus)
    corpus.execute(
        "UPDATE run_stream_coverage SET status = 'partial' WHERE rowid = "
        "(SELECT rowid FROM run_stream_coverage LIMIT 1)"
    )
    corpus.conn.commit()
    assert not before.matches(corpus_fingerprint(corpus))


def test_removing_a_stream_from_coverage_changes_the_fingerprint(corpus):
    before = corpus_fingerprint(corpus)
    corpus.execute(
        "DELETE FROM run_stream_coverage WHERE rowid = "
        "(SELECT rowid FROM run_stream_coverage LIMIT 1)"
    )
    corpus.conn.commit()
    assert not before.matches(corpus_fingerprint(corpus))


def test_page_grade_does_not_read_events_but_deep_grade_does(corpus):
    """The honest limit of the cheap grade, pinned so it cannot be overclaimed.

    A payload whose content changed while its event count stayed the same is
    invisible to `page`. That is the whole reason `deep` exists, and why a
    published result is pinned with `deep`.
    """
    page_before = corpus_fingerprint(corpus)
    deep_before = corpus_fingerprint(corpus, grade="deep")

    # Same row count, different content.
    corpus.execute(
        "UPDATE events SET amount = IFNULL(amount, 0) + 1 WHERE event_id = "
        "(SELECT event_id FROM events ORDER BY event_id LIMIT 1)"
    )
    corpus.conn.commit()

    assert page_before.matches(corpus_fingerprint(corpus)), (
        "page grade claimed to detect a change it cannot see"
    )
    assert not deep_before.matches(corpus_fingerprint(corpus, grade="deep")), (
        "deep grade missed a changed event row"
    )


def test_drift_names_the_affected_run(corpus):
    before = corpus_fingerprint(corpus, grade="deep")
    run_id = corpus.scalar("SELECT run_id FROM events ORDER BY event_id LIMIT 1")
    corpus.execute(
        "UPDATE events SET amount = IFNULL(amount, 0) + 7 WHERE run_id = ? "
        "AND event_id = (SELECT MIN(event_id) FROM events WHERE run_id = ?)",
        (run_id, run_id),
    )
    corpus.conn.commit()
    drifted = before.drifted(corpus_fingerprint(corpus, grade="deep"))
    assert drifted == [run_id]


def test_field_boundaries_cannot_be_forged(corpus):
    """Length-prefixed fields: ('ab','c') must not hash like ('a','bc')."""
    import hashlib

    from wcl_mplus.fingerprint import _feed

    a, b = hashlib.sha256(), hashlib.sha256()
    _feed(a, "ab", "c")
    _feed(b, "a", "bc")
    assert a.hexdigest() != b.hexdigest()


def test_an_unknown_grade_is_refused(corpus):
    with pytest.raises(ValueError, match="grade"):
        corpus_fingerprint(corpus, grade="quick")
    run_id = corpus.scalar("SELECT run_id FROM dungeon_runs LIMIT 1")
    with pytest.raises(ValueError, match="grade"):
        run_digest(corpus, run_id, grade="quick")
