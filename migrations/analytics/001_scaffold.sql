-- ---------------------------------------------------------------------------
-- Analytical store, migration 001: scaffolding only.
--
-- This database holds DERIVED data and nothing else. Every row in it can be
-- recomputed from the ingest store; deleting the file costs compute and no
-- information. That is what makes the layer boundary real rather than a
-- convention -- an earlier draft of the architecture asserted that analysis
-- never writes to collection truth while putting derived tables in the same
-- file, which cannot both be true.
--
-- SQLite cannot enforce foreign keys across attached databases, so a `run_id`
-- here does not reference `dungeon_runs`. The replacement is stronger for this
-- purpose: every derivation records the corpus fingerprint it read, and
-- `analytics verify` recomputes it. A foreign key proves a run exists; a
-- fingerprint proves its evidence is unchanged, which is the failure that
-- would actually invalidate a conclusion.
--
-- The model tables (build dimensions, canonical actions, archetypes, states,
-- cohorts) arrive in later migrations. This one establishes identity and
-- provenance, so nothing can be derived without recording what it was derived
-- from.
-- ---------------------------------------------------------------------------

CREATE TABLE analytics_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  REAL NOT NULL
);

-- Which ingest corpus this store derives from. One row: an analytical database
-- belongs to exactly one corpus, and pointing it at another is a mistake worth
-- refusing rather than silently mixing two populations.
CREATE TABLE analytics_source (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    source_db_path      TEXT NOT NULL,
    identity_scheme_version INTEGER NOT NULL,
    identity_salt_fingerprint TEXT NOT NULL,
    bound_at            REAL NOT NULL
);

-- One row per derivation pass. Everything derived points back at one of these,
-- so "which code, reading which evidence, produced this row" is always
-- answerable without inference.
CREATE TABLE derivations (
    derivation_id       TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,          -- builds|actions|archetypes|states|cohorts
    started_at          REAL NOT NULL,
    finished_at         REAL,
    status              TEXT NOT NULL,          -- running|complete|failed
    corpus_fingerprint  TEXT NOT NULL,
    fingerprint_grade   TEXT NOT NULL,          -- page|deep
    fingerprint_version INTEGER NOT NULL,
    run_count           INTEGER NOT NULL,
    dedupe_policy       TEXT NOT NULL,          -- permissive|strict
    provisional         INTEGER NOT NULL DEFAULT 0,
    model_versions      TEXT NOT NULL,          -- JSON
    software_version    TEXT NOT NULL,
    normalizer_version  INTEGER NOT NULL,
    query_version       INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL,
    notes               TEXT
);
CREATE INDEX idx_derivations_kind ON derivations (kind, started_at);

-- Per-run digests as they stood when a derivation read them. This is what lets
-- `verify` say WHICH runs drifted rather than only that something did.
CREATE TABLE derivation_runs (
    derivation_id  TEXT NOT NULL REFERENCES derivations (derivation_id),
    run_id         TEXT NOT NULL,
    run_digest     TEXT NOT NULL,
    PRIMARY KEY (derivation_id, run_id)
);
CREATE INDEX idx_derivation_runs_run ON derivation_runs (run_id);

-- Runs a derivation deliberately left out, and why. An exclusion that is not
-- recorded is indistinguishable from a run that never existed.
CREATE TABLE derivation_exclusions (
    derivation_id  TEXT NOT NULL REFERENCES derivations (derivation_id),
    run_id         TEXT NOT NULL,
    reason         TEXT NOT NULL,   -- duplicate|missing_stream|unclassified|filtered
    detail         TEXT,
    PRIMARY KEY (derivation_id, run_id, reason)
);
