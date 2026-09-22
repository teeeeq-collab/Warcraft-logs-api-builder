"""The analytical store: a second database that holds only derived data.

Everything here can be recomputed from the ingest store. Deleting the file
costs compute and no information -- which is what makes the layer boundary a
fact rather than a convention. An earlier draft of the architecture asserted
that analysis never writes to collection truth while placing derived tables in
the same file; both could not be true, and this module is the correction.

Three properties it exists to guarantee:

* **The ingest store is opened read-only.** Not by discipline -- by SQLite's
  own `mode=ro`. A derivation that tried to write collection truth would fail
  rather than succeed quietly.

* **Nothing is derived without recording what it was derived from.** Every pass
  opens a `derivations` row carrying the corpus fingerprint, its grade, the
  dedupe policy, and every model version in force, and every run's digest as it
  stood when it was read.

* **A store belongs to one corpus.** It binds to the identity scheme of the
  database that created it and refuses another, because pseudonyms from two
  schemes never match and merging them silently produces one population that
  does not exist.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import Database, DatabaseError, SchemaVersionError, connect, migrate, migrations_dir
from .db import schema_version as _schema_version
from .fingerprint import CorpusFingerprint, corpus_fingerprint
from .redaction import RedactedError
from .sanitize import IDENTITY_SCHEME_VERSION, salt_fingerprint
from .version import (
    NORMALIZER_VERSION,
    QUERY_VERSION,
    SCHEMA_VERSION,
    SOFTWARE_VERSION,
)

logger = logging.getLogger(__name__)

#: Analytical schema version; matches the highest applied analytics migration.
ANALYTICS_SCHEMA_VERSION = 1

#: Ledger table inside the analytical store. Separate from the ingest store's
#: `schema_migrations` because the two schemas version independently.
ANALYTICS_LEDGER = "analytics_migrations"

#: Model versions, recorded on every derivation. A derived row must always be
#: distinguishable from one produced by different semantics.
MODEL_VERSIONS: dict[str, int | str] = {
    "build_model": 0,
    "action_model": 0,
    "archetype_model": 0,
    "state_schema": 0,
    "cohort_model": 0,
    "similarity_model": 0,
    "scl": "experimental-1",
}


class AnalyticsError(RedactedError):
    """The analytical store refused an operation."""


def analytics_migrations_dir() -> Path:
    return migrations_dir() / "analytics"


def default_analytics_path(source_db: Path) -> Path:
    """Where the analytical store for a given corpus lives by default.

    Beside the corpus rather than inside `data/db/`, so a `rm -rf` of the
    analytics directory is obviously safe and obviously does not touch
    collection truth.
    """
    source = Path(source_db)
    return source.parent.parent / "analytics" / f"{source.stem}.analysis.sqlite"


@dataclass
class DerivationRecord:
    """An open derivation pass."""

    derivation_id: str
    kind: str
    fingerprint: CorpusFingerprint
    dedupe_policy: str
    provisional: bool
    exclusions: list[tuple[str, str, str | None]] = field(default_factory=list)


@dataclass
class VerifyResult:
    """What `verify` found for one derivation."""

    derivation_id: str
    kind: str
    grade: str
    intact: bool
    drifted_runs: list[str]
    missing_runs: list[str]
    recorded_fingerprint: str
    current_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "derivation_id": self.derivation_id,
            "kind": self.kind,
            "grade": self.grade,
            "intact": self.intact,
            "drifted_runs": self.drifted_runs,
            "missing_runs": self.missing_runs,
            "recorded_fingerprint": self.recorded_fingerprint,
            "current_fingerprint": self.current_fingerprint,
        }


class AnalyticsStore:
    """A derived-data database bound to one ingest corpus."""

    def __init__(self, path: Path, source: Database, *, auto_migrate: bool = True) -> None:
        self.path = Path(path)
        self.source = source
        self.conn = connect(self.path)
        if auto_migrate:
            migrate(self.conn, directory=analytics_migrations_dir(), table=ANALYTICS_LEDGER)
        version = _schema_version(self.conn, table=ANALYTICS_LEDGER)
        if version != ANALYTICS_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Analytical store at {self.path} is at version {version}, but this "
                f"code expects {ANALYTICS_SCHEMA_VERSION}. Delete it and rebuild -- "
                "it holds only derived data."
            )
        self._bind()

    # -- lifecycle --------------------------------------------------------

    def _bind(self) -> None:
        """Claim this store for the source corpus, or refuse a different one.

        Pseudonyms are the only handle either database has on a person, so an
        analytical store built over one identity scheme cannot describe a corpus
        written under another: the same player would read as two, silently.
        """
        fingerprint = salt_fingerprint()
        row = self.conn.execute("SELECT * FROM analytics_source LIMIT 1").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO analytics_source (id, source_db_path, "
                "  identity_scheme_version, identity_salt_fingerprint, bound_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (str(self.source.path), IDENTITY_SCHEME_VERSION, fingerprint, time.time()),
            )
            self.conn.commit()
            return
        if (
            int(row["identity_scheme_version"]) != IDENTITY_SCHEME_VERSION
            or str(row["identity_salt_fingerprint"]) != fingerprint
        ):
            raise AnalyticsError(
                f"This analytical store was built over identity scheme "
                f"v{row['identity_scheme_version']} (salt {row['identity_salt_fingerprint']}), "
                f"but the corpus uses v{IDENTITY_SCHEME_VERSION} (salt {fingerprint}). "
                "Delete the analytical store and rebuild it -- it holds only derived data."
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> AnalyticsStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- derivations ------------------------------------------------------

    def begin(
        self,
        kind: str,
        run_ids: list[str],
        *,
        dedupe_policy: str,
        grade: str = "page",
        provisional: bool = False,
        notes: str | None = None,
    ) -> DerivationRecord:
        """Open a derivation pass over a named run set.

        The fingerprint is taken here, before any work, so it describes the
        evidence the derivation actually read rather than whatever the corpus
        looked like once it finished.
        """
        fingerprint = corpus_fingerprint(self.source, run_ids, grade=grade)
        record = DerivationRecord(
            derivation_id=uuid.uuid4().hex[:16],
            kind=kind,
            fingerprint=fingerprint,
            dedupe_policy=dedupe_policy,
            provisional=provisional,
        )
        with self.conn:
            self.conn.execute(
                "INSERT INTO derivations (derivation_id, kind, started_at, finished_at, status, "
                "  corpus_fingerprint, fingerprint_grade, fingerprint_version, run_count, "
                "  dedupe_policy, provisional, model_versions, software_version, "
                "  normalizer_version, query_version, schema_version, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.derivation_id,
                    kind,
                    time.time(),
                    None,
                    "running",
                    fingerprint.value,
                    fingerprint.grade,
                    fingerprint.fingerprint_version,
                    fingerprint.run_count,
                    dedupe_policy,
                    1 if provisional else 0,
                    json.dumps(MODEL_VERSIONS, sort_keys=True),
                    SOFTWARE_VERSION,
                    NORMALIZER_VERSION,
                    QUERY_VERSION,
                    SCHEMA_VERSION,
                    notes,
                ),
            )
            self.conn.executemany(
                "INSERT INTO derivation_runs (derivation_id, run_id, run_digest) VALUES (?,?,?)",
                [(record.derivation_id, r, d) for r, d in fingerprint.runs.items()],
            )
        return record

    def exclude(
        self, record: DerivationRecord, run_id: str, reason: str, detail: str | None = None
    ) -> None:
        """Record a run this derivation left out, and why.

        An exclusion that is not written down is indistinguishable from a run
        that never existed, which is how a filtered population turns into a
        claim about the whole one.
        """
        record.exclusions.append((run_id, reason, detail))
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO derivation_exclusions "
                "  (derivation_id, run_id, reason, detail) VALUES (?,?,?,?)",
                (record.derivation_id, run_id, reason, detail),
            )

    def finish(self, record: DerivationRecord, status: str = "complete") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE derivations SET finished_at = ?, status = ? WHERE derivation_id = ?",
                (time.time(), status, record.derivation_id),
            )

    # -- verification -----------------------------------------------------

    def verify(self, *, kind: str | None = None) -> list[VerifyResult]:
        """Recompute each derivation's fingerprint and report drift.

        This is the answer to "is this analysis still describing the corpus it
        was computed from" -- a command rather than an assumption. A drifted run
        is named, so the fix is a re-derivation of that run rather than of
        everything.
        """
        sql = "SELECT * FROM derivations WHERE status = 'complete'"
        params: tuple[Any, ...] = ()
        if kind is not None:
            sql += " AND kind = ?"
            params = (kind,)
        results: list[VerifyResult] = []
        for row in self.conn.execute(sql + " ORDER BY started_at", params).fetchall():
            recorded = {
                r["run_id"]: r["run_digest"]
                for r in self.conn.execute(
                    "SELECT run_id, run_digest FROM derivation_runs WHERE derivation_id = ?",
                    (row["derivation_id"],),
                )
            }
            current = corpus_fingerprint(
                self.source, list(recorded), grade=str(row["fingerprint_grade"])
            )
            present = {
                r["run_id"]
                for r in self.source.query(
                    "SELECT run_id FROM dungeon_runs WHERE run_id IN "
                    f"({','.join('?' for _ in recorded) or 'NULL'})",
                    tuple(recorded),
                )
            }
            missing = sorted(set(recorded) - present)
            drifted = sorted(
                r for r, d in recorded.items() if r in present and current.runs.get(r) != d
            )
            results.append(
                VerifyResult(
                    derivation_id=str(row["derivation_id"]),
                    kind=str(row["kind"]),
                    grade=str(row["fingerprint_grade"]),
                    intact=not drifted and not missing,
                    drifted_runs=drifted,
                    missing_runs=missing,
                    recorded_fingerprint=str(row["corpus_fingerprint"]),
                    current_fingerprint=current.value,
                )
            )
        return results


def open_analytics(
    source: Database, path: Path | None = None, *, auto_migrate: bool = True
) -> AnalyticsStore:
    """Open (creating if needed) the analytical store for a corpus."""
    target = Path(path) if path is not None else default_analytics_path(source.path)
    try:
        return AnalyticsStore(target, source, auto_migrate=auto_migrate)
    except DatabaseError:
        raise
