# Changelog

Collector, schema, normalization and analysis changes.

Version stamps that matter for reproducibility: `software_version`,
`schema_version`, `normalizer_version`, `query_version`. Any change that alters
what gets collected or how it is interpreted must bump the relevant one and be
recorded here.

---

## Unreleased — 2026-09-14 — **focus resolution, identity versioning, design package**

- `schema_version` 3 → **4** (`migrations/004_identity_scheme.sql`)
- 361 → **377 tests**, all offline
- New: `identity_scheme_version`, carried in `provenance()`

### Fixed

**`--focus-player` could never resolve an actor.** `normalize_actors` stores
players as `pseudonym(name)`, and `_resolve_focus_actor` compared the supplied
character name against `actors.name` directly. No real name can match a salted
hash, so every focus stream was skipped on every run, for every focus player.
A `reference_player` collection reported success and contained no focus data;
the only trace was a per-run diagnostic that read like an ordinary absence.

The typed name now goes through the same pseudonym function before matching,
and both spellings a person has to hand are accepted: a character name with or
without a `-Realm` suffix, and a pseudonym copied from a validation report.

**Absence and failure are now different things.** Missing from one run is
normal and stays a warning. Matching nothing in *any* run is a failed request
and ends the job with an error diagnostic, an error on the result and exit
code 2.

**A real character name no longer reaches the database.** The absence
diagnostic recorded the name the operator typed. It now records the pseudonym,
which is enough to check the lookup and keeps the corpus free of real names.

### Added

**Identity scheme versioning.** Pseudonyms are the only handle the corpus has
on a person, so the pseudonym function is part of the data format: change the
salt and every stored player silently becomes someone else. `corpus_identity`
records the scheme and salt fingerprint a database's names were written under,
and a job run under a different scheme stops with a message naming the recovery
path. A pinned test asserts the digest, so the scheme cannot drift unnoticed.

`idx_actors_name`, so focus resolution is an index lookup rather than a scan of
the largest dimension table.

### Documentation

**Design package revision 2**, reconciling the four documents against the
architecture review's 17 amendments. `ARCHITECTURE_REVIEW_RESPONSES.md` records
a response to each: 16 agree, 0 disagree, and three corrections to errors of
mine that the review caught.

The three that were defects rather than refinements:

- **The layer boundary was a convention, not a fact.** Revision 1 said Layer 2
  never writes to Layer 1 and then put four derived tables in the ingest
  database. The analytical store is now a separate database file with its own
  migration sequence and version, attached read-only for derivation.
- **Build identity hashed talents and gear together**, which made item level a
  talent variable. Two players with identical talents at ilvl 681 and 684 became
  different builds, so a cohort query would have reported N=1 for a
  configuration dozens of players ran. Replaced by five separable dimensions.
- **Byte-identical Parquet was required.** Parquet embeds writer version and
  row-group layout, so that would have pinned pyarrow forever and failed a
  correctness gate on a dependency bump with no row changed. Determinism is now
  asserted on a logical fingerprint computed without reference to the file
  format.

Also retracted: the claim that numbers from the 94-run corpus would be
"statistically meaningless". Evidence quality depends on the claim. Recast
intervals have NPC instances as their independent unit and are supported today;
player-behaviour claims have people as theirs and are not. Every statistic now
reports seven counts plus concentration rather than a single N.

Four design documents answering the expanded architecture brief:
`SHIFU_ARCHITECTURE_PLAN.md`, `ANALYTICAL_DATA_MODEL.md`,
`SCL_EXPERIMENT_PLAN.md`, `IMPLEMENTATION_PHASES.md`. All are proposals; none
of the work they describe has been built.

`API_NOTES.md` corrected: the per-page probe implied ~37 points/run and the
conclusion "quota, not wall clock, is the binding constraint". A real 94-run
corpus measured **13.19 points/run**, which reverses it. The estimate is kept
beside the measurement, with the reason it was wrong.

`PROJECT_STATE.md` rewritten — it still described schema 1, normalizer 1 and a
pipeline that had never ingested a real report.

---

## 0.2.0 — 2026-09-12 — **Phase 1: the collector**

Storage, ingestion and validation. Built against the event shapes observed in
Phase 0, and tested end to end offline; it has not yet ingested a real report.

- `software_version` 0.1.5 → 0.2.0
- `schema_version` 0 → **1** (`migrations/001_initial.sql`)
- 247 → **317 tests**, all offline

### Added

**Storage** — 14-table SQLite schema with a numbered migration runner, enforced
foreign keys, WAL journaling and batched writes. Every table choice traces to
an observed field; indexes are chosen for the queries the research actually
runs, notably `(run_id, source_id, source_instance, rel_ms)` for per-NPC-copy
cast recurrence.

**Normalizer** — maps real report, fight, pull, NPC, master-data and event
shapes to rows. Four timestamp bases per event (report-, absolute-, run- and
pull-relative). Player and pet names pseudonymized at the ingest boundary; NPC
and ability names kept. Unpromoted fields preserved verbatim in `extra`, and a
field name the API starts returning that this project does not model is raised
as a diagnostic rather than dropped.

**Collector** — report → run → pulls → NPC instances → roster → events, with:

- *Exact resume.* The pagination checkpoint is the `event_pages` table, and a
  page row is written in the same transaction as its events. The last recorded
  page is therefore by construction the last one whose events landed; there is
  no checkpoint that can disagree with the data.
- *Idempotent re-ingest.* Running a job twice changes no row counts. Event
  identity is `(page_id, seq_in_page)`, not a content hash — two genuinely
  identical events can occur at one millisecond, and a hash would merge them.
- *Explicit failure states.* Archived, missing and partial reports are recorded
  as states, never as silence.

**Pull assignment** — Warcraft Logs' own boundaries, closed intervals, binary
search. Events outside every pull are **kept**, counted and reported: they are
where movement, drinking and out-of-combat deaths live. Overlapping pull
intervals are reported rather than silently resolved.

**Duplicate detection** — multi-field fingerprint (dungeon, key level, start
time, duration, roster overlap, affixes) with transitive grouping. Deliberately
biased toward false negatives: **nothing is ever deleted**, one member per
group is marked canonical, and a "same group runs the key again" case is
correctly *not* a duplicate.

**Validation report** — JSON plus Markdown, covering run counts by status,
bracket and epoch; event totals and pull-assignment rate; unknown actors and
abilities; pagination completeness; duplicate groups; ingest diagnostics; and
known limitations. Its central section **reconstructs a per-NPC-copy cast
timeline from the database**, so "instance identity survived collection" is
demonstrated rather than asserted — and when no such pull exists, the report
says the claim is untested rather than passing silently.

**CLI** — `collect` (with `--dry-run`, `--refresh`, `--dungeon`,
`--max-runs-per-report`), `dedupe`, `validate`, `stats`.

**Config** — `config/roles.yml` maps spec to role, because Warcraft Logs
reports a spec but no role and role is game knowledge that changes between
expansions.

### Changed

- Event profiles accept a hostility filter (`Casts@Enemies`). This is a
  finding, not a tuning choice: an unfiltered cast sample returned 50 player
  casts and zero NPC casts, which would have made NPC mechanic timelines
  invisible. The mechanics profile now fetches enemy and friendly casts as
  separate streams.
- `report_provenance` is unique per *discovery event*
  `(report_code, source_type, seed, job_id)`, so two sources finding one report
  keep two rows while re-running one job does not invent a second.

### Known limitation

Player identity is a hash of the character name alone: master data exposes no
realm, so two same-named characters on different realms collide. This slightly
weakens roster-based duplicate detection and is stated in every validation run.

### Not yet done

**No real report has been ingested.** The pipeline is proven against the
synthetic API only. Phase 1 closes when 5–10 real Murder Row runs pass
validation and Gate C is assessed.

---

## 0.1.5 — 2026-09-12 — **Gate A closed**

The last Phase 0 unknown confirmed. No code changes; documentation and version
stamp only.

### Verified against the live API

- **Enemy cast events carry `sourceInstance`.** `hostilityType: Enemies` on one
  +10 Murder Row fight returned 11 distinct NPC actors with **36 of 50** events
  carrying the instance marker, and both `cast` and `begincast` — so cast start
  and cast completion are separately observable, which distinguishes an
  interrupted cast from a completed one without inference.

  The unfiltered sample of the same fight had returned five players and zero
  instance markers. **Per-NPC-copy cast timelines are supported.**

- 14 of 50 enemy casts carried no `sourceInstance`, almost certainly because
  only one copy of that NPC existed. Recorded as an inference, not a fact: the
  field is stored NULL and resolved against the pull's instance range during
  analysis. Where a pull holds several copies of the species, the attribution
  is unknown and is recorded as unknown rather than defaulted to instance 1.

- Enemy cast events also carry `hitPoints`/`maxHitPoints`, so enemy health may
  be readable directly from enemy casts — relevant to the execute-phase
  questions, and untested.

---

## 0.1.4 — 2026-09-12 — **Gate A passed**

Fourth live run: 16 of 16 steps, 0 failures. Pulls, NPC identity, event shapes,
pagination and cost are now observed rather than assumed.

- `software_version` 0.1.3 -> 0.1.4
- `query_version` 3 -> 4 (enemy-cast probe added)

### Verified against the live API

- **Pagination cursor is EXCLUSIVE.** A 316-page traversal of one +10 fight
  returned 7,920 events and emitted 7,920: nothing lost, nothing
  double-counted, no out-of-order timestamps, no warnings. The multiset
  boundary matching is therefore not load-bearing on this endpoint; it stays,
  because it costs nothing under an exclusive cursor and is the difference
  between correct and corrupt if that ever changes.
- **NPC instance identity is real in events**: `sourceInstance` on Debuffs and
  DamageTaken, `targetInstance` on Interrupts and Summons, `killerInstance` on
  Deaths, both on Buffs. At pull level, one pull contained **18 copies** of NPC
  236085 — merging those would have manufactured seventeen phantom recasts.
- **Damage events carry more than the brief hoped for**: `maxHitPoints` (so
  damage as a fraction of player health is reconstructible, no guessing),
  `buffs` (aura IDs active on the target at the moment of the hit, so defensive
  uptime is a lookup rather than a correlation), and `unmitigatedAmount`
  alongside `mitigated` (separating what the mob swung for from what the tank
  took).
- `extraAbilityGameID` on interrupts and dispels names *what was interrupted*
  and *what was removed*.
- **Cost is not the constraint.** The whole run cost ~29 points of 3600/hour;
  316 event pages ≈ 0.05 points each. Time and bandwidth bind first: 97 s and
  3.3 MB. `min_points_reserve: 200` is over-cautious and can be revisited.
- Timestamps are integer milliseconds relative to report start. `log_version`
  17.
- Report discovery confirmed across three runs; unscoped results carry
  `zone: null`, so **zone-scoped discovery is the one to build sampling on**.

### Added

- **Enemy-cast probe.** The unfiltered Casts sample came back as 50 player
  casts and not one enemy cast: five players out-cast the trash in raw event
  count, and players are not instanced, so the sample could say nothing about
  NPC cast timelines. Recon now samples `hostilityType: Enemies` separately and
  counts how many events carry `sourceInstance`, raising a limitation if none
  do — in which case per-instance recast timing is not supported by the API and
  must be recorded as such rather than inferred from cast ordering.
- Event samples now record `distinct_source_ids` and
  `events_with_source_instance`, so a player-dominated sample is visible as
  such instead of looking like an answer.

### Note for collection

An unfiltered event query is dominated by players. `hostilityType` is not an
optimisation for this project; it is what makes the enemy side visible at all.

---

## 0.1.3 — 2026-09-12

Third live run. One more gated field, and a fix to the strategy rather than the
symptom.

- `software_version` 0.1.2 → 0.1.3
- `query_version` 2 → 3 (generated selections narrowed; cached responses from
  the old queries are invalidated)

### Fixed

- **Permission-gated nested fields, properly this time.** The third run failed
  on `User.battleTag`, having failed on `User.avatar` the run before. Two
  compounding faults: expanding a composite to *every* scalar asked for far
  more than the research needs, and the drop-and-retry never fired because it
  matched the schema spelling `battleTag` against prose reading "battle tag".

  - **Identity-first expansion**: when a type exposes `id`, `name`, `slug` or
    `compactName`, only those are selected. `User` reduces to `{ id name }`,
    making gated extras unreachable whatever they are named. Types with no
    identity leaves (`ReportArchiveStatus`, `ReportMapBoundingBox`) still
    expand fully — archived-report detection depends on those fields.
  - **Normalized blame matching**: `battleTag` now matches "battle tag",
    "Battle-Tag" and "battletag". Names under four characters stay strict, or
    a normalized `id` would match "invalid".
  - **Most-specific match only**: "compact name" contains the word `name`, so
    a naive scan dropped a field the server never objected to.

- **Cache identity ignored the query text.** Responses were keyed on the query
  *name*, variables and `query_version`, but queries are generated from
  introspection and the same name can produce a different document between
  runs. A stale response answering a different question could be served. The
  rendered query's hash is now part of the key.

### Verified against the live API

- All eight Season 2 dungeons resolved to zone 55 and were written to
  `config/dungeons.discovered.yml`, including the three reused names, each via
  season-zone resolution.
- The raw cache works: the third run served 20 of 22 requests from disk and
  issued only 2.

### Still unverified

Event shape, pagination semantics, per-instance identity *in events*, pull
content and event-page cost.

---

## 0.1.2 — 2026-09-12

Second live run. Two more defects found and fixed; the season is identified and
broad report discovery is confirmed.

- `software_version` 0.1.1 → 0.1.2
- `query_version` unchanged at 2
- `schema_version` unchanged at 0

### Fixed

- **A permission-gated field inside an auto-expanded selection.** Expanding
  every composite to *all* its scalar fields pulled in `User.avatar`, which is
  permission-gated in a way introspection does not reveal. The API returned
  partial data plus `You do not have permission to view the avatar for this
  user.`, and the client correctly refused to treat half an answer as
  complete — so the report step failed again, on a new cause.

  Two layers: a deny-list of observed-gated and media field names
  (`SchemaIntrospector.RISKY_LEAF_NAMES`), and `Recon._execute_with_leaf_retry`,
  which drops any leaf the server names in an error and retries. The deny-list
  can only hold what is already known to fail; the retry covers the rest, so an
  unanticipated gated field costs one extra request rather than a whole round
  trip. Dropped fields are recorded as limitations.

- **Reused dungeon names were reported as unresolvable.** Ruby Life Pools also
  exists in Dragonflight S1 and S4; Kings' Rest and Temple of Sethraliss also
  exist in Battle for Azeroth. Name-only matching refused to choose — safe, but
  it left season dungeons unidentified. `match_zones` now runs two passes:
  match the dungeons unique to the season, infer the season zone from where
  they agree (or read `season.wcl_mplus_zone_id` from config), then resolve the
  reused names inside that zone. All eight now resolve with no ambiguity, and a
  name still ambiguous after pass 2 is reported rather than guessed.

### Changed

- `config/dungeons.yml` now lists all **eight** Midnight Season 2 dungeons,
  using Warcraft Logs' exact spellings with alternatives as aliases (notably
  `Kings' Rest`). Every numeric ID stays `null`: IDs come from
  `discover-dungeons --write`, never from source.

### Verified against the live API

- **Midnight Season 2 is zone 55** (expansion 7, partition S2, not frozen),
  with eight encounters: Altar of Fangs (12993), Den of Nalorakk (12825),
  Kings' Rest (61762), Murder Row (12813), Ruby Life Pools (112521), Temple of
  Sethraliss (61877), The Blinding Vale (12859), Voidscar Arena (12923).
  Confirmed independently by the project owner.
- **Report discovery works without a guild or user scope**, both unscoped and
  zone-scoped, with `has_more_pages: true`. Representative season-wide sampling
  is therefore possible — the most consequential finding for the research
  design, since it removes the forced leaderboard and single-guild bias.
- `ReportPagination.total` is **`-1`**: not a usable denominator. Sample size
  must be counted from what is actually fetched.
- Eight expansions, newest first, Midnight (7) current.
- Cost: 22 requests ≈ 23 points of 3600 per hour.

### Still unverified

Event shape, pagination semantics, per-instance identity *in events*, pull
content and event-page cost. All need one more recon run.

---

## 0.1.1 — 2026-09-12

First live API contact. Two defects found and fixed, plus one reporting bug.

- `software_version` 0.1.0 → 0.1.1
- `query_version` 1 → 2 (report metadata, fights and pull queries now carry
  introspection-derived sub-selections; a new discovery probe query was added)
- `schema_version` unchanged at 0
- `normalizer_version` unchanged at 1

### Fixed

- **Composite fields were selected without a sub-selection.** The live API
  rejected the report query with `Field "archiveStatus" of type
  "ReportArchiveStatus" must have a sub selection.`, aborting recon before
  fights, pulls, events, pagination and cost were probed.

  Root cause: sub-selections came from a hardcoded map of field names *assumed*
  to be objects, which is exactly the guessing this project forbids.
  `SchemaIntrospector.build_selection` now asks introspection for each field's
  type kind and expands any composite to its scalar and enum fields. A
  composite offering no scalars is skipped and recorded rather than sent bare.
  Classification reads the kind from the field's own type reference rather than
  the schema type list, since built-in scalars are not reliably enumerated
  there.

- **Mythic+ dungeons were matched against zone names.** `discover-dungeons`
  matched 0 of 4 configured dungeons against the live API. A Mythic+ season is
  **one zone whose `encounters` are the dungeons**, not one zone per dungeon.
  `match_zones` now searches encounters first, falls back to zone names,
  records which way each match was found, derives the season zone when all
  dungeons agree, and reports a name appearing in two seasons as ambiguous
  instead of resolving it.

- **The expansion list was truncated from the wrong end.** The API returns
  expansions newest first; reporting took the last six entries and so printed
  the six *oldest*, hiding the current expansion and making the season look
  absent from the API. Now sorted by ID descending with nothing dropped.

### Added

- `recon`: a `reports_probe` step that makes real unscoped and zone-scoped
  `ReportData.reports` calls. Introspection showed `guildID` and `userID` are
  optional alongside `zoneID`/`gameZoneID`, so broad sampling may be possible —
  but argument presence is not behaviour, and representative sampling is a
  stated research requirement.
- `recon`: `zone_inventory` in the findings — every zone with its encounter
  names, so a season's dungeon list can be identified without a second run.
- `queries/discover_reports_probe.graphql`.
- Regression tests for all three defects. The simulator now enforces the
  server's sub-selection rule and models a Mythic+ season as one zone with
  dungeon encounters, so these cannot regress silently. 216 → 232 tests.

### Verified against the live API

- Auth, and a token lifetime of **360 days**, not the assumed hour.
- Rate limiting: `limitPerHour` 3600, `pointsSpentThisHour`, `pointsResetIn` —
  all hypothesised names correct. A 17-request recon cost **~2 points**.
- `ReportFight` 23/23 wanted fields, including every Mythic+ field
  (`keystoneLevel`, `keystoneAffixes`, `keystoneTime`, `countReached`,
  `countRequired`, `averageItemLevel`, `npcCountMap`, `gameZone`).
  `rating` is `Float`, not `Int`.
- `ReportDungeonPull` 10/10 and `ReportDungeonPullNPC` 6/6 — the pull-level
  half of the NPC-instance-identity requirement is available.
- `EventDataType`: all 14 hypothesised values exist.
- `Report.gameVersion` does not exist (unused).
- `Report.events` accepts 28 arguments including `sourceInstanceID`,
  `targetInstanceID`, `filterExpression` and the aura-presence filters. None is
  used yet: server-side filtering would discard the raw stream this project
  exists to preserve.
- No credential appeared anywhere in live output, reports or fixtures.

### Still unverified

Event shape, pagination semantics, per-instance identity *in events*, pull
content, event-page cost, and the eight Season 2 dungeon identities. All need
one more recon run.

---

## 0.1.0 — 2026-09-12

First commit. Phase 0 (API reconnaissance) implementation.

- `software_version` 0.1.0
- `schema_version` 0 — no database schema yet, by design
- `normalizer_version` 1
- `query_version` 1

### Added

**Security**
- Secret redaction layer: exact-value registration plus pattern matching for
  bearer tokens, `Authorization` headers and `client_secret` / `access_token`
  fields in JSON and form bodies. Applied to log records (message, args and
  exception text), exception messages, and every cache write.
- Raw cache refuses to write any payload containing a registered credential.
- `wclmplus cache-audit` scans a cache for credentials before a dataset is
  shared.
- `.env` git-ignored; `.env.example` ships placeholders only.

**API layer**
- OAuth2 client-credentials authentication, in-memory token caching, refresh
  margin ahead of expiry, and actionable errors for every failure mode.
- GraphQL client separating transient failures (network, timeout, 5xx, 429)
  from permanently invalid ones (GraphQL validation, 403), so an invalid query
  cannot consume the retry budget or the hourly quota. Partial data returned
  alongside errors is rejected rather than mistaken for a complete answer.
- Rate limiting: defensive field parsing with a candidate-name list, hourly
  budget guard with a configurable reserve, `Retry-After` handling, exponential
  backoff with jitter, and `QuotaExhausted` instead of a long silent sleep.
- Raw cache: versioned identity including the page cursor, atomic gzip writes,
  tolerance of truncated files, per-kind statistics.

**Pagination**
- Event paginator correct whether the cursor is inclusive or exclusive.
  Boundary events are matched as a **multiset**, so a re-sent copy is dropped
  while a genuinely identical second event at the same millisecond is kept.
  Progress is verified per page; non-advancing cursors, backwards cursors and
  all-repeat pages raise instead of looping or silently truncating. Resumable
  checkpoints carry the boundary bookkeeping, without which a resume would
  re-emit boundary events.

**Schema safety**
- Two-stage schema introspection and per-field verification.
- Queries generated from confirmed fields only, so a renamed or removed field
  is reported as an absence instead of crashing a run. Empty selection sets are
  refused.
- Query templates in `queries/*.graphql`, editable without touching Python.

**Reconnaissance**
- `Recon` engine running every Phase 0 probe and writing `RECON_REPORT.md` plus
  `recon_findings.json` — including on failure, since a recorded failure is a
  finding. Empirically measures pagination cursor semantics and query point
  cost, detects pulls containing duplicate NPC species, and records every
  limitation it encounters.

**Discovery, config, CLI**
- `ReportSource` abstraction with a fully working `ManualReportSource`
  (codes, URLs, CSV, comments) and provenance on every candidate. API-backed
  sources refuse to run until recon confirms their schema shape.
- Dungeon registry with **no hardcoded IDs**; `discover-dungeons` resolves names
  against live zones and writes a generated overlay, preserving the authored,
  commented config. Ambiguous names are reported, not guessed.
- Sampling config: key brackets, four event profiles, three sample profiles,
  tracked bias list. Event types validated against the live enum.
- Hotfix epochs with overlap rejection and half-open window assignment.
- Fixture sanitizer: player and uploader names pseudonymized with a stable
  salt; NPC and ability names preserved as research data.
- CLI with 10 commands, 5 usable with no credentials at all.

**Tests**
- 216 tests, none requiring credentials or network. Live smoke tests opt-in via
  `pytest -m live`.
- `tests/wcl_simulator.py`: a clearly-labelled synthetic API stand-in for
  offline control-flow testing, kept separate from `tests/fixtures/` so it can
  never be mistaken for API evidence.

### Known limitations

- **No live API call has been made.** No credentials were available, and the
  build environment's egress proxy denied all connections to
  `warcraftlogs.com`. Every schema statement is a labelled hypothesis; see
  blocker B1 in `PROJECT_STATE.md` and the warning at the top of
  `API_NOTES.md`.
- Four of the eight Midnight Season 2 dungeons are unknown and not guessed.
- No database, no ingestion, no analysis. Phase 1 begins after Gate A.

### Fixed during development

- Redaction filter coerced all log arguments to strings, breaking `%d` / `%f`
  formatting. Only string arguments are rewritten now; a non-string cannot
  carry a secret. Regression tests added.
- Query template rendering substituted placeholders inside `#` comments,
  mangling template documentation and defeating the unfilled-placeholder check.
  Comment lines are now skipped, and template indentation is preserved.
