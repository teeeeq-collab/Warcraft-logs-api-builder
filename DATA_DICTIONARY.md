# Data dictionary

> **Status: PROPOSED DESIGN, NOT IMPLEMENTED.**
> Database schema version is **0**: no tables exist yet. Storage is Phase 1
> work and is deliberately deferred until `wclmplus recon` reveals the real
> event field names (blocker **B1** in `PROJECT_STATE.md`). Building a schema
> around guessed field names is exactly the mistake this ordering avoids.
>
> What *is* implemented and documented below as real: the raw cache envelope,
> the pagination checkpoint, and the provenance block.

---

## Conventions

### Timestamps — three kinds, never conflated

| Name | Meaning | Units |
| --- | --- | --- |
| `report_timestamp_ms` | Milliseconds since the report's own start. What the API returns on events. | integer ms |
| `absolute_timestamp_ms` | Unix epoch milliseconds. `report.startTime + report_timestamp_ms`. Needed for hotfix epochs and date filtering. | integer ms |
| `run_relative_ms` | Milliseconds since the run (fight) started. | integer ms |
| `pull_relative_ms` | Milliseconds since the containing pull started. `NULL` when the event falls outside every pull. | integer ms |

All four are stored per event. Deriving them later is cheap; recovering a
discarded one is impossible. Millisecond resolution is preserved everywhere:
overlap analysis at ±1s is a stated research requirement, so no ingestion-time
bucketing is permitted.

### Null semantics

`NULL` always means **"the API did not supply this"**, never zero, never
"false", never "not applicable". A count that is genuinely zero is stored as
`0`. This distinction decides whether a mechanic was absent or merely
unobserved.

### Raw vs derived

Every column is tagged:

- **raw** — as the API returned it, unmodified
- **derived** — computed by this project; carries `normalizer_version`
- **provenance** — how the row came to exist

Derived values must be reproducible from raw values plus the raw cache. If a
derived column cannot be recomputed from what is stored, the raw input is
missing and must be added.

---

## Implemented: raw cache envelope

Every cached response (`data/raw_cache/<kind>/<report_code>/<hash>.json.gz`):

| Field | Kind | Notes |
| --- | --- | --- |
| `cache_format_version` | provenance | Envelope layout version. Currently 1. |
| `kind` | provenance | Logical request name, e.g. `events_sample_Casts`. Part of the cache key. |
| `report_code` | provenance | `NULL` for non-report queries; those live under `_global/`. |
| `params` | provenance | Query variables, **redacted**. Any `Authorization`-like key is stripped before storage. |
| `query_version` | provenance | Part of the cache key: bumping it invalidates stale responses after a query edit. |
| `fetched_at` | provenance | Unix seconds. |
| `provenance` | provenance | Software, normalizer, query and schema versions; git commit; dirty-tree flag. |
| `response` | raw | The GraphQL `data` object, verbatim. |

Cache identity = SHA-256 of `{kind, query_version, params}`, truncated to 32
hex characters. Because `params` includes the page cursor, two pages of one
query never collide.

**Never stored:** client secret, access token, `Authorization` header. Enforced
by scanning the serialized bytes before writing and refusing the write on a
hit — a credential leak becomes a loud failure, not a file.

---

## Implemented: pagination checkpoint

Persisted per paginated query so an interrupted download resumes exactly:

| Field | Kind | Notes |
| --- | --- | --- |
| `kind` | provenance | Logical query name. |
| `report_code` | provenance | |
| `variables` | provenance | Query variables for the resumed request. |
| `next_start_time` | derived | Cursor to resume from. `NULL` once complete. |
| `pages_fetched` | derived | Cumulative across resumes. |
| `events_emitted` | derived | Cumulative count actually yielded. |
| `boundary_timestamp` | derived | Timestamp of the last page's final event. |
| `boundary_counts` | derived | Fingerprint → count of events already emitted at that timestamp. **Required for correctness on resume**: without it, a resume re-emits the boundary events. |
| `complete` | derived | True when the cursor came back null. |

---

## Implemented: provenance block

Recorded in every cache envelope, recon report and (later) collection job:

| Field | Notes |
| --- | --- |
| `software_version` | Collector version. |
| `normalizer_version` | Bumped when raw → normalized mapping changes. |
| `query_version` | Bumped when `queries/*.graphql` change meaningfully. |
| `schema_version` | Database schema version. `0` in Phase 0. |
| `git_commit` | Short commit, or `NULL` outside a repo. |
| `git_dirty` | `true` if uncommitted changes exist, so a recorded commit is known not to describe the code fully. |

---

## Proposed tables (Phase 1)

Names are negotiable; the concepts are not. Column lists will be finalized
against real event shapes after Gate A.

### `collection_jobs`
Job ID, config hash, sample profile, event profile, start/end, status,
software/normalizer/query/schema versions, git commit, reports attempted,
reports completed, failures.

### `report_provenance`
Report code, discovery source type, seed, discovery timestamp, rank, page.
**One row per discovery event**, so a report found by two sources keeps both
provenance records — necessary to assess uploader and leaderboard bias.

### `reports`
Report code (PK), absolute start/end, visibility, archive status, accessibility,
zone, revision, log version, retrieval time, owner pseudonym.

### `dungeon_runs`
Run ID (PK), report code, WCL fight ID, dungeon key, WCL zone ID, game zone ID,
relative and absolute start/end, duration, key level, affixes, keystone bonus,
keystone time, rating, count reached/required, average item level, kill state,
timed flag, **hotfix epoch**, duplicate group ID, canonical-run flag, collection
status.

`collection_status` is an enum, not a boolean: `complete`, `metadata-only`,
`archived-events-unavailable`, `inaccessible`, `partial-run`, `malformed`,
`collection-failed`, `validation-failed`. **Only `complete` runs may enter an
event-frequency denominator.**

### `players`
Local player ID (PK), stable identity hash, class, spec, role, item level,
talent import code. **No names.** The identity hash is salted with a fixed,
non-secret project salt so the same player matches across reports — which is
what roster-based duplicate detection needs.

### `run_players`
Run ID, player ID, report actor ID, role, spec, item level. Resolves the
per-report actor ID to a stable player.

### `actors`
Report code, actor ID (composite PK), type, subtype, game ID, pet owner,
player/NPC flag, name (NPCs only — player names are pseudonymized).

### `abilities`
Game ability ID (PK), name, icon, type, first seen, last seen.

### `pulls`
Pull ID (PK), run ID, pull index, WCL pull ID, encounter ID, relative and
absolute start/end, duration, x, y, map IDs, bounding box, pull name, kill
state, **composition signature**, previous/next pull ID.

The composition signature is a canonical string of NPC game IDs and
multiplicities (`npcA×1|npcB×2|npcC×1`) used to group similar pulls. Two pulls
with the same signature at different coordinates are **not** assumed identical —
x/y, map and sequence are retained precisely so route context is not lost.

### `pull_npcs`
Pull ID, NPC game ID, report actor ID, minimum/maximum instance ID,
minimum/maximum instance group ID, derived multiplicity.

Derived multiplicity is marked derived and carries a confidence flag: it is
inferred from the instance-ID range, which is an inference, not a fact.

### `event_pages`
Page ID (PK), run ID, event category, requested start/end, page cursor,
raw-cache path, event count, next cursor, success flag, error text.

The audit trail for pagination: which pages were requested, which succeeded,
and where the raw response lives.

### `events`
The core table. Minimum columns:

report code, run ID, pull ID (nullable), report-relative ms, absolute ms,
run-relative ms, pull-relative ms, event type, source actor ID, **source
instance**, target actor ID, **target instance**, ability game ID, amount,
absorbed, overkill, mitigated/blocked/resisted, hit type, tick flag, stack
count, extra ability game ID, `extra` (JSON), raw-cache reference,
normalizer version.

`extra` holds every field not promoted to a column, as JSON. This is deliberate:
forcing every event into a fixed schema would discard fields whose value is not
yet understood, and the whole point is to answer questions not yet asked.

`source_instance` and `target_instance` are **never** dropped or defaulted.
They are the only thing separating two copies of one NPC species.

### Derived views (reproducible from `events`)
`casts`, `aura_transitions`, `dispels`, `interrupts`, `damage`, `healing`,
`deaths`, `summons`.

Views, not authored tables, so a change to interpretation is a re-derivation
rather than a re-download. Each must be reconstructible from `events` plus the
raw cache alone.

---

## Semantics that must not be lost

| Rule | Reason |
| --- | --- |
| A cast start and its completion are **one** mechanic occurrence. | Counting both doubles every cast statistic. |
| A true interrupt is raw; a CC "stop" is **derived with a confidence score**. | An incomplete cast may be an interrupt, a mob death, target loss, a phase change, or movement. Labelling all of them CC stops is wrong. |
| Aura removal is not automatically a dispel. | Natural expiry and dispel are different events with different research meaning. |
| Mob death time and pull end time are stored so observations can be **censored**. | A mob that dies nine seconds after casting gives no evidence about a cooldown longer than nine seconds. |
| Key level alone never explains cast counts. | The usual mechanism is: higher key → more health → longer pull → room for another cast. Pull duration and mob lifetime must be available alongside key level. |
| Hotfix epoch is stored per run. | Mechanics change mid-season; pooling epochs silently would contaminate current-behaviour conclusions. |
