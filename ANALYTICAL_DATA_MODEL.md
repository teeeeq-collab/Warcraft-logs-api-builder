# Analytical Data Model

**Status:** proposal, revision 2. Incorporates the architecture review; see
[`ARCHITECTURE_REVIEW_RESPONSES.md`](ARCHITECTURE_REVIEW_RESPONSES.md) for the
reasoning behind each change. No migration beyond `004` has been written.

Companion documents: [`SHIFU_ARCHITECTURE_PLAN.md`](SHIFU_ARCHITECTURE_PLAN.md),
[`SCL_EXPERIMENT_PLAN.md`](SCL_EXPERIMENT_PLAN.md),
[`IMPLEMENTATION_PHASES.md`](IMPLEMENTATION_PHASES.md).

---

## 0. Two stores, one boundary

Revision 1 said "Layer 2 never writes to Layer 1" and then put four derived
tables in the ingest database. Revision 2 separates them physically.

```
data/db/<corpus>.sqlite                 INGEST — collection truth
    migrations/                          schema_version 4
    reports, dungeon_runs, pulls, pull_npcs, actors, abilities,
    events, event_pages, run_stream_coverage, run_players,
    combatant_info (NEW, migration 005), corpus_identity
                       │
                       │  read-only ATTACH; derivation only
                       ▼
data/analytics/<corpus>.analysis.sqlite  ANALYSIS — disposable
    migrations/analytics/                analytics_schema_version 1
    build dimensions, canonical actions, archetypes,
    gameplay states, outcomes, cohorts, evaluations
                       │
                       ▼
data/analytics/parquet/                  PROJECTION — disposable
```

**The rule for which store anything belongs in:**

> Could a future version of this code change what an existing row *means*,
> without any new data from the API?

No → it is transcription → ingest store. Yes → it is a model → analysis store.

Mapping CombatantInfo's fields into columns is transcription. Deciding what
constitutes "a build" is a model. That line runs through the middle of the
build work and §1 below splits it accordingly.

### The foreign key that cannot exist

SQLite does not enforce foreign keys across attached databases. An analytical
`run_id` cannot reference `dungeon_runs.run_id`.

The replacement is stronger for this purpose. Every analytical table records the
**corpus fingerprint** it was derived from (§6), and `wclmplus analytics verify`
checks referential integrity on demand. A foreign key proves *this run exists*.
A fingerprint proves *this run exists and has not been recollected or
renormalized since this row was derived* — which is the failure that would
actually invalidate a conclusion, and the one an FK would not catch.

---

## 1. Rules every table below obeys

1. **Nothing in the analysis store is a source of truth.** Delete it; rebuild it.
   A fact that exists only there is a bug.
2. **Every derived row carries its deriver's version.** Never reinterpret an old
   row under new semantics.
3. **Every derived row carries provenance** to run, pull and time window, or to
   an immutable source-set reference.
4. **Coverage is checked before a stream is read.** `run_stream_coverage`
   distinguishes "asked, none" from "never asked"; a statistic that ignores it is
   wrong in the direction of "this never happens".
5. **Dedupe policy is explicit** (§7). No analytical entry point has a default.
6. **`unknown` is a value**, and so is `stale`. Not `NULL`-meaning-zero.
7. **Every statistic reports evidence depth at every level** (§6), never a single
   `N`.

---

## 2. CombatantInfo: normalization, then derivation

### 2a. Ingest store — migration `005_combatant_info.sql`

Transcription only. One row per player per run, columns mapped directly from the
payload, raw JSON retained. No hashes, no identity decisions, no interpretation.

```sql
CREATE TABLE combatant_info (
    run_id          TEXT NOT NULL REFERENCES dungeon_runs (run_id),
    actor_id        INTEGER NOT NULL,
    spec_id         INTEGER,
    item_level      REAL,
    talents         TEXT,              -- JSON, as WCL reports it
    talent_tree     TEXT,              -- JSON, as WCL reports it
    gear            TEXT,              -- JSON array, unmodified
    stats           TEXT,              -- JSON
    auras           TEXT,              -- JSON: state at snapshot, not configuration
    raw             TEXT NOT NULL,     -- the whole event payload
    source_event_id INTEGER REFERENCES events (event_id),
    normalizer_version INTEGER NOT NULL,
    PRIMARY KEY (run_id, actor_id)
);
CREATE INDEX idx_combatant_spec ON combatant_info (spec_id);
```

> **Backfill:** offline, zero API cost. The 460 CombatantInfo events in the
> current corpus are already stored in `events.extra`.

**A limitation the schema states rather than implies:** CombatantInfo is a
snapshot at fight start. Gear swapped mid-dungeon is invisible. Every equipment
and stat row below therefore means "as at the start of the run", never
"throughout it".

### 2b. Analysis store — separable build dimensions

Revision 1 hashed talents and gear into one `build_id`. That made **item level a
talent variable**: two Mistweavers with identical talents and hero talents at
ilvl 681 and 684 became different builds. Over a few hundred runs a cohort query
for "Apex Mistweavers" would have returned a scatter of one-member builds and
reported `N=1` for a configuration dozens of players ran — a confident statistic
with the wrong denominator.

Five independent dimensions, plus an optional composite.

```sql
-- Talents alone. Nothing about gear enters this hash.
CREATE TABLE talent_loadouts (
    talent_hash    TEXT PRIMARY KEY,
    spec_id        INTEGER,
    class          TEXT,
    spec           TEXT,
    talents        TEXT NOT NULL,      -- canonical JSON
    talent_tree    TEXT,
    model_version  INTEGER NOT NULL
);

-- Hero talent identity. EXTERNAL: cannot be read out of the payload without
-- knowing the tree structure, which is game metadata this project does not have.
CREATE TABLE hero_talents (
    hero_talent_id TEXT PRIMARY KEY,
    display_name   TEXT,
    spec_id        INTEGER,
    status         TEXT NOT NULL,      -- unknown|external   (never 'observed')
    source         TEXT,               -- citation when status = 'external'
    model_version  INTEGER NOT NULL
);

-- Equipment. item_level is a COLUMN, never part of the hash: it is continuous
-- and would fragment equipment identity the way it would have fragmented builds.
CREATE TABLE equipment_snapshots (
    equipment_hash TEXT PRIMARY KEY,
    items          TEXT NOT NULL,      -- canonical JSON: slot, id, enchant, gems
    item_level     REAL,
    model_version  INTEGER NOT NULL
);

-- Trinkets as an UNORDERED pair. Which one sits in slot 13 is arbitrary;
-- treating it as meaningful would split one configuration into two.
CREATE TABLE trinket_configs (
    trinket_config_id TEXT PRIMARY KEY,
    item_id_low       INTEGER,         -- sorted, so order cannot vary
    item_id_high      INTEGER,
    model_version     INTEGER NOT NULL
);

-- Secondaries, bucketed. Raw values never repeat across players, so an exact
-- hash would make every stat snapshot unique and useless for matching.
CREATE TABLE stat_snapshots (
    stat_hash      TEXT PRIMARY KEY,
    stats          TEXT NOT NULL,      -- canonical JSON, bucketed
    bucket_scheme  TEXT NOT NULL,
    model_version  INTEGER NOT NULL
);

-- The composite, for "this exact configuration". Available, never mandatory.
CREATE TABLE run_player_config (
    run_id            TEXT NOT NULL,
    actor_id          INTEGER NOT NULL,
    config_id         TEXT NOT NULL,
    talent_hash       TEXT REFERENCES talent_loadouts (talent_hash),
    hero_talent_id    TEXT REFERENCES hero_talents (hero_talent_id),
    equipment_hash    TEXT REFERENCES equipment_snapshots (equipment_hash),
    trinket_config_id TEXT REFERENCES trinket_configs (trinket_config_id),
    stat_hash         TEXT REFERENCES stat_snapshots (stat_hash),
    item_level        REAL,
    corpus_fingerprint TEXT NOT NULL,
    model_version     INTEGER NOT NULL,
    PRIMARY KEY (run_id, actor_id)
);
CREATE INDEX idx_config_talent  ON run_player_config (talent_hash);
CREATE INDEX idx_config_hero    ON run_player_config (hero_talent_id);
CREATE INDEX idx_config_trinket ON run_player_config (trinket_config_id);
CREATE INDEX idx_config_equip   ON run_player_config (equipment_hash);
```

Each question in the review is one join on one dimension:

| Question | Dimension |
|---|---|
| Apex vs no-Apex | `hero_talent_id` |
| talent build A vs B | `talent_hash` |
| trinket X vs Y | `trinket_config_id` |
| similar gear | `equipment_hash`, or `item_level` as a range |
| same talents regardless of equipment | `talent_hash` alone |

**Hero talents stay `unknown` until an external source establishes them.** A
cohort filtered on `hero_talent_id` while the status is `unknown` **refuses to
run** rather than returning whichever rows happen to be populated — a silently
partial cohort is worse than no cohort.

---

## 3. Canonical action mapping

Analysis store. Unchanged from revision 1 except for its location.

```sql
CREATE TABLE canonical_actions (
    action_id     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    actor_side    TEXT NOT NULL,        -- enemy|player
    model_version INTEGER NOT NULL,
    notes         TEXT
);

CREATE TABLE action_ability_map (
    action_id       TEXT NOT NULL REFERENCES canonical_actions (action_id),
    ability_game_id INTEGER NOT NULL,
    role            TEXT NOT NULL,      -- cast|impact|aura|tick|summon
    confidence      TEXT NOT NULL,      -- measured|inferred|manual
    evidence        TEXT,               -- JSON: co-occurrence rate, N, run IDs
    model_version   INTEGER NOT NULL,
    PRIMARY KEY (action_id, ability_game_id)
);
```

`validate.py::paired_abilities` supplies the evidence — it already reports
`rate_a`, `rate_b` and `inseparable`. Evidence is not automatically promoted: a
4:1 pair is not one-for-one and the mapping must say so. The default is
**separate**; merging is a claim and carries its evidence.

---

## 4. Pull archetypes

```sql
CREATE TABLE pull_archetypes (
    archetype_id     TEXT PRIMARY KEY,
    dungeon_key      TEXT NOT NULL,
    label            TEXT,
    core_species     TEXT NOT NULL,
    typical_position INTEGER,
    centroid         TEXT,             -- JSON: interpretable feature centroid
    -- Reserved for pack decomposition (review amendment 14). Populated later,
    -- present now so decomposition never needs a migration.
    components       TEXT,             -- JSON: inferred latent packs, or NULL
    component_status TEXT NOT NULL,    -- unknown|inferred
    member_count     INTEGER NOT NULL,
    model_version    INTEGER NOT NULL
);

CREATE TABLE pull_archetype_members (
    pull_id       TEXT NOT NULL,
    archetype_id  TEXT NOT NULL REFERENCES pull_archetypes (archetype_id),
    match_kind    TEXT NOT NULL,       -- exact|species|fuzzy
    distance      REAL,
    model_version INTEGER NOT NULL,
    PRIMARY KEY (pull_id, archetype_id, model_version)
);
```

Three levels; the first two already exist and are indexed in the ingest store.

| Level | Basis | Exists |
|---|---|---|
| exact | `composition_signature` | yes |
| species | `species_signature` | yes |
| fuzzy | interpretable feature distance | new |

Fuzzy features, all interpretable: species overlap (Jaccard), count difference,
map-coordinate distance, `pull_index` offset, neighbour identity, duration ratio,
boss flag. Weights are configuration. Clustering is evaluated only after these
are measured.

**Pack decomposition** (amendment 14) is the eventual goal: representing an
observed pull as `Pack A + Pack B (+ stray C)` rather than as a fuzzy variant.
It is a latent-structure problem — observed pulls are unions of unobserved packs
— and recovering the parts requires the *combinations* to vary across many
routes and many groups. It is the most N-hungry item in this package. Designed
for, not scheduled.

---

## 5. Gameplay state

### 5a. States, with per-field observability

```sql
CREATE TABLE gameplay_states (
    state_id         TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    pull_id          TEXT,
    archetype_id     TEXT,
    rel_ms           INTEGER NOT NULL,
    pull_rel_ms      INTEGER,
    key_level        INTEGER,
    key_bracket      TEXT,
    hotfix_epoch     TEXT,
    subject_actor_id INTEGER,
    subject_config_id TEXT,
    payload          TEXT NOT NULL,    -- JSON: field -> value
    field_meta       TEXT NOT NULL,    -- JSON: field -> metadata record, see below
    block_meta       TEXT NOT NULL,    -- JSON: tags for run-constant fields
    source_event_id  INTEGER,
    corpus_fingerprint TEXT NOT NULL,
    model_version    INTEGER NOT NULL
);
CREATE INDEX idx_states_pull    ON gameplay_states (pull_id, rel_ms);
CREATE INDEX idx_states_subject ON gameplay_states (run_id, subject_actor_id, rel_ms);
```

Revision 1 tagged each field `observed | derived | inferred | unknown`. That is
necessary and insufficient: HP last seen 40 ms ago and HP last seen 8 s ago were
both "observed", and only one is worth anything.

**`payload` owns the values. `field_meta` owns only the metadata**, keyed by the
same field name. The value is never repeated in `field_meta`: two copies of one
number are two things to keep in agreement, and they would eventually disagree —
after which no reader could tell which one the analysis actually used.

```json
// payload
{ "subject_hp": 44, "subject_mana": 72, "enemies_alive": 3 }

// field_meta — same keys, metadata only, no "value"
{
  "subject_hp": {
    "status": "stale",
    "observed_at_ms": 184920,
    "age_ms": 8300,
    "method": "last_hit_points_on_target",
    "model_version": 1,
    "confidence": null
  }
}
```

A field present in `payload` with no `field_meta` entry is `observed` and fresh —
the quiet default described in §5c. A field in `field_meta` with no `payload`
entry is a contradiction and `analytics verify` reports it.

| Status | Meaning |
|---|---|
| `observed` | read from an event, within its staleness horizon |
| `stale` | read from an event, past its horizon; `age_ms` says how far |
| `derived` | computed from observed values by a documented rule |
| `inferred` | a model, with `method` naming it and `confidence` where probabilistic |
| `unknown` | not establishable from this corpus |

### 5b. Staleness horizons

Recording age is not enough — a consumer handed `age_ms: 8300` can ignore it,
and eventually one will. Horizons live in configuration, not in code, and the
degradation to `stale` is automatic:

```yaml
# config/state_horizons.yml
hp:            2000     # moves constantly
resource:      3000
position:      1500
aura_active:   null     # explicit apply/remove events: exact between them
enemies_alive: null     # derived from deaths: exact between them
build:         null     # constant within a run
```

`null` is a real category, not a missing value. An aura's state is known exactly
between its apply and remove events; calling it stale after two seconds would be
wrong in the other direction.

### 5c. Payload cost

Seven metadata keys across ~30 fields is roughly 200 extra JSON keys per state.
Acceptable in a table; ruinous in an LLM export, where it would dwarf the data.
Two mitigations, both structural:

- **`block_meta`** carries one tag for fields constant within a run — spec,
  config, key level, dungeon, hotfix epoch — instead of repeating a record per
  field per state.
- **The exporter emits `field_meta` only for fields that are not
  `observed`-and-fresh.** Exceptions get described; the ordinary case stays
  quiet. This is the same principle as default suppression in the SCL plan, and
  it is lossless for the same reason: the rule is documented and the default is
  recoverable.

### 5d. Cooldown availability

| Evidence | Best claim |
|---|---|
| nothing | `unknown` |
| casts + charges observed in this run | `inferred`, `method` stated, `confidence` set |
| + external ability metadata | `derived`, source cited |

Never `observed` — the API reports no cooldown timer. Never from a tooltip held
in memory. An NPC's observed recast interval is not its cooldown.

### 5e. Actions

```sql
CREATE TABLE state_actions (
    state_id        TEXT NOT NULL REFERENCES gameplay_states (state_id),
    seq             INTEGER NOT NULL,
    action_id       TEXT,
    ability_game_id INTEGER,
    target_actor_id INTEGER,
    target_instance INTEGER,
    rel_ms          INTEGER NOT NULL,
    source_event_id INTEGER,
    PRIMARY KEY (state_id, seq)
);
```

### 5f. Outcomes at multiple horizons

`state_id` alone as a primary key forced a single window to be chosen before
anyone knew which window answered the question. "Did the player survive" has
different answers at +1 s and +10 s.

```sql
CREATE TABLE state_outcomes (
    state_id         TEXT NOT NULL REFERENCES gameplay_states (state_id),
    horizon_id       TEXT NOT NULL,    -- +1s|+3s|+5s|+10s|mechanic_window|pull_end
    requested_window_ms INTEGER,       -- NULL for semantic horizons
    actual_window_ms INTEGER NOT NULL,
    -- TRUE when the horizon ran past the end of available data. A state 2s
    -- before the pull ends has no +10s outcome -- it has a TRUNCATED one, and
    -- the difference decides whether it belongs in a distribution.
    truncated        INTEGER NOT NULL DEFAULT 0,
    deaths           INTEGER,
    party_hp_delta   TEXT,             -- JSON per player
    damage_taken     INTEGER,
    healing_done     INTEGER,
    enemies_died     INTEGER,
    components       TEXT NOT NULL,    -- JSON: raw components, unaggregated
    coverage         TEXT NOT NULL,    -- JSON: which streams backed each component
    model_version    INTEGER NOT NULL,
    PRIMARY KEY (state_id, horizon_id)
);
```

**Any aggregate over a horizon must report how many members were truncated.**
Without it, "deaths within 10 s" is systematically understated for every state
near a pull boundary — and the bias points the same direction every time, which
is the kind that survives review.

No performance score exists in this schema. Components only.

---

## 6. Cohorts: definitions and evaluations

A definition answers *who matches now*. An evaluation answers *exactly who
produced this published number*. Both are needed and only the second is
reproducible.

```sql
-- Dynamic. Re-evaluates as the corpus grows.
CREATE TABLE cohort_definitions (
    cohort_id        TEXT PRIMARY KEY,
    label            TEXT,
    filters          TEXT NOT NULL,     -- JSON: spec, dungeon, bracket, dimensions, epoch
    required_streams TEXT NOT NULL,     -- JSON: no default; a cohort must say
    dedupe_policy    TEXT NOT NULL,     -- permissive|strict
    model_version    INTEGER NOT NULL,
    created_at       REAL NOT NULL
);

-- Immutable. One row per published result.
CREATE TABLE cohort_evaluations (
    evaluation_id      TEXT PRIMARY KEY,
    cohort_id          TEXT NOT NULL REFERENCES cohort_definitions (cohort_id),
    definition_version INTEGER NOT NULL,
    corpus_fingerprint TEXT NOT NULL,
    model_versions     TEXT NOT NULL,   -- JSON: every analytical version in force
    evaluated_at       REAL NOT NULL,
    dedupe_policy      TEXT NOT NULL,
    provisional        INTEGER NOT NULL DEFAULT 0,
    exclusions         TEXT NOT NULL,   -- JSON: run_id -> reason
    evidence           TEXT NOT NULL    -- JSON: the block in section 7
);

-- The SOURCE set, not the derived set.
CREATE TABLE cohort_evaluation_members (
    evaluation_id TEXT NOT NULL REFERENCES cohort_evaluations (evaluation_id),
    run_id        TEXT NOT NULL,
    actor_id      INTEGER,
    PRIMARY KEY (evaluation_id, run_id, actor_id)
);
```

**Why members are runs and players, never states.** A cohort spanning millions
of states would produce a member list larger than the analysis it documents, and
it would be redundant: states are deterministically re-derivable from the source
set given the model versions the evaluation already records. The source set plus
the versions *is* the reproducible identity.

**Corpus fingerprint:** see §6a. It is content-derived, so changing the evidence
changes the fingerprint.

---

## 6a. The corpus fingerprint

An earlier draft hashed `(run_id, normalizer_version, coverage_status)` per run.
**That is not sufficient**, and the failure is not hypothetical: it proves the
labels are unchanged while the evidence underneath them could differ. A run
re-collected at the same normalizer version, a page repaired after a failure, a
`partial` stream later completed, or a raw payload that changed under a stable
cursor would all leave those three values identical and the events different.
An evaluation pinned to such a fingerprint would claim reproducibility it does
not have.

The objective is exact: **changing the actual evidence must change the
fingerprint.** So the fingerprint digests the evidence.

### Per-run digest

```
run_digest(run_id) = sha256(
    run_id
    ⧺ NORMALIZER_VERSION ⧺ QUERY_VERSION ⧺ SCHEMA_VERSION
    ⧺ IDENTITY_SCHEME_VERSION ⧺ identity_salt_fingerprint
    ⧺ page_component(run_id)
    ⧺ coverage_component(run_id)
)
```

**`page_component`** — the content of the raw evidence, taken from `event_pages`
in a declared order (`data_type`, `hostility`, `source_id`, `target_id`,
`page_index`):

```
for each page:  raw_cache_key ⧺ event_count ⧺ status ⧺ next_cursor_ms
```

`raw_cache_key` is the raw cache's own versioned identity for that page, which
already includes the query, its variables and the cursor. Two collections that
produced byte-different payloads cannot share one.

**`coverage_component`** — from `run_stream_coverage`, ordered by the manifest's
own unique key:

```
for each stream:  data_type ⧺ hostility ⧺ source_id ⧺ target_id
                  ⧺ scope ⧺ pages ⧺ events ⧺ status
```

This is what makes "asked, none" and "never asked" different fingerprints, which
matters because they are different evidence for every statistic downstream.

### Two grades, because they cost differently

| Grade | Built from | Cost | Detects |
|---|---|---|---|
| `page` (default) | cache keys, counts, statuses, cursors | one indexed scan of `event_pages` | re-collection, repair, completion, any change in what was fetched |
| `deep` | + SHA-256 of each normalized event row in `(page_id, seq_in_page)` order | a full scan of `events` | additionally: a normalizer producing different rows from identical payloads at an unchanged version |

`page` is the default because it is cheap enough to compute on every derivation
and catches every change that comes through the API. `deep` exists because the
one thing `page` cannot see is a code change that alters normalization *without*
a version bump — which should never happen, and is exactly the class of mistake a
fingerprint is for. **A published evaluation must be pinned with `deep`.**

Raw payload bytes are deliberately *not* hashed directly: the cache is gzipped
and may legitimately be recompressed or re-fetched without the content changing.
The cache key plus the normalized-row digest covers the same ground without
making a compression detail look like an evidence change.

### Corpus fingerprint

```
corpus_fingerprint = sha256( grade ⧺ concat(sorted(run_digest(r) for r in runs)) )
```

Sorted, so run ordering cannot affect it. Stored with its grade and its run
count, because a fingerprint whose grade is unknown cannot be compared with
another.

### What every analytical row stores

`corpus_fingerprint` on a derived row is the fingerprint of the run set that
produced it. `analytics verify` recomputes and reports drift per run, so the
answer to "is this analysis still valid" is a command rather than an assumption.

---

## 7. Evidence depth and pseudoreplication

Every statistic carries this block. Not a single `N`.

```json
{
  "n_events": 104812,
  "n_states": 3120,
  "n_pulls": 412,
  "n_runs": 88,
  "n_players": 31,
  "n_reports": 10,
  "n_parties": 12,
  "independent_unit": "player",
  "n_independent": 31,
  "concentration": { "top_player_share": 0.18, "top_run_share": 0.04 },
  "coverage": { "required": ["Healing@Friendlies"], "runs_excluded": 6 },
  "dedupe_policy": "strict",
  "provisional": false
}
```

`independent_unit` is declared per claim class and `n_independent` is the count
of *that* unit — the number any interpretation should use.

| Claim class | Independent unit |
|---|---|
| does this NPC cast X | observation |
| recast interval of X | NPC instance |
| target distribution of X | cast |
| pack occurrence and position | pull |
| player behaviour in a window | **player** |
| cohort comparison | **player**, matched |

**`concentration` is what stops `N=100,000` being a lie of composition.** A
distribution where one player supplies 60% of observations is not a population
distribution, however large the event count. There is no global minimum run
count anywhere in this design; each claim reports its own depth and the reader
judges.

---

## 8. Dedupe policy

| Policy | `is_canonical IS NULL` | Use |
|---|---|---|
| `permissive` | allowed, reported | browsing, retrieval, debugging |
| `strict` | **refused** — result marked `provisional`, export blocked | statistics, publication |

**No default at the research boundary.** Every analytical entry point takes the
policy as a required argument. A default of `permissive` means a statistics path
silently gets the lax rule; a default of `strict` means an interactive query
fails for no reason. Making the caller state which kind of question they are
asking is cheap. Guessing is how an undeduplicated corpus ends up underneath a
published number — which is the live corpus's current state.

---

## 9. Analytical projection

Files, not tables.

```
data/analytics/parquet/
  _manifest.json                       version, source fingerprint, run set, built_at
  events/dungeon=<key>/epoch=<id>/stream=<type>/part-*.parquet
  pulls/dungeon=<key>/part-*.parquet
  states/dungeon=<key>/spec=<spec>/part-*.parquet
  dict/{actors,npcs,abilities,items,talents,archetypes}.parquet
```

Partitioned by dungeon, hotfix epoch and stream because those are the three
filters nearly every question applies first. States partition by spec as well,
because cohort questions are spec-first. Dictionary encoding throughout: stable
numeric IDs in fact tables, display names only in `dict/`.

### Determinism is semantic, not byte-level

Revision 1 required byte-identical rebuilds. That was brittle and wrong: Parquet
embeds writer version, codec settings, row-group boundaries and dictionary page
layout, so a routine `pyarrow` bump would fail a correctness gate with no row
changed — which trains people to ignore the gate.

Required instead:

- identical logical rows;
- identical ordering under a declared total order;
- identical partitioning;
- identical source manifest;
- identical **logical fingerprint** — a hash over canonically serialised, sorted,
  explicitly-typed rows, computed without reference to the file format.

The fingerprint is what the test asserts, and it is stable across writer versions
because it never sees the writer. Byte-identity under a pinned environment stays
a **warning**: worth noticing, not worth failing.

---

## 10. Export-scoped identity

No implementation now; the interface is reserved.

Internal pseudonyms are stable by design so longitudinal analysis works — and
that same stability makes them unsafe to publish, since two snapshots sharing an
ID are trivially correlated.

An export pseudonym **cannot** derive from `IDENTITY_SALT`: that salt is public
in this repository, so anyone could recompute the mapping. It must be
`HMAC(random_per_export_salt, internal_pseudonym)`, with the salt generated at
export time and **not included in the export**. Whether the operator keeps the
salt privately is a real choice with a real consequence — keep it and you can
re-identify your own snapshot later; discard it and the mapping is gone for
everyone, including you. The export manifest records which was done, never the
salt.

---

## 11. Version registry

| Model | Constant | Store |
|---|---|---|
| Ingest schema | `SCHEMA_VERSION` = 4 | ingest |
| Normalizer | `NORMALIZER_VERSION` = 2 | ingest |
| Query set | `QUERY_VERSION` = 5 | ingest |
| Identity scheme | `IDENTITY_SCHEME_VERSION` = 1 | ingest |
| Analytics schema | `ANALYTICS_SCHEMA_VERSION` | analysis |
| Build model | `BUILD_MODEL_VERSION` | analysis |
| Canonical actions | `ACTION_MODEL_VERSION` | analysis |
| Pull archetypes | `ARCHETYPE_MODEL_VERSION` | analysis |
| Gameplay state | `STATE_SCHEMA_VERSION` | analysis |
| Cohorts | `COHORT_MODEL_VERSION` | analysis |
| Similarity | `SIMILARITY_MODEL_VERSION` | analysis |
| Projection | `PROJECTION_VERSION` | projection |
| SCL | `SCL_VERSION` = `experimental-1` | export |

---

## 12. Migration sequence

**Ingest store** — additive only; the existing 94 runs need no re-collection.

| # | File | Adds | Backfill |
|---|---|---|---|
| 004 | `identity_scheme.sql` | **applied** — `corpus_identity`, `idx_actors_name` | n/a |
| 005 | `stream_narrowing.sql` | **applied** — `event_pages.source_id` / `target_id` | n/a |
| 006 | `combatant_info.sql` | normalized CombatantInfo | **offline, free** |

Migration 005 was not in the plan. `event_pages` identified a stream by
`(run_id, data_type, hostility)` while `run_stream_coverage` identified it by
that plus the actor narrowing, so a focus-narrowed stream and its party-wide
twin were the same stream to the pagination layer: the focus stream would resume
from the other's cursor, both coverage rows would report the sum of both, and no
event row could be traced back to the request that fetched it. No shipped
profile intersects those two lists today, and the planned `reference_player`
redesign intersects them immediately.

**Analysis store** — `migrations/analytics/`, its own sequence from 001.

| # | Adds |
|---|---|
| 001 | **applied** — scaffolding: `analytics_source`, `derivations`, `derivation_runs`, `derivation_exclusions` |
| 002 | build dimensions, `run_player_config` |
| 003 | canonical actions |
| 004 | pull archetypes |
| 005 | gameplay states, actions, outcomes |
| 006 | cohort definitions, evaluations, members |

Only one migration now touches the canonical corpus, and it is transcription of
data already stored. Everything interpretive lives in a database that can be
deleted without consequence — which is what the layer boundary was supposed to
mean in the first place.

The one thing no migration can backfill: a stream that was never requested.
`run_stream_coverage` names those, per run, and it is why a state reconstructed
from a `mechanics_research` run carries more `unknown` tags than one from
`forensic_full`.
