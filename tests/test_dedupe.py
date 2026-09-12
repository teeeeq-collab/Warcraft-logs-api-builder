"""Duplicate-run detection tests.

The bias is deliberate: prefer false negatives. Leaving two records of one run
separate costs a little statistical power; merging two genuinely different runs
corrupts the data irrecoverably.
"""

from __future__ import annotations

from wcl_mplus.dedupe import RunFingerprint, compare, roster_overlap

ROSTER = frozenset({"p1", "p2", "p3", "p4", "p5"})


def fingerprint(
    run_id="A:1",
    report="A",
    start=1_000_000_000,
    duration=1_500_000,
    roster=ROSTER,
    key=10,
    dungeon="murder-row",
    affixes="[1,2,3]",
):
    return RunFingerprint(run_id, dungeon, key, start, duration, roster, affixes, report)


def test_two_uploads_of_one_run_match():
    """The case this exists for: one run, logged by two people."""
    match = compare(fingerprint(), fingerprint("B:1", "B", start=1_000_030_000))
    assert match is not None
    assert match.confidence >= 0.9
    assert any("roster overlap" in r for r in match.reasons)


def test_same_group_running_the_key_again_is_not_a_duplicate():
    """Same dungeon, same key, same five people -- but a real second run.

    Time is what separates them. Treating this as a duplicate would delete a
    genuine observation.
    """
    later = fingerprint("C:1", "C", start=1_000_000_000 + 40 * 60 * 1000)
    assert compare(fingerprint(), later) is None


def test_different_roster_is_not_a_duplicate():
    other = fingerprint("D:1", "D", start=1_000_030_000, roster=frozenset({"x1", "x2", "x3"}))
    assert compare(fingerprint(), other) is None


def test_different_key_level_or_dungeon_is_not_a_duplicate():
    assert compare(fingerprint(), fingerprint("E:1", "E", start=1_000_030_000, key=12)) is None
    assert (
        compare(
            fingerprint(), fingerprint("F:1", "F", start=1_000_030_000, dungeon="ruby-life-pools")
        )
        is None
    )


def test_two_fights_in_one_report_are_never_duplicates():
    """A report holds many runs; two of its fights are two runs."""
    assert compare(fingerprint(), fingerprint("A:2", "A", start=1_000_030_000)) is None


def test_partial_roster_still_matches():
    """One uploader may miss a player who joined late."""
    partial = fingerprint(
        "G:1", "G", start=1_000_030_000, roster=frozenset({"p1", "p2", "p3", "p4", "zz"})
    )
    assert compare(fingerprint(), partial) is not None


def test_roster_below_threshold_does_not_match():
    weak = fingerprint(
        "H:1", "H", start=1_000_030_000, roster=frozenset({"p1", "z2", "z3", "z4", "z5"})
    )
    assert compare(fingerprint(), weak) is None


def test_unknown_dungeon_never_matches():
    """Without a dungeon there is not enough to justify merging observations."""
    a = fingerprint(dungeon=None)
    b = fingerprint("B:1", "B", start=1_000_030_000, dungeon=None)
    assert compare(a, b) is None


def test_missing_roster_is_handled_without_crashing():
    a = fingerprint(roster=frozenset())
    b = fingerprint("B:1", "B", start=1_000_010_000, roster=frozenset())
    match = compare(a, b)
    assert match is not None
    assert any("roster unavailable" in r for r in match.reasons)


def test_roster_overlap_uses_the_smaller_roster():
    assert roster_overlap(frozenset({"a", "b"}), frozenset({"a", "b", "c", "d"})) == 1.0
    assert roster_overlap(frozenset(), frozenset({"a"})) == 0.0


def test_duplicate_grouping_is_transitive(tmp_path):
    """A matching B and B matching C makes one group of three."""
    from wcl_mplus.db import Database
    from wcl_mplus.dedupe import group_duplicates

    db = Database(tmp_path / "dupes.sqlite")
    for index, code in enumerate("ABC"):
        db.upsert(
            "reports",
            {
                "report_code": code,
                "start_time_ms": 0,
                "end_time_ms": 1,
                "retrieved_at": 0,
                "collection_status": "complete",
            },
        )
        db.upsert(
            "dungeon_runs",
            {
                "run_id": f"{code}:1",
                "report_code": code,
                "fight_id": 1,
                "dungeon_key": "murder-row",
                "keystone_level": 10,
                # 20 s apart each: A-B and B-C match directly, A-C by transitivity.
                "abs_start_ms": 1_000_000_000 + index * 20_000,
                "rel_start_ms": 0,
                "rel_end_ms": 1_500_000,
                "abs_end_ms": 1_001_500_000,
                "duration_ms": 1_500_000,
                "keystone_affixes": "[1,2,3]",
                "hotfix_epoch": "unclassified",
                "collection_status": "complete",
            },
        )
        for player in ROSTER:
            db.upsert("players", {"player_id": player, "first_seen": 0, "last_seen": 0})
            db.upsert(
                "run_players",
                {"run_id": f"{code}:1", "actor_id": hash(player) % 100, "player_id": player},
            )
    db.conn.commit()

    groups = group_duplicates(db)
    assert len(groups) == 1
    assert len(next(iter(groups.values()))) == 3

    # Nothing deleted; exactly one canonical member.
    assert db.scalar("SELECT COUNT(*) FROM dungeon_runs") == 3
    assert db.scalar("SELECT COUNT(*) FROM dungeon_runs WHERE is_canonical = 1") == 1
    assert db.scalar("SELECT COUNT(*) FROM ingest_diagnostics WHERE kind='duplicate_candidate'") > 0


def test_regrouping_is_authoritative_not_additive(tmp_path):
    """Re-running dedupe replaces previous grouping rather than layering on it."""
    from wcl_mplus.db import Database
    from wcl_mplus.dedupe import group_duplicates

    db = Database(tmp_path / "regroup.sqlite")
    db.upsert(
        "reports",
        {
            "report_code": "A",
            "start_time_ms": 0,
            "end_time_ms": 1,
            "retrieved_at": 0,
            "collection_status": "complete",
        },
    )
    db.upsert(
        "dungeon_runs",
        {
            "run_id": "A:1",
            "report_code": "A",
            "fight_id": 1,
            "dungeon_key": "murder-row",
            "keystone_level": 10,
            "abs_start_ms": 1,
            "rel_start_ms": 0,
            "rel_end_ms": 1,
            "abs_end_ms": 2,
            "duration_ms": 1,
            "hotfix_epoch": "unclassified",
            "collection_status": "complete",
            "duplicate_group_id": "stale-group",
        },
    )
    db.conn.commit()

    group_duplicates(db)
    row = db.query("SELECT duplicate_group_id, is_canonical FROM dungeon_runs")[0]
    assert row["duplicate_group_id"] is None, "the stale group was cleared"
    assert row["is_canonical"] == 1, "a run in no group is its own canonical observation"
