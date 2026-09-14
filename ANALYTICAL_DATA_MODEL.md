# Analytical Data Model

**Status:** proposal for review. No migration beyond `004` has been written.

Schemas for the analysis layer described in
[`SHIFU_ARCHITECTURE_PLAN.md`](SHIFU_ARCHITECTURE_PLAN.md). Ordering and
acceptance are in [`IMPLEMENTATION_PHASES.md`](IMPLEMENTATION_PHASES.md).

---

## 0. Rules that every table below obeys

1. **Nothing here is a source of truth.** Every table is derived from the ingest
   store or from the raw cache, and every one can be dropped and rebuilt. If a
   fact exists only in a derived table, that is a bug.
2. **Every derived table carries its deriver's version.** A row produced by
   build-parser v1 must be distinguishable from one produced by v2, because they
   can mean different things. Never reinterpret an old row under new semantics.
3. **Every derived row carries provenance** back to run, pull and time window,
   or to an efficient reference to the set of runs it came from.
4. **Coverage is checked before a stream is read**, not after. `run_stream_coverage`
   already distinguishes "asked, none" from "never asked"; a derived statistic
   that ignores it is wrong in the direction of "this never happens", which is
   the most believable kind of wrong.
5. **Canonical runs only, by default.** `is_canonical = 1 OR is_canonical IS NULL`.
   `NULL` means dedupe has not run and is reported as such, never treated as
   confirmed-unique.
6. **`unknown` is a value.** Not `NULL`-meaning-zero, not a default, not omitted.

---

## 1. Player build snapshots (brief §6)

### Migration `005_player_builds.sql`

```sql
-- One row per DISTINCT player configuration ever observed. A player who never
-- changes talents contributes one row across a hundred runs; one who respecs
-- contributes two, and the two stay separable -- which is the whole point.
CREATE TABLE player_build_snapshots (
    build_id            TEXT PRIMARY KEY,   -- deterministic hash, see below
    class               TEXT,
    spec                TEXT,
    spec_id             INTEGER,
    hero_talent         TEXT,               -- NULL until semantics are established
    hero_talent_status  TEXT NOT NULL,      -- unknown|derived|external  (never 'observed')
    talent_payload      TEXT,               -- JSON: talents as WCL reports them
    talent_tree_payload TEXT,               -- JSON: talentTree as WCL reports it
    gear_payload        TEXT,               -- JSON: full gear array, unmodified
    item_level          REAL,
    primary_stats       TEXT,               -- JSON
    secondary_stats     TEXT,               -- JSON
    trinket_1_item_id   INTEGER,
    trinket_2_item_id   INTEGER,
    raw_combatant_info  TEXT NOT NULL,      -- the source payload, retained whole
    parser_version      INTEGER NOT NULL,
    first_seen          REAL NOT NULL,
    last_seen           REAL NOT NULL
);
CREATE INDEX idx_builds_spec    ON player_build_snapshots (spec, hero_talent);
CREATE INDEX idx_builds_trinket ON player_build_snapshots (trinket_1_item_id, trinket_2_item_id);

-- Which build each player brought to each run. The join that makes
-- "compare Apex Mistweavers against non-Apex" one query instead of a scan.
CREATE TABLE run_player_builds (
    run_id       TEXT NOT NULL REFERENCES dungeon_runs (run_id),
    actor_id     INTEGER NOT NULL,
    build_id     TEXT NOT NULL REFERENCES player_build_snapshots (build_id),
    source_event_id INTEGER REFERENCES events (event_id),
    PRIMARY KEY (run_id, actor_id)
);
CREATE INDEX idx_run_player_builds_build ON run_player_builds (build_id);
```

### `build_id` construction

```
build_id = sha256(
    parser_version || spec_id || canonical_json(talents)
                   || canonical_json(talentTree)
                   || canonical_json([{slot, id, itemLevel, gems, enchant} for gear])
)[:16]
```

Deterministic by construction: keys sorted, no whitespace, no floats that
round differently across runs. Two identical configurations collapse to one row;
a single changed talent produces a different row and stays distinct.

**Deliberately excluded from the hash:** current stats (they vary with buffs at
the instant of the snapshot), and `auras` (state, not configuration).

### What is *not* claimed

`hero_talent_status` exists because hero-talent identity cannot be read reliably
out of the talent payload without knowing the tree structure, and that structure
is external game metadata. The column stays `unknown` until a knowledge source
supplies it. **A cohort filtered on `hero_talent` must refuse to run while the
status is `unknown`** rather than silently returning the subset that happens to
be populated.

> **Backfill:** offline, zero API cost. The 460 CombatantInfo events in the
> current corpus are already stored. Re-parsing is a local pass.

---

## 2. Canonical action mapping (brief §28)

### Migration `006_canonical_actions.sql`

```sql
-- One real game action, which may surface as several ability IDs.
CREATE TABLE canonical_actions (
    action_id       TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    actor_side      TEXT NOT NULL,      -- enemy|player
    model_version   INTEGER NOT NULL,
    notes           TEXT
);

-- The mapping, with its evidence. Never populated from name similarity alone.
CREATE TABLE action_ability_map (
    action_id     TEXT NOT NULL REFERENCES canonical_actions (action_id),
    ability_game_id INTEGER NOT NULL,
    role          TEXT NOT NULL,        -- cast|impact|aura|tick|summon
    confidence    TEXT NOT NULL,        -- measured|inferred|manual
    evidence      TEXT,                 -- JSON: co-occurrence rate, N, run IDs
    model_version INTEGER NOT NULL,
    PRIMARY KEY (action_id, ability_game_id)
);
```

`validate.py::paired_abilities` already measures co-occurrence and reports
`rate_a`, `rate_b` and `inseparable`. That is the evidence that populates
`action_ability_map`; it is not automatically promoted. A 4:1 pair is *not*
one-for-one and the mapping must say so.

The default is **separate**. An unmapped ability is its own action. Merging is a
claim and requires evidence recorded next to it.

---

## 3. Pull archetypes (brief §29)

### Migration `007_pull_archetypes.sql`

```sql
CREATE TABLE pull_archetypes (
    archetype_id      TEXT PRIMARY KEY,
    dungeon_key       TEXT NOT NULL,
    label             TEXT,             -- human name, optional, never load-bearing
    core_species      TEXT NOT NULL,    -- the species set that defines membership
    typical_position  INTEGER,          -- median pull_index
    centroid          TEXT,             -- JSON: interpretable feature centroid
    member_count      INTEGER NOT NULL,
    model_version     INTEGER NOT NULL
);

CREATE TABLE pull_archetype_members (
    pull_id       TEXT NOT NULL REFERENCES pulls (pull_id),
    archetype_id  TEXT NOT NULL REFERENCES pull_archetypes (archetype_id),
    match_kind    TEXT NOT NULL,   -- exact|species|fuzzy
    distance      REAL,            -- NULL for exact and species matches
    model_version INTEGER NOT NULL,
    PRIMARY KEY (pull_id, archetype_id, model_version)
);
```

Three levels, in increasing looseness and decreasing confidence:

| Level | Basis | Already exists |
|---|---|---|
| exact | `composition_signature` — same species, same counts | yes, indexed |
| species | `species_signature` — same species, any counts | yes, indexed |
| fuzzy | interpretable feature distance | no — this is the new work |

Fuzzy features, all interpretable, none learned: species overlap (Jaccard),
count difference, map-coordinate distance, `pull_index` offset, neighbour
identity, duration ratio, boss flag. Weights are configuration, not constants in
code, so they can be argued with.

Clustering is evaluated *after* these are measured and only adopted if it beats
them on something they cannot express.

---

## 4. Gameplay state (brief §§30-32)

### Migration `008_gameplay_state.sql`

```sql
-- A reconstructed state at one instant. Not one row per event: one row per
-- moment an analysis cares about (an action, a death, a damage window).
CREATE TABLE gameplay_states (
    state_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES dungeon_runs (run_id),
    pull_id         TEXT REFERENCES pulls (pull_id),
    archetype_id    TEXT,
    rel_ms          INTEGER NOT NULL,
    pull_rel_ms     INTEGER,
    key_level       INTEGER,
    key_bracket     TEXT,
    hotfix_epoch    TEXT,
    subject_actor_id INTEGER,          -- whose state this is
    subject_build_id TEXT REFERENCES player_build_snapshots (build_id),
    payload         TEXT NOT NULL,     -- JSON: the state itself, see below
    observability   TEXT NOT NULL,     -- JSON: tag per field
    source_event_id INTEGER REFERENCES events (event_id),
    model_version   INTEGER NOT NULL
);
CREATE INDEX idx_states_pull    ON gameplay_states (pull_id, rel_ms);
CREATE INDEX idx_states_subject ON gameplay_states (run_id, subject_actor_id, rel_ms);

-- What the subject did from that state.
CREATE TABLE state_actions (
    state_id      TEXT NOT NULL REFERENCES gameplay_states (state_id),
    seq           INTEGER NOT NULL,
    action_id     TEXT,                -- canonical action where mapped
    ability_game_id INTEGER,
    target_actor_id INTEGER,
    target_instance INTEGER,
    rel_ms        INTEGER NOT NULL,
    source_event_id INTEGER REFERENCES events (event_id),
    PRIMARY KEY (state_id, seq)
);

-- What followed, in a defined window. Components, never a score.
CREATE TABLE state_outcomes (
    state_id      TEXT PRIMARY KEY REFERENCES gameplay_states (state_id),
    window_ms     INTEGER NOT NULL,
    deaths        INTEGER,
    party_hp_delta TEXT,               -- JSON per player
    damage_taken  INTEGER,
    healing_done  INTEGER,
    enemies_died  INTEGER,
    components    TEXT NOT NULL,       -- JSON: raw components, unaggregated
    coverage      TEXT NOT NULL,       -- JSON: which streams backed each component
    model_version INTEGER NOT NULL
);
```

### The `observability` column is the load-bearing one

```json
{
  "party_hp":        "derived",
  "subject_mana":    "observed",
  "cooldowns":       "unknown",
  "subject_target":  "inferred",
  "enemy_positions": "unknown"
}
```

Every consumer — the exporter, the comparison layer, the LLM context — reads
this before reading `payload`. A field tagged `unknown` is rendered as unknown,
never dropped and never defaulted. This is what stops a state with six holes in
it from being presented as a complete picture.

### Cooldown availability (brief §31)

Modelled as `unknown` today, with a defined upgrade path:

| Evidence available | Best claim |
|---|---|
| nothing | `unknown` |
| casts + charges observed in this run | `probable`, with the assumption stated |
| + external ability metadata (§40) | `derived`, with the source cited |

Never `observed` — the API reports no cooldown timer. And never from a tooltip
value held in memory: the empirical corpus alone cannot establish recast rules,
and an NPC's observed recast interval is not its cooldown.

---

## 5. Cohorts (brief §35)

### Migration `009_cohorts.sql`

```sql
-- The filter set and its version. NOT the members: membership is a query,
-- and materialising it would let a cohort silently go stale as runs arrive.
CREATE TABLE cohort_definitions (
    cohort_id     TEXT PRIMARY KEY,
    label         TEXT,
    filters       TEXT NOT NULL,   -- JSON: spec, dungeon, bracket, build, epoch, ...
    required_streams TEXT NOT NULL,-- JSON: streams a run MUST have collected
    model_version INTEGER NOT NULL,
    created_at    REAL NOT NULL
);
```

`required_streams` is mandatory and has no default. A cohort that needs Healing
excludes every run that never requested it, **reports the exclusion count**, and
never quietly averages over a mixed-fidelity population. A corpus assembled from
`mechanics_research` and `user_coaching` profiles is exactly such a population,
and `validate` already detects that mixing.

No performance score is stored anywhere in this schema. §36 is explicit: the
definition of "high-performing" is a research judgement, and baking it into a
column makes it unrevisable and invisible.

---

## 6. Analytical projection (brief §§7, 55, 57)

**Not a migration. Files, not tables.**

```
data/analytics/
  _manifest.json                       projection version, source DB, run set, built_at
  events/dungeon=<key>/epoch=<id>/stream=<type>/part-*.parquet
  pulls/dungeon=<key>/part-*.parquet
  states/dungeon=<key>/spec=<spec>/part-*.parquet
  dict/{actors,npcs,abilities,items,builds,archetypes}.parquet
```

Partitioning by dungeon, hotfix epoch and stream is chosen because those are the
three filters nearly every question applies first, and partition pruning is where
columnar storage actually pays. Spec partitions the state tables because cohort
questions are spec-first.

**Dictionary encoding** (§57): stable numeric IDs inside the fact tables; display
names live only in `dict/`. An NPC name repeated across ten million damage rows
is ten million copies of a string that means one thing.

Rules, restated because they are easy to erode:

- derived from canonical ingest data only;
- rebuildable from scratch, deterministically — same input, same bytes;
- versioned, with the version in `_manifest.json`;
- incremental where practical, but a full rebuild must always work;
- **no information exists only here**;
- SQLite and the raw cache remain the provenance authorities;
- regenerable after any normalizer improvement.

`pyarrow` is an optional extra today. DuckDB would be a second. The offline test
suite must pass with neither installed, and collection must never require either.

---

## 7. Version registry (brief §47)

Every derived model gets a version, and every derived row records the version
that produced it.

| Model | Constant | Starts at | Bump when |
|---|---|---|---|
| Build parser | `BUILD_PARSER_VERSION` | 1 | CombatantInfo → snapshot mapping changes |
| Canonical actions | `ACTION_MODEL_VERSION` | 1 | a mapping or its confidence rule changes |
| Pull archetypes | `ARCHETYPE_MODEL_VERSION` | 1 | features or weights change |
| Gameplay state | `STATE_SCHEMA_VERSION` | 1 | a state field is added, removed or retagged |
| Cohorts | `COHORT_MODEL_VERSION` | 1 | a filter's meaning changes |
| Similarity | `SIMILARITY_MODEL_VERSION` | 1 | features or weighting change |
| Projection | `PROJECTION_VERSION` | 1 | partition layout or column set changes |
| SCL | `SCL_VERSION` | `experimental-1` | any encoding change; see the experiment plan |

These join the existing four (`SOFTWARE_VERSION`, `NORMALIZER_VERSION`,
`QUERY_VERSION`, `SCHEMA_VERSION`) and the identity scheme version added in
migration 004, all already carried by `version.py::provenance()`.

---

## 8. Migration sequence

| # | File | Adds | Backfillable offline |
|---|---|---|---|
| 004 | `identity_scheme.sql` | **applied** — `corpus_identity`, `idx_actors_name` | n/a |
| 005 | `player_builds.sql` | build snapshots, run→build link | **yes** |
| 006 | `canonical_actions.sql` | action mapping + evidence | yes |
| 007 | `pull_archetypes.sql` | archetypes + membership | yes |
| 008 | `gameplay_state.sql` | states, actions, outcomes | partly — bounded by stream coverage |
| 009 | `cohorts.sql` | cohort definitions | yes |

Every one is additive: new tables, no column dropped, no meaning changed. The
existing 94 runs need no re-collection for any of them. That is a direct
consequence of the collector keeping raw payloads and routing unpromoted fields
to `events.extra`, and it is worth naming as the dividend of that decision.

The one thing **no** migration can backfill: a stream that was never requested.
`run_stream_coverage` is what tells you which those are, per run, and it is why
a state reconstructed from a `mechanics_research` run will carry more `unknown`
tags than one from `forensic_full`.
