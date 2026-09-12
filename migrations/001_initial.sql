-- ---------------------------------------------------------------------------
-- 001_initial — Midnight Season 2 Mythic+ research schema
--
-- Designed against the event and field shapes OBSERVED in five live recon runs
-- (see API_NOTES.md), not against guesses. Column choices that look odd are
-- usually a finding:
--
--   * three timestamp bases per event, because pull-relative timing is the
--     research unit but report-relative is what the API returns;
--   * source_instance/target_instance are nullable and are NEVER defaulted,
--     because a missing value means "probably the only copy" and that
--     inference belongs in analysis, not ingestion;
--   * `buffs` is kept verbatim: it lists the auras active on a damage target
--     at the instant of the hit, which turns "was a defensive up" into a
--     lookup instead of a correlation across timelines;
--   * unmitigated_amount is stored alongside amount and mitigated, because the
--     difference between them is the difference between measuring danger and
--     measuring mitigation;
--   * every event keeps its raw payload reference and an `extra` JSON blob, so
--     nothing observed is discarded for lack of a column.
-- ---------------------------------------------------------------------------

CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    applied_at  REAL    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Provenance
-- ---------------------------------------------------------------------------

CREATE TABLE collection_jobs (
    job_id              TEXT PRIMARY KEY,
    started_at          REAL NOT NULL,
    finished_at         REAL,
    status              TEXT NOT NULL,          -- running|complete|failed|interrupted
    sample_profile      TEXT,
    event_profile       TEXT,
    config_hash         TEXT,
    software_version    TEXT NOT NULL,
    normalizer_version  INTEGER NOT NULL,
    query_version       INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL,
    git_commit          TEXT,
    git_dirty           INTEGER,
    reports_attempted   INTEGER NOT NULL DEFAULT 0,
    reports_completed   INTEGER NOT NULL DEFAULT 0,
    reports_failed      INTEGER NOT NULL DEFAULT 0,
    notes               TEXT
);

-- One row per discovery event, not per report: a report found by two sources
-- keeps both records, which is what makes uploader and leaderboard bias
-- assessable after the fact.
CREATE TABLE report_provenance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_code     TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    seed            TEXT,
    discovered_at   REAL NOT NULL,
    rank            INTEGER,
    page            INTEGER,
    job_id          TEXT,
    extra           TEXT                        -- JSON
);
CREATE INDEX idx_report_provenance_code ON report_provenance (report_code);
-- A discovery event is identified by report + source + seed + job. Two
-- different sources finding the same report keep two rows (that is the point);
-- re-running one job does not create a second row for the same discovery.
CREATE UNIQUE INDEX idx_report_provenance_event
    ON report_provenance (report_code, source_type, IFNULL(seed, ''), IFNULL(job_id, ''));

-- ---------------------------------------------------------------------------
-- Reports and runs
-- ---------------------------------------------------------------------------

CREATE TABLE reports (
    report_code         TEXT PRIMARY KEY,
    title_hash          TEXT,                   -- pseudonymized; titles name people
    owner_hash          TEXT,
    start_time_ms       INTEGER NOT NULL,       -- absolute unix ms
    end_time_ms         INTEGER NOT NULL,
    zone_id             INTEGER,
    zone_name           TEXT,
    region_id           INTEGER,
    region_slug         TEXT,
    revision            INTEGER,
    segments            INTEGER,
    visibility          TEXT,
    is_archived         INTEGER,
    is_accessible       INTEGER,
    archive_date        INTEGER,
    log_version         INTEGER,
    game_version        INTEGER,
    retrieved_at        REAL NOT NULL,
    collection_status   TEXT NOT NULL
);

-- collection_status is an enum, never a boolean. Only 'complete' runs may
-- enter an event-frequency denominator (brief section 45).
CREATE TABLE dungeon_runs (
    run_id              TEXT PRIMARY KEY,       -- "<report_code>:<fight_id>"
    report_code         TEXT NOT NULL REFERENCES reports (report_code),
    fight_id            INTEGER NOT NULL,
    dungeon_key         TEXT,                   -- resolved via config, may be NULL
    encounter_id        INTEGER,
    game_zone_id        INTEGER,
    game_zone_name      TEXT,
    wcl_zone_id         INTEGER,
    rel_start_ms        INTEGER NOT NULL,       -- ms since report start
    rel_end_ms          INTEGER NOT NULL,
    abs_start_ms        INTEGER NOT NULL,       -- unix ms
    abs_end_ms          INTEGER NOT NULL,
    duration_ms         INTEGER NOT NULL,
    keystone_level      INTEGER,
    keystone_affixes    TEXT,                   -- JSON array
    keystone_bonus      INTEGER,
    keystone_time_ms    INTEGER,
    rating              REAL,
    count_reached       INTEGER,
    count_required      INTEGER,
    average_item_level  REAL,
    kill                INTEGER,
    timed               INTEGER,                -- derived from keystone_bonus
    size                INTEGER,
    npc_count_map       TEXT,                   -- JSON
    hotfix_epoch        TEXT NOT NULL,
    key_bracket         TEXT,
    duplicate_group_id  TEXT,
    is_canonical        INTEGER,
    collection_status   TEXT NOT NULL,
    job_id              TEXT,
    UNIQUE (report_code, fight_id)
);
CREATE INDEX idx_runs_dungeon_key   ON dungeon_runs (dungeon_key, keystone_level);
CREATE INDEX idx_runs_bracket       ON dungeon_runs (key_bracket);
CREATE INDEX idx_runs_epoch         ON dungeon_runs (hotfix_epoch);
CREATE INDEX idx_runs_dupe_group    ON dungeon_runs (duplicate_group_id);
CREATE INDEX idx_runs_status        ON dungeon_runs (collection_status);

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------

-- No names. The hash is salted with a fixed, non-secret project salt so the
-- same player matches across reports, which is what roster-based duplicate
-- detection needs.
--
-- LIMITATION: masterData exposes no realm for players, so identity is a hash
-- of the character name alone. Two same-named characters on different realms
-- collide. Recorded in DATA_DICTIONARY.md rather than worked around.
CREATE TABLE players (
    player_id   TEXT PRIMARY KEY,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    run_count   INTEGER NOT NULL DEFAULT 0
);

-- Class and spec live here, not on players: a player can bring a different
-- character or spec to each run.
CREATE TABLE run_players (
    run_id      TEXT NOT NULL REFERENCES dungeon_runs (run_id),
    actor_id    INTEGER NOT NULL,
    player_id   TEXT NOT NULL REFERENCES players (player_id),
    class       TEXT,
    spec        TEXT,
    role        TEXT,
    item_level  REAL,
    PRIMARY KEY (run_id, actor_id)
);
CREATE INDEX idx_run_players_player ON run_players (player_id);
CREATE INDEX idx_run_players_role   ON run_players (role, spec);

-- NPC names are research data and are kept. Player names are pseudonymized
-- before they reach this table.
CREATE TABLE actors (
    report_code TEXT NOT NULL,
    actor_id    INTEGER NOT NULL,
    game_id     INTEGER,
    name        TEXT,
    type        TEXT,
    sub_type    TEXT,
    icon        TEXT,
    pet_owner   INTEGER,
    is_player   INTEGER NOT NULL,
    PRIMARY KEY (report_code, actor_id)
);
CREATE INDEX idx_actors_game_id ON actors (game_id);

CREATE TABLE abilities (
    game_id     INTEGER PRIMARY KEY,
    name        TEXT,
    icon        TEXT,
    type        TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

-- ---------------------------------------------------------------------------
-- Pulls
-- ---------------------------------------------------------------------------

-- Warcraft Logs' own pull boundaries are authoritative (brief section 18).
-- No combat-gap detector is implemented.
CREATE TABLE pulls (
    pull_id                 TEXT PRIMARY KEY,   -- "<run_id>:<wcl_pull_id>"
    run_id                  TEXT NOT NULL REFERENCES dungeon_runs (run_id),
    wcl_pull_id             INTEGER NOT NULL,
    pull_index              INTEGER NOT NULL,   -- order within the run
    name                    TEXT,
    encounter_id            INTEGER,
    is_boss                 INTEGER NOT NULL DEFAULT 0,
    rel_start_ms            INTEGER NOT NULL,
    rel_end_ms              INTEGER NOT NULL,
    run_rel_start_ms        INTEGER NOT NULL,
    run_rel_end_ms          INTEGER NOT NULL,
    abs_start_ms            INTEGER NOT NULL,
    abs_end_ms              INTEGER NOT NULL,
    duration_ms             INTEGER NOT NULL,
    kill                    INTEGER,
    x                       INTEGER,
    y                       INTEGER,
    map_ids                 TEXT,               -- JSON
    bounding_box            TEXT,               -- JSON
    composition_signature   TEXT,               -- "npcA x1 | npcB x2"
    npc_species_count       INTEGER,
    npc_total_count         INTEGER,
    prev_pull_id            TEXT,
    next_pull_id            TEXT,
    UNIQUE (run_id, wcl_pull_id)
);
CREATE INDEX idx_pulls_run          ON pulls (run_id, pull_index);
CREATE INDEX idx_pulls_signature    ON pulls (composition_signature);
CREATE INDEX idx_pulls_time         ON pulls (run_id, rel_start_ms, rel_end_ms);

-- instance_count is DERIVED from the instance-ID range and carries its own
-- confidence, because a range is an inference about multiplicity, not a count.
CREATE TABLE pull_npcs (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    pull_id                     TEXT NOT NULL REFERENCES pulls (pull_id),
    npc_game_id                 INTEGER NOT NULL,
    actor_id                    INTEGER,
    min_instance_id             INTEGER,
    max_instance_id             INTEGER,
    min_instance_group_id       INTEGER,
    max_instance_group_id       INTEGER,
    instance_count              INTEGER,
    instance_count_confidence   TEXT NOT NULL   -- exact|inferred|unknown
);
CREATE UNIQUE INDEX idx_pull_npcs_unique ON pull_npcs (pull_id, npc_game_id, IFNULL(actor_id, -1));
CREATE INDEX idx_pull_npcs_game_id ON pull_npcs (npc_game_id);

-- ---------------------------------------------------------------------------
-- Events
-- ---------------------------------------------------------------------------

-- The audit trail for pagination: which pages were requested, which succeeded,
-- and where the raw response is cached.
CREATE TABLE event_pages (
    page_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL REFERENCES dungeon_runs (run_id),
    data_type           TEXT NOT NULL,
    hostility           TEXT,                   -- Enemies|Friendlies|NULL
    page_index          INTEGER NOT NULL,
    requested_start_ms  INTEGER NOT NULL,
    requested_end_ms    INTEGER NOT NULL,
    cursor_ms           INTEGER,
    next_cursor_ms      INTEGER,
    event_count         INTEGER NOT NULL DEFAULT 0,
    raw_cache_path      TEXT,
    status              TEXT NOT NULL,          -- ok|failed
    error               TEXT,
    fetched_at          REAL NOT NULL
);
-- Expression indexes cannot live in a table-level UNIQUE constraint, and
-- hostility is deliberately NULL rather than '' when no filter was applied.
CREATE UNIQUE INDEX idx_event_pages_unique
    ON event_pages (run_id, data_type, IFNULL(hostility, ''), page_index);
CREATE INDEX idx_event_pages_run ON event_pages (run_id, data_type);

-- Every event traces to the page it came from, which is what makes re-fetching
-- a page idempotent: its rows are replaced, not appended to.
CREATE TABLE events (
    event_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id                 INTEGER NOT NULL REFERENCES event_pages (page_id),
    seq_in_page             INTEGER NOT NULL,
    run_id                  TEXT NOT NULL REFERENCES dungeon_runs (run_id),
    report_code             TEXT NOT NULL,
    pull_id                 TEXT,               -- NULL = outside every pull, kept
    data_type               TEXT NOT NULL,
    hostility               TEXT,

    -- Three bases. Deriving later is cheap; recovering a discarded one is not.
    rel_ms                  INTEGER NOT NULL,   -- since report start
    abs_ms                  INTEGER NOT NULL,   -- unix ms
    run_rel_ms              INTEGER NOT NULL,   -- since run start
    pull_rel_ms             INTEGER,            -- since pull start, NULL if unassigned

    type                    TEXT NOT NULL,
    source_id               INTEGER,
    source_instance         INTEGER,            -- NULL is meaningful; never defaulted
    target_id               INTEGER,
    target_instance         INTEGER,
    ability_game_id         INTEGER,
    extra_ability_game_id   INTEGER,            -- what was interrupted / dispelled

    amount                  INTEGER,
    absorbed                INTEGER,
    blocked                 INTEGER,
    mitigated               INTEGER,
    unmitigated_amount      INTEGER,
    overkill                INTEGER,
    hit_type                INTEGER,
    is_tick                 INTEGER,
    is_aoe                  INTEGER,
    is_buff                 INTEGER,
    stack                   INTEGER,

    hit_points              INTEGER,
    max_hit_points          INTEGER,
    buffs                   TEXT,               -- auras active at the instant of the hit

    killer_id               INTEGER,
    killer_instance         INTEGER,
    killing_ability_game_id INTEGER,

    x                       INTEGER,
    y                       INTEGER,
    map_id                  INTEGER,

    extra                   TEXT,               -- JSON: everything not promoted
    normalizer_version      INTEGER NOT NULL,
    UNIQUE (page_id, seq_in_page)
);

-- Indexes chosen for the queries the research actually runs: per-pull
-- mechanic timelines, per-NPC-instance cast recurrence, and death windows.
CREATE INDEX idx_events_pull_time       ON events (pull_id, rel_ms);
CREATE INDEX idx_events_run_time        ON events (run_id, rel_ms);
CREATE INDEX idx_events_ability         ON events (ability_game_id, type);
CREATE INDEX idx_events_source_instance ON events (run_id, source_id, source_instance, rel_ms);
CREATE INDEX idx_events_target          ON events (run_id, target_id, rel_ms);
CREATE INDEX idx_events_type            ON events (type);

-- ---------------------------------------------------------------------------
-- Diagnostics
-- ---------------------------------------------------------------------------

CREATE TABLE ingest_diagnostics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT,
    run_id      TEXT,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,                  -- info|warning|error
    detail      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX idx_diagnostics_run ON ingest_diagnostics (run_id, kind);
