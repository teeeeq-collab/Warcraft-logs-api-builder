-- ---------------------------------------------------------------------------
-- run_stream_coverage: what was ASKED FOR, per run, not only what came back.
--
-- The problem this solves has no other solution. An analysis that finds zero
-- Healing rows for a run cannot tell, from the events alone, whether nobody
-- healed or whether Healing was never requested. Both look identical: an
-- absence. As soon as one corpus mixes collection profiles -- which it will,
-- the moment a mechanics corpus and a coaching import share a database -- every
-- statistic over a partially collected stream is silently wrong, and wrong in
-- the direction of "this never happens", which is the most believable kind of
-- wrong.
--
-- A row here is written when a stream is REQUESTED, before it is known whether
-- any events exist. `events` = 0 with `status` = 'ok' therefore means "asked,
-- genuinely none"; no row at all means "never asked". Derived analyses must
-- consult this table before using a run.
--
-- `source_id` / `target_id` record actor narrowing. A stream collected for one
-- focus player is not the same stream collected for the party, and must never
-- be pooled with it.
-- ---------------------------------------------------------------------------

CREATE TABLE run_stream_coverage (
    coverage_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL REFERENCES dungeon_runs (run_id),
    data_type           TEXT NOT NULL,
    hostility           TEXT,                   -- Enemies|Friendlies|NULL
    collection_profile  TEXT NOT NULL,
    scope               TEXT NOT NULL,          -- all|focus
    source_id           INTEGER,                -- actor narrowing, NULL = unnarrowed
    target_id           INTEGER,
    requested_start_ms  INTEGER NOT NULL,
    requested_end_ms    INTEGER NOT NULL,
    pages               INTEGER NOT NULL DEFAULT 0,
    events              INTEGER NOT NULL DEFAULT 0,
    -- 'ok' once the stream paginated to exhaustion; 'partial' if it stopped
    -- early; 'failed' if the API refused. Only 'ok' means the zero above can be
    -- trusted as a real zero.
    status              TEXT NOT NULL,
    error               TEXT,
    job_id              TEXT,
    query_version       INTEGER NOT NULL,
    normalizer_version  INTEGER NOT NULL,
    software_version    TEXT NOT NULL,
    collected_at        REAL NOT NULL
);

-- One row per (run, stream, narrowing). Re-collecting a stream replaces its
-- coverage row rather than appending a second, so the manifest always describes
-- the current state of the data rather than its history.
CREATE UNIQUE INDEX idx_coverage_unique
    ON run_stream_coverage (
        run_id, data_type, IFNULL(hostility, ''),
        IFNULL(source_id, -1), IFNULL(target_id, -1)
    );
CREATE INDEX idx_coverage_run     ON run_stream_coverage (run_id);
CREATE INDEX idx_coverage_stream  ON run_stream_coverage (data_type, hostility);
CREATE INDEX idx_coverage_profile ON run_stream_coverage (collection_profile);
