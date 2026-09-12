# Data dictionary

Schema version **1** (`migrations/001_initial.sql`). Built against event and
field shapes **observed** in five live recon runs, not against guesses.

---

## Conventions

### Timestamps — four columns per event, never conflated

| Column | Meaning | Units |
| --- | --- | --- |
| `rel_ms` | Milliseconds since the **report** started. What the API returns. | integer ms |
| `abs_ms` | Unix epoch milliseconds. `report.start_time_ms + rel_ms`. Needed for hotfix epochs and date filtering. | integer ms |
| `run_rel_ms` | Milliseconds since the **run** started. | integer ms |
| `pull_rel_ms` | Milliseconds since the **pull** started. `NULL` when the event falls outside every pull. | integer ms |

All four are stored. Deriving one later is cheap; recovering a discarded one is
impossible. Millisecond resolution is preserved throughout: overlap analysis at
±1 s is a stated research requirement, so no ingestion-time bucketing happens.

### NULL always means "the API did not supply this"

Never zero, never false, never "not applicable". A genuine zero is stored as
`0`. The distinction decides whether a mechanic was **absent** or merely
**unobserved** — and those support opposite conclusions.

The sharpest case is `events.source_instance`. A missing value probably means
the NPC had only one copy, but that is an inference about the *pull*, not a
fact about the event. It stays `NULL` and is resolved during analysis against
`pull_npcs.min_instance_id`/`max_instance_id`: one copy in the pull, the NULL
maps to it; several copies, the attribution is genuinely unknown and must be
reported as unknown.

### Raw, derived, provenance

Every column is one of:

- **raw** — as the API returned it
- **derived** — computed here; carries `normalizer_version`, and where the
  derivation is an inference it carries its own confidence column
- **provenance** — how the row came to exist

A derived value must be reproducible from raw values plus the raw cache.

---

## `collection_jobs` — provenance

`job_id`, `started_at`, `finished_at`, `status` (`running` | `complete` |
`failed` | `interrupted`), `sample_profile`, `event_profile`, `config_hash`,
`software_version`, `normalizer_version`, `query_version`, `schema_version`,
`git_commit`, `git_dirty`, `reports_attempted`, `reports_completed`,
`reports_failed`, `notes`.

Every row written during a job can be traced to the exact code and
configuration that produced it.

## `report_provenance` — how each report entered the corpus

`report_code`, `source_type`, `seed`, `discovered_at`, `rank`, `page`,
`job_id`, `extra` (JSON).

**One row per discovery event, not per report.** A report found by both a
manual list and zone-scoped discovery keeps both records — which is what makes
uploader and leaderboard bias assessable after the fact. Uniqueness is
`(report_code, source_type, seed, job_id)`, so re-running one job does not
invent a second discovery.

## `reports`

`report_code` (PK), `title_hash`, `owner_hash`, `start_time_ms`, `end_time_ms`,
`zone_id`, `zone_name`, `region_id`, `region_slug`, `revision`, `segments`,
`visibility`, `is_archived`, `is_accessible`, `archive_date`, `log_version`,
`game_version`, `retrieved_at`, `collection_status`.

Report **titles are hashed**: they routinely contain player and guild names.

## `dungeon_runs`

`run_id` (PK, `"<report_code>:<fight_id>"`), `report_code`, `fight_id`,
`dungeon_key`, `encounter_id`, `game_zone_id`, `game_zone_name`, `wcl_zone_id`,
`rel_start_ms`, `rel_end_ms`, `abs_start_ms`, `abs_end_ms`, `duration_ms`,
`keystone_level`, `keystone_affixes` (JSON), `keystone_bonus`,
`keystone_time_ms`, `rating`, `count_reached`, `count_required`,
`average_item_level`, `kill`, `timed`, `size`, `npc_count_map` (JSON),
`hotfix_epoch`, `key_bracket`, `duplicate_group_id`, `is_canonical`,
`collection_status`, `job_id`.

`timed` is **derived** from `keystone_bonus > 0`. It stays `NULL` when the
bonus is unknown: "not timed" and "unknown" are different facts.

`collection_status` is an enum, not a boolean:

`complete` · `metadata-only` · `archived-events-unavailable` · `inaccessible` ·
`partial-run` · `malformed` · `collection-failed` · `validation-failed`

**Only `complete` runs may enter an event-frequency denominator.**

## `players` and `run_players`

`players`: `player_id` (PK, salted hash), `first_seen`, `last_seen`,
`run_count`. **No names.**

`run_players`: `run_id`, `actor_id`, `player_id`, `class`, `spec`, `role`,
`item_level`.

Class and spec live on the *run*, not the player: a player brings a different
character or spec to each run. `role` comes from `config/roles.yml`, because
Warcraft Logs reports a spec but no role and role is game knowledge.

> **Limitation.** Master data exposes no realm for players, so `player_id` is a
> hash of the character name alone. Two same-named characters on different
> realms collide. This weakens roster-based duplicate detection slightly and is
> reported in every validation run.

## `actors`

`report_code` + `actor_id` (PK), `game_id`, `name`, `type`, `sub_type`, `icon`,
`pet_owner`, `is_player`.

**NPC names are kept** — they are the subject of the research. Player and pet
names are pseudonymized at the ingest boundary, so no downstream table or
export can leak one.

## `abilities`

`game_id` (PK), `name`, `icon`, `type`, `first_seen`, `last_seen`.

`gameID` arrives as a float and is truncated to an integer.

## `pulls`

`pull_id` (PK, `"<run_id>:<wcl_pull_id>"`), `run_id`, `wcl_pull_id`,
`pull_index`, `name`, `encounter_id`, `is_boss`, the four time bases,
`duration_ms`, `kill`, `x`, `y`, `map_ids` (JSON), `bounding_box` (JSON),
`composition_signature`, `npc_species_count`, `npc_total_count`,
`prev_pull_id`, `next_pull_id`.

Warcraft Logs' own pull boundaries are authoritative; no combat-gap detector
exists. `is_boss` is derived from a non-zero `encounter_id`.

`composition_signature` is canonical: `"236085x18 | 236084x2"`, sorted by game
ID. Two pulls with the same signature at different coordinates are **not**
assumed identical — `x`, `y`, `map_ids` and `pull_index` are retained so route
context survives into analysis.

## `pull_npcs`

`pull_id`, `npc_game_id`, `actor_id`, `min_instance_id`, `max_instance_id`,
`min_instance_group_id`, `max_instance_group_id`, `instance_count`,
`instance_count_confidence`.

`instance_count` is **derived** from the instance-ID range and always carries
its confidence (`exact` | `inferred` | `unknown`), because a range is an
inference about multiplicity, not a reported count. Absent instance IDs yield
`(1, "inferred")`; a half-present range yields `(NULL, "unknown")`.

Observed in live data: one pull held **18 copies** of NPC 236085.

## `event_pages` — the pagination audit trail *and* the checkpoint

`page_id` (PK), `run_id`, `data_type`, `hostility`, `page_index`,
`requested_start_ms`, `requested_end_ms`, `cursor_ms`, `next_cursor_ms`,
`event_count`, `raw_cache_path`, `status`, `error`, `fetched_at`.

**Resume state lives here, not in a side file.** A page row is written in the
same transaction as its events, so the last recorded page is by construction
the last one whose events actually landed. There is no checkpoint that can
disagree with the data. A stream is finished when its last page has
`next_cursor_ms IS NULL`.

## `events`

Identity is `(page_id, seq_in_page)`. A re-fetched page **replaces** its own
events rather than appending beside them, which is what makes re-ingest
idempotent without needing a content hash — and a content hash would have been
wrong anyway, since two genuinely identical events can occur at one
millisecond.

| Column group | Columns |
| --- | --- |
| Identity | `event_id`, `page_id`, `seq_in_page`, `run_id`, `report_code`, `pull_id`, `data_type`, `hostility` |
| Time | `rel_ms`, `abs_ms`, `run_rel_ms`, `pull_rel_ms` |
| Actors | `type`, `source_id`, `source_instance`, `target_id`, `target_instance` |
| Abilities | `ability_game_id`, `extra_ability_game_id` |
| Damage | `amount`, `absorbed`, `blocked`, `mitigated`, `unmitigated_amount`, `overkill`, `hit_type`, `is_tick`, `is_aoe` |
| Auras | `stack`, `is_buff`, `buffs` |
| Health | `hit_points`, `max_hit_points` |
| Deaths | `killer_id`, `killer_instance`, `killing_ability_game_id` |
| Position | `x`, `y`, `map_id` |
| Everything else | `extra` (JSON), `normalizer_version` |

Three columns deserve their own note, because each answers a question the brief
expected to be unanswerable:

- **`max_hit_points`** is on every damage event, so damage as a fraction of
  player health is directly computable. No estimation needed.
- **`buffs`** is a dot-separated list of aura IDs active on the target at the
  instant of the hit (`"384072.386208.132404."`). "Was a defensive up for this
  tankbuster" is a lookup, not a correlation across timelines.
- **`unmitigated_amount`** beside `mitigated` separates what the mob swung for
  from what the tank actually took — the difference between measuring danger
  and measuring mitigation.

`extra` holds every field not promoted, verbatim. Forcing events into a fixed
schema would discard fields whose value is not yet understood, and the point is
to answer questions not yet asked. New field names appearing in the API are
raised as an `unexpected_event_fields` diagnostic rather than dropped silently.

### Indexes

Chosen for the queries the research actually runs, not speculatively:

| Index | Serves |
| --- | --- |
| `(pull_id, rel_ms)` | per-pull mechanic timelines |
| `(run_id, source_id, source_instance, rel_ms)` | **per-NPC-copy cast recurrence** |
| `(run_id, target_id, rel_ms)` | death windows, per-player damage |
| `(ability_game_id, type)` | mechanic frequency across the corpus |

## `ingest_diagnostics`

`job_id`, `run_id`, `kind`, `severity`, `detail`, `created_at`.

Diagnostics are **data, not logging**. A validation report is only trustworthy
if what could not be done is recorded beside what could. Kinds currently
emitted: `report_unavailable`, `archived_report`, `no_matching_runs`,
`no_pulls`, `overlapping_pulls`, `events_outside_pulls`,
`unexpected_event_fields`, `empty_roster`, `event_collection_failed`,
`report_failed`, `duplicate_candidate`, `pagination`, `selection`.

---

## Semantics that must not be lost

| Rule | Reason |
| --- | --- |
| A cast start (`begincast`) and its completion (`cast`) are **one** mechanic occurrence. | Counting both doubles every cast statistic. Both are stored; analysis must not sum them. |
| A true interrupt is raw; a CC "stop" is **derived with a confidence score**. | An incomplete cast may be an interrupt, a mob death, target loss, a phase change, or movement. Labelling all of them CC stops invents data. |
| Aura removal is not automatically a dispel. | `dispel` and `removedebuff` are different events with different research meaning. `is_buff` and `extra_ability_game_id` disambiguate. |
| Mob death time and pull end time are stored so observations can be **censored**. | A mob that dies nine seconds after casting gives no evidence about a cooldown longer than nine seconds. |
| Key level alone never explains cast counts. | The usual mechanism is: higher key → more health → longer pull → room for another cast. `duration_ms` and pull duration must accompany `keystone_level` in any such claim. |
| `hotfix_epoch` is stored per run. | Mechanics change mid-season; pooling epochs silently would contaminate current-behaviour conclusions. |
| Events outside every pull are **kept**. | Between-pull events are where movement, drinking and out-of-combat deaths live. Dropping them would hide why a pull started badly. |

---

## Not yet implemented

Derived views (`casts`, `aura_transitions`, `dispels`, `interrupts`, `damage`,
`deaths`) and Parquet export are Phase 5. They will be **views**, not authored
tables, so a change in interpretation is a re-derivation rather than a
re-download — and each must be reconstructible from `events` plus the raw cache
alone.
