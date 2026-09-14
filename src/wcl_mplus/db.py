"""SQLite storage: migrations, connection handling, and batched writes.

Design notes that are not obvious:

* **Migrations are numbered SQL files applied in order and recorded**, so a
  database can always state which schema version produced it. That matters for
  a research corpus: a row's meaning is only defined relative to a schema.
* **Writes are batched inside one transaction per unit of work.** A run's
  events arrive as thousands of rows; committing per row would dominate
  ingestion time.
* **Foreign keys are enforced.** SQLite leaves them off by default, and a
  research dataset with dangling run references is worse than one that fails
  loudly during ingest.
* **`INSERT OR REPLACE` is used only where re-ingest must be idempotent** --
  replacing a page's rows rather than appending to them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import RedactedError
from .settings import project_root

logger = logging.getLogger(__name__)

#: Schema version this code expects. Bumped by adding a migration file.
EXPECTED_SCHEMA_VERSION = 4


class DatabaseError(RedactedError):
    """Storage-layer failure."""


class SchemaVersionError(DatabaseError):
    """The database schema does not match what this code expects."""


def migrations_dir() -> Path:
    return project_root() / "migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Find numbered migration files, ordered by version.

    Files are named `<version>_<name>.sql`, e.g. `001_initial.sql`. A gap or a
    duplicate version is an error: applying migrations out of order would
    produce a schema nobody can reason about.
    """
    base = directory or migrations_dir()
    found: list[Migration] = []
    for path in sorted(base.glob("*.sql")):
        stem = path.stem
        head, _, name = stem.partition("_")
        try:
            version = int(head)
        except ValueError:
            logger.warning("Ignoring migration file with no version prefix: %s", path.name)
            continue
        found.append(Migration(version=version, name=name or stem, path=path))

    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise DatabaseError(f"Duplicate migration version(s) in {base}: {versions}")
    for index, version in enumerate(versions, start=1):
        if version != index:
            raise DatabaseError(f"Migration versions must run 1..N with no gaps; found {versions}")
    return found


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection with the pragmas this project depends on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if read_only and path.exists():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        # WAL keeps reads working while a long ingest is writing.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return []
    return [
        r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]


def migrate(conn: sqlite3.Connection, *, directory: Path | None = None) -> list[int]:
    """Apply any pending migrations. Returns the versions applied."""
    pending = [
        m for m in discover_migrations(directory) if m.version not in set(applied_versions(conn))
    ]
    applied: list[int] = []
    for migration in pending:
        logger.info("Applying migration %03d_%s", migration.version, migration.name)
        try:
            with conn:
                conn.executescript(migration.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, time.time()),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Migration {migration.version:03d}_{migration.name} failed: {exc}"
            ) from None
        applied.append(migration.version)
    return applied


def schema_version(conn: sqlite3.Connection) -> int:
    versions = applied_versions(conn)
    return max(versions) if versions else 0


class Database:
    """A migrated SQLite database with the project's write helpers."""

    def __init__(self, path: Path, *, auto_migrate: bool = True, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self.conn = connect(self.path, read_only=read_only)
        if auto_migrate and not read_only:
            migrate(self.conn)
        version = schema_version(self.conn)
        if version != EXPECTED_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database at {self.path} is at schema version {version}, but this "
                f"code expects {EXPECTED_SCHEMA_VERSION}. Run migrations, or use a "
                "database built by a matching version."
            )

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One transaction per unit of work; rolls back on any exception."""
        try:
            with self.conn:
                yield self.conn
        except sqlite3.Error as exc:
            raise DatabaseError(f"Transaction failed: {exc}") from None

    # -- writes -----------------------------------------------------------

    def upsert(self, table: str, row: dict[str, Any], *, replace: bool = True) -> None:
        """Insert one row, replacing an existing one with the same key."""
        self.upsert_many(table, [row], replace=replace)

    def upsert_many(
        self, table: str, rows: Sequence[dict[str, Any]], *, replace: bool = True
    ) -> int:
        """Insert many rows in one statement. Returns the count written."""
        if not rows:
            return 0
        columns = list(rows[0])
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        sql = (
            f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"
        )
        values = [tuple(row.get(col) for col in columns) for row in rows]
        try:
            self.conn.executemany(sql, values)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Insert into {table} failed: {exc}") from None
        return len(values)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, tuple(params))
        except sqlite3.Error as exc:
            raise DatabaseError(f"Query failed: {exc}") from None

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.execute(sql, params).fetchone()
        return None if row is None else row[0]

    # -- diagnostics ------------------------------------------------------

    def diagnostic(
        self,
        kind: str,
        detail: str,
        *,
        severity: str = "warning",
        run_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Record an ingest observation. Diagnostics are data, not logging.

        A validation report is only trustworthy if the things it could not do
        are written down next to the things it could.
        """
        self.upsert(
            "ingest_diagnostics",
            {
                "job_id": job_id,
                "run_id": run_id,
                "kind": kind,
                "severity": severity,
                "detail": detail,
                "created_at": time.time(),
            },
        )

    # -- stats ------------------------------------------------------------

    def table_counts(self) -> dict[str, int]:
        tables = [
            r["name"]
            for r in self.query(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {name: int(self.scalar(f"SELECT COUNT(*) FROM {name}") or 0) for name in tables}

    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


def json_or_none(value: Any) -> str | None:
    """Serialize a structure for a JSON column, or None if there is nothing."""
    if value is None:
        return None
    if isinstance(value, (list, dict)) and not value:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
