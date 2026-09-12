"""Storage-layer tests: migrations, constraints, and write helpers."""

from __future__ import annotations

import sqlite3

import pytest

from wcl_mplus.db import (
    Database,
    DatabaseError,
    SchemaVersionError,
    discover_migrations,
    json_or_none,
    migrate,
    schema_version,
)


def test_migrations_are_discovered_in_order():
    found = discover_migrations()
    assert found
    assert [m.version for m in found] == list(range(1, len(found) + 1))


def test_migration_gaps_are_rejected(tmp_path):
    """Applying migrations out of order produces a schema nobody can reason about."""
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "003_c.sql").write_text("SELECT 1;")
    with pytest.raises(DatabaseError, match="no gaps"):
        discover_migrations(tmp_path)


def test_duplicate_migration_versions_are_rejected(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "001_b.sql").write_text("SELECT 1;")
    with pytest.raises(DatabaseError, match="Duplicate migration"):
        discover_migrations(tmp_path)


def test_migrations_are_idempotent(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    assert schema_version(db.conn) == 1
    assert migrate(db.conn) == [], "nothing left to apply"
    db.close()

    again = Database(tmp_path / "a.sqlite")
    assert schema_version(again.conn) == 1


def test_schema_version_mismatch_is_refused(tmp_path, monkeypatch):
    """A database from another schema version must not be used silently."""
    db = Database(tmp_path / "b.sqlite")
    db.close()
    monkeypatch.setattr("wcl_mplus.db.EXPECTED_SCHEMA_VERSION", 99)
    with pytest.raises(SchemaVersionError, match="expects 99"):
        Database(tmp_path / "b.sqlite", auto_migrate=False)


def test_foreign_keys_are_enforced(tmp_path):
    """A research dataset with dangling run references is worse than a loud failure."""
    db = Database(tmp_path / "c.sqlite")
    with pytest.raises(DatabaseError):
        db.upsert(
            "pulls",
            {
                "pull_id": "orphan",
                "run_id": "does-not-exist",
                "wcl_pull_id": 1,
                "pull_index": 0,
                "rel_start_ms": 0,
                "rel_end_ms": 1,
                "run_rel_start_ms": 0,
                "run_rel_end_ms": 1,
                "abs_start_ms": 0,
                "abs_end_ms": 1,
                "duration_ms": 1,
            },
        )


def test_transaction_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    with pytest.raises(DatabaseError), db.transaction():
        db.upsert(
            "reports",
            {
                "report_code": "R",
                "start_time_ms": 0,
                "end_time_ms": 1,
                "retrieved_at": 0,
                "collection_status": "complete",
            },
        )
        db.execute("INSERT INTO reports (report_code) VALUES ('R')")  # duplicate PK
    assert db.scalar("SELECT COUNT(*) FROM reports") == 0, "the whole unit of work rolled back"


def test_upsert_many_writes_in_one_statement(tmp_path):
    db = Database(tmp_path / "e.sqlite")
    rows = [
        {
            "game_id": i,
            "name": f"a{i}",
            "icon": None,
            "type": None,
            "first_seen": 0.0,
            "last_seen": 0.0,
        }
        for i in range(500)
    ]
    assert db.upsert_many("abilities", rows) == 500
    db.conn.commit()
    assert db.scalar("SELECT COUNT(*) FROM abilities") == 500


def test_insert_or_ignore_preserves_the_original(tmp_path):
    db = Database(tmp_path / "f.sqlite")
    db.upsert("players", {"player_id": "p", "first_seen": 1.0, "last_seen": 1.0})
    db.upsert("players", {"player_id": "p", "first_seen": 9.0, "last_seen": 9.0}, replace=False)
    db.conn.commit()
    assert db.scalar("SELECT first_seen FROM players WHERE player_id = 'p'") == 1.0


def test_diagnostics_are_stored_as_data(tmp_path):
    db = Database(tmp_path / "g.sqlite")
    db.diagnostic("kind", "something happened", severity="warning")
    db.conn.commit()
    row = db.query("SELECT * FROM ingest_diagnostics")[0]
    assert row["kind"] == "kind" and row["severity"] == "warning"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ([], None), ({}, None), ([1, 2], "[1,2]"), ({"b": 1, "a": 2}, '{"a":2,"b":1}')],
)
def test_json_or_none(value, expected):
    assert json_or_none(value) == expected


def test_table_counts_covers_every_table(tmp_path):
    db = Database(tmp_path / "h.sqlite")
    counts = db.table_counts()
    for table in ("events", "pulls", "dungeon_runs", "reports"):
        assert table in counts
    assert all(isinstance(v, int) for v in counts.values())


def test_read_only_connection_refuses_writes(tmp_path):
    Database(tmp_path / "i.sqlite").close()
    db = Database(tmp_path / "i.sqlite", auto_migrate=False, read_only=True)
    with pytest.raises((DatabaseError, sqlite3.OperationalError)):
        db.execute("INSERT INTO players (player_id, first_seen, last_seen) VALUES ('x',0,0)")
