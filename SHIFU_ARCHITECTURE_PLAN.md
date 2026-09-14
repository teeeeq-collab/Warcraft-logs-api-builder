# Shifu Architecture Plan

**Status:** proposal, **revision 2** — incorporates the architecture review.
Nothing here has been built except the items marked **DONE**, which the master
brief authorised as immediate work (§68).

Per-amendment responses, with the reasoning for each change, are in
[`ARCHITECTURE_REVIEW_RESPONSES.md`](ARCHITECTURE_REVIEW_RESPONSES.md). Six
amendments found real defects; three of those were mine and are corrected here
rather than quietly patched.

**Companion documents:**
[`ANALYTICAL_DATA_MODEL.md`](ANALYTICAL_DATA_MODEL.md) (schemas),
[`SCL_EXPERIMENT_PLAN.md`](SCL_EXPERIMENT_PLAN.md) (compression research),
[`IMPLEMENTATION_PHASES.md`](IMPLEMENTATION_PHASES.md) (ordering and acceptance).
The current-state facts this plan builds on are in
[`CURRENT_STATE_ARCHITECTURE_AUDIT.md`](CURRENT_STATE_ARCHITECTURE_AUDIT.md).

---

## 1. What this plan does and does not propose

The collector is not being replaced. It is being given two layers above it and
nothing taken away from it.

Every proposal below is classified as **preserve**, **extend** or **replace**.
There is exactly one `replace`, and it replaces a query that could never match
anything. Everything else is additive.

```
                        WARCRAFT LOGS v2
                               │
╔══════════════════════════════╪══════════════════════════════╗
║ LAYER 1 — COLLECTION          EXISTS, PRESERVED             ║
║ auth · rawcache · client · ratelimit · paginate · schema    ║
║ normalize · pullassign · collect · dedupe · validate        ║
║                                                             ║
║ STORE: data/db/<corpus>.sqlite      schema_version 4        ║
║ runs · pulls · events · coverage · actors · abilities       ║
║ + combatant_info  (NEW, migration 005 — transcription only) ║
╚══════════════════════════════╪══════════════════════════════╝
                               │  read-only ATTACH
                               │  derivation only, never writes back
╔══════════════════════════════╪══════════════════════════════╗
║ LAYER 2 — ANALYSIS            NEW                           ║
║ repository/  query boundary, coverage- and dedupe-aware     ║
║ builds/      separable build dimensions                     ║
║ actions/     canonical action mapping                       ║
║ archetypes/  pull identity, later pack decomposition        ║
║ state/       State(t), per-field observability + staleness  ║
║ cohorts/     definitions AND immutable evaluations          ║
║                                                             ║
║ STORE: data/analytics/<corpus>.analysis.sqlite              ║
║        analytics_schema_version 1 — DISPOSABLE              ║
║ then:  data/analytics/parquet/  — DISPOSABLE                ║
╚══════════════════════════════╪══════════════════════════════╝
                               │
╔══════════════════════════════╪══════════════════════════════╗
║ LAYER 3 — INTERFACE           NEW (CLI exists)              ║
║ export/   JSON · JSONL · Markdown · experimental SCL        ║
║ cli.py    renders; owns no analysis                         ║
║ LATER:    MCP · HTTP · hosted Shifu                         ║
╚═════════════════════════════════════════════════════════════╝
```

### The boundary is physical, not a convention

Revision 1 asserted that Layer 2 never writes to Layer 1 and then put four
derived tables in the ingest database. Both could not be true. **The analytical
store is now a separate database file** with its own migration directory
(`migrations/analytics/`), its own version constant, and its own lifecycle.
Delete it and you lose compute, nothing else.

The test for which store anything belongs in:

> Could a future version of this code change what an existing row *means*,
> without any new data from the API?

No → transcription → ingest store. Yes → a model → analysis store.

That line runs straight through the build work, which is why it is split:
mapping CombatantInfo's fields into columns is transcription and stays in Layer
1; deciding what constitutes "a build" is a model and moves to Layer 2. Keeping
the whole thing in Layer 1 would mean every build-model revision needed a
migration against the canonical corpus — exactly the coupling the boundary
exists to prevent.

**A constraint worth naming:** SQLite cannot enforce foreign keys across attached
databases, so an analytical `run_id` cannot reference `dungeon_runs`. The
replacement is stronger for this purpose — every analytical row records the
**corpus fingerprint** it was derived from, and a `verify` pass checks integrity
on demand. An FK proves the run exists; a fingerprint proves the run exists *and
has not been recollected since this row was derived*, which is the failure that
would actually corrupt a conclusion.

---

## 2. Change record format

Every proposal in this package uses the same block, because §68 asks for the
same seven facts each time.

> **Component** — the existing file or table affected
> **Disposition** — preserve / extend / replace
> **Schema** — new tables or columns, or "none"
> **Migration** — the numbered file needed, or "none"
> **Tests** — what must be proved before it counts as done
> **Compatibility** — what breaks for code or data written before it
> **Existing corpus** — what happens to the 94 runs already collected
> **Backfill** — whether raw-cache replay can produce it without re-fetching

The last one is the reason this project can afford to be ambitious. Every raw
API response is gzipped on disk and linked from `event_pages.raw_cache_path`,
and normalization is lossless — unpromoted fields go to `events.extra`. So most
schema work is **backfillable offline, at zero API cost**. Where that is not
true it is called out, because those are the only changes that cost quota.

---

## 3. Foundational fixes (brief §§3-5)

### 3.1 Focus-player resolution — **DONE**

> **Component** `collect.py::_resolve_focus_actor`, `sanitize.py`
> **Disposition** replace (the query), extend (`sanitize.py`)
> **Schema** `corpus_identity`; `collection_jobs.identity_scheme_version`; `idx_actors_name`
> **Migration** `004_identity_scheme.sql`
> **Tests** `tests/test_collect.py` (4 new), `tests/test_identity.py` (11 new)
> **Compatibility** none broken; a name that previously matched nothing now matches
> **Existing corpus** unaffected; migration 004 is additive
> **Backfill** n/a — this is a collection-time behaviour

The defect: `normalize_actors` stores players as `pseudonym(name)`, and
resolution compared the typed character name against that. It could not match,
ever. Every `reference_player` collection reported success and collected no
focus stream, leaving only a per-run diagnostic that read like an ordinary
absence.

The fix puts the supplied name through the same function before matching, and
accepts both spellings a person actually has: a character name (with or without
a `-Realm` suffix) and a pseudonym copied from a validation report.

Three properties the brief demanded, now held:

- **absence ≠ failure.** Missing from one run is normal and stays a warning.
  Matching nothing in *any* run is a failed request and ends the job with an
  error diagnostic, an error on the result, and exit code 2.
- **no silent skip.** `FocusResolution` counts runs seen against runs resolved,
  and `collect` reports the ratio.
- **privacy preserved.** The diagnostic records the *pseudonym*, not the typed
  name. Previously it wrote the real character name into `ingest_diagnostics`.

### 3.2 Identity scheme stability — **DONE** (brief §4)

> **Component** `sanitize.py`, `version.py`, `collect.py::start_job`
> **Disposition** extend
> **Schema** `corpus_identity` (one row per database)
> **Migration** `004_identity_scheme.sql`
> **Tests** `tests/test_identity.py`
> **Compatibility** additive; existing databases claim the current scheme on next job
> **Existing corpus** claimed as v1 on the next `collect`; names unchanged
> **Backfill** n/a

Pseudonyms are the only handle the corpus has on a person, so the pseudonym
function is part of the data format. `IDENTITY_SCHEME_VERSION` and a salt
fingerprint now travel in `provenance()` and are recorded in the database. A job
run against a corpus written under a different scheme stops with an error naming
the recovery path, instead of appending names that will never join.

A pinned test asserts `identity_hash("Tankadin") == "a9c90dd21e39"`. If that
test ever fails, the scheme changed and the version must be bumped with it.

### 3.3 Deduplication of the live corpus — **NOT DONE, blocked on the operator**

> **Component** `dedupe.py` (exists and is tested); no code change needed
> **Disposition** preserve
> **Schema** none
> **Migration** none
> **Tests** `tests/test_dedupe.py` already covers it
> **Compatibility** n/a
> **Existing corpus** 94 runs currently classified `is_canonical = NULL` = *never checked*
> **Backfill** n/a — it is a local pass over rows already present

`wclmplus dedupe` has never been run against the live corpus. Until it is, every
`N` in every statistic is an upper bound: two uploads of one real run count
twice. `validate`'s `dedupe_coverage` reports this as `never_run` rather than
letting "0 duplicate groups" pass for "no duplicates".

**Every downstream analytical query must default to canonical runs.** The
repository layer enforces this (§4.1) rather than trusting each caller. Duplicate
rows are never deleted — they are evidence about upload behaviour.

---

## 4. The analysis layer

Full schemas are in `ANALYTICAL_DATA_MODEL.md`. This section states what each
piece is for and what it costs.

### 4.1 Repository boundary (brief §25) — the first thing to build

> **Component** new `src/wcl_mplus/repository/`; `cli.py::packs` loses its SQL
> **Disposition** extend (new), replace (the SQL inside `packs`)
> **Schema** none
> **Migration** none
> **Tests** every method, against the simulator database; plus a coverage-refusal test
> **Compatibility** `packs` output unchanged — it is the same query, relocated
> **Existing corpus** none
> **Backfill** n/a

This is first because everything else in Layer 2 is a client of it, and because
the audit found SQL spread across `validate.py`, `dedupe.py`, `collect.py` and
`cli.py`. Adding six analytical modules on top of that pattern would make the
corpus's access rules unenforceable.

Two non-negotiable properties, which are the whole reason it exists rather than
being a stylistic preference:

1. **Canonical by default.** Every run-scoped query filters
   `is_canonical = 1 OR is_canonical IS NULL` unless asked for
   `include_duplicates=True`. Getting this wrong inflates every count.
2. **Coverage-checked by construction.** A query that reads a stream takes the
   stream it needs as an argument and consults `run_stream_coverage` first. Runs
   that never requested it are **excluded and counted**, not silently treated as
   zero. Returning a number without saying which runs could have contributed is
   the failure mode this project has spent the most effort avoiding, and it is
   far too easy to reintroduce by hand.

Streaming matters: `get_pull_events` on a large corpus must yield batches, never
materialise millions of rows. SQLite cursors do this natively.

### 4.2 CombatantInfo: normalization, then build dimensions (brief §6)

> **Component** `normalize.py` (extend), new `builds/` in Layer 2
> **Disposition** extend
> **Schema** `combatant_info` (ingest, migration 005); five build dimension tables + `run_player_config` (analysis)
> **Migration** ingest 005; analytics 001
> **Tests** known fixture → known columns; identical talents dedupe regardless of gear; a respec stays distinct
> **Compatibility** additive; `events.extra` keeps the raw payload
> **Existing corpus** 460 CombatantInfo events across 94 runs, parseable now
> **Backfill** **yes, offline, zero API cost**

The highest-value schema extension and the cheapest: CombatantInfo is five events
per fight, ~36 KB, 2 points — the best value in the API — carrying `talents`,
`talentTree`, `gear`, `specID`, `auras`, `itemLevel` and every stat. It is
currently collected and left entirely inside `events.extra` as JSON.

**Revision 2 splits it in two, and splits the build itself into dimensions.**

Revision 1 hashed talents and gear into one `build_id`. That made **item level a
talent variable**: two Mistweavers with identical talents and hero talents at
ilvl 681 and 684 became different builds. Over a few hundred runs, a cohort query
for "Apex Mistweavers" would have returned a scatter of one-member builds and
reported `N=1` for a configuration dozens of players ran. Not a performance
problem — a confident statistic with the wrong denominator.

Five independent dimensions now, plus an optional composite: talent loadout,
hero talent, equipment snapshot, trinket configuration, stat snapshot. Each
question becomes one join on one dimension — Apex vs no-Apex on `hero_talent_id`,
same talents regardless of gear on `talent_hash` alone. Item level is a column,
never part of the equipment hash. Trinkets are an unordered pair, because which
one occupies slot 13 is arbitrary.

**Hero talents stay `unknown`** until external metadata establishes them, and a
cohort filtered on one refuses to run while that is true rather than returning
whichever rows happen to be populated.

**A limitation stated rather than implied:** CombatantInfo is a snapshot at fight
start, so gear swapped mid-dungeon is invisible. Every equipment and stat row
means "as at the start of the run".

### 4.3 Canonical action mapping (brief §28)

> **Component** new `actions/` in Layer 2; `validate.py::paired_abilities` is the evidence source
> **Disposition** extend; raw events untouched
> **Schema** `canonical_actions`, `action_ability_map` (analysis, migration 002)
> **Migration** analytics 002 — **no ingest migration**
> **Tests** a 4:1 paired ability counts once as an action and four times as events
> **Compatibility** additive
> **Existing corpus** derived; no run changes
> **Backfill** yes, offline

The project already detects ability pairs that fire together. Nothing stops a
future analysis double-counting them. The mapping is versioned, records
provenance and confidence, allows manual override, and **never merges on name
alone**. Default is separate; merging is a claim carrying its evidence.

### 4.4 Pull archetypes, and later pack decomposition (brief §29)

> **Component** new `archetypes/` in Layer 2; the two existing signatures preserved
> **Disposition** extend
> **Schema** `pull_archetypes`, `pull_archetype_members` (analysis, migration 003)
> **Migration** analytics 003
> **Tests** the two existing signatures keep exact behaviour; an overpull joins the archetype its species imply
> **Compatibility** additive
> **Existing corpus** derivable now
> **Backfill** yes, offline

`composition_signature` (exact) and `species_signature` (species-equivalent) exist
and are indexed. The new work is the third level: a pull a tank chained,
overpulled or partly skipped is semantically the same pull with a different
signature. **Interpretable features first** — species overlap, count difference,
map coordinates, sequence position, neighbours, duration. Clustering is evaluated
only afterwards, and only if it beats them.

**Pack decomposition** is the eventual target: `Pack A + Pack B (+ stray C)`
rather than "a fuzzy variant of archetype X". It is what players actually ask
about when they ask whether two packs can be combined. It is a latent-structure
problem — observed pulls are unions of unobserved packs — and recovering the
parts needs the *combinations* to vary across many routes and groups, which makes
it the most sample-hungry item in this package. The archetype table carries a
`components` column from the start so it never needs a migration, and it is
scheduled for nothing.

### 4.5 Gameplay state reconstruction (brief §30)

> **Component** new `state/` in Layer 2
> **Disposition** extend
> **Schema** `gameplay_states`, `state_actions`, `state_outcomes` (analysis, migration 004)
> **Migration** analytics 004
> **Tests** a hand-authored 20-event timeline reconstructs a known state exactly; a stale field degrades on its horizon; a truncated horizon is marked
> **Compatibility** additive
> **Existing corpus** partially reconstructible; bounded per-field by stream coverage
> **Backfill** yes for collected streams; **no** for streams never requested

The centre of Shifu, and where honesty about observability decides whether any of
it is trustworthy.

**Revision 2 replaces the four-way tag with a per-field record.** Four tags were
necessary and insufficient: HP last seen 40 ms ago and HP last seen 8 s ago were
both "observed", and only one is worth anything. Each field now carries value,
status, `observed_at_ms`, `age_ms`, method, model version and confidence.

| Status | Meaning |
|---|---|
| `observed` | read from an event, within its staleness horizon |
| `stale` | read from an event, past its horizon; `age_ms` says how far |
| `derived` | computed from observed values by a documented rule |
| `inferred` | a model; `method` names it, `confidence` where probabilistic |
| `unknown` | not establishable from this corpus |

Horizons live in configuration, per field kind, and degradation to `stale` is
automatic — recording an age is not enough, because a consumer handed
`age_ms: 8300` can ignore it and eventually one will. `null` horizons are a real
category: an aura's state is known exactly between its apply and remove events,
and calling that stale after two seconds would be wrong in the other direction.

**Outcomes support multiple horizons.** `state_id` alone as a primary key forced
one window to be chosen before anyone knew which window answered the question;
"did the player survive" differs at +1 s and +10 s. The key is now
`(state_id, horizon_id)`, with fixed and semantic windows, and a horizon running
past the end of available data is marked **truncated** rather than silently
shortened. Every aggregate reports its truncation count — without it, "deaths
within 10 s" is understated for every state near a pull boundary, and the bias
points the same way every time.

### 4.6 Cohorts, evaluations, and evidence depth (brief §§35-36)

> **Component** new `cohorts/` in Layer 2
> **Disposition** extend
> **Schema** `cohort_definitions`, `cohort_evaluations`, `cohort_evaluation_members` (analysis, migration 005)
> **Migration** analytics 005
> **Tests** a cohort excludes runs lacking a required stream and says how many; an evaluation reproduces from its manifest; a concentrated distribution reports its concentration
> **Compatibility** additive
> **Existing corpus** definable; populatable at useful N only for some claim classes (§7)
> **Backfill** yes

**Definitions and evaluations are different objects.** A definition answers *who
matches now*; an evaluation answers *exactly who produced this published number*.
Only the second is reproducible, and revision 1 had only the first. An evaluation
records the definition version, corpus fingerprint, model versions, timestamp,
exclusions with reasons, and the **source set** — runs and players, never states,
which are re-derivable from it and would otherwise produce a member list larger
than the analysis it documents.

**Every statistic reports evidence depth at every level**, never a single `N`:
events, states, pulls, runs, players, reports, parties — plus which unit is
independent for that claim class, and a **concentration** measure. A distribution
where one player supplies 60% of observations is not a population distribution
however large the event count, and printing `N=100,000` beside it would be a lie
of composition rather than of arithmetic.

No performance score exists anywhere in the schema. §36 is explicit: the
definition of "high-performing" is a research judgement, and a column would make
it unrevisable and invisible.

### 4.6a Dedupe policy

> **Component** `repository/`
> **Disposition** extend
> **Schema** none

| Policy | `is_canonical IS NULL` | Use |
|---|---|---|
| `permissive` | allowed, reported | browsing, retrieval, debugging |
| `strict` | **refused** — result marked `provisional`, export blocked | statistics, publication |

**No default at the research boundary.** Every analytical entry point takes the
policy as a required argument. Defaulting to `permissive` silently gives a
statistics path the lax rule; defaulting to `strict` fails interactive queries for
no reason. Making the caller state which kind of question they are asking is
cheap, and guessing is how an undeduplicated corpus ends up underneath a
published number — which is the live corpus's current state.

### 4.7 Analytical projection: Parquet + DuckDB (brief §7)

> **Component** new `projection/`; SQLite preserved as the source of truth
> **Disposition** extend
> **Schema** none in SQLite; Parquet files under `data/analytics/`
> **Migration** none
> **Tests** deterministic rebuild — same source in, identical **logical fingerprint** out; a dropped projection rebuilds
> **Compatibility** additive; nothing depends on it existing
> **Existing corpus** projectable
> **Backfill** yes — it is by definition derived

**Recommendation: design it now, build it in Phase 4, do not build it yet.**

At 4.97 M events and 2.18 GB, SQLite with the existing indexes is not the
bottleneck, and a projection built against a corpus this small would be tuned
for the wrong shape. The brief's own target — hundreds of GB, eventually >1 TB —
is where columnar scans win, and the crossover is measurable rather than
guessable.

**Trigger to build it:** when a representative analytical query (a
mechanic distribution across every collected run of one dungeon) exceeds ~30
seconds on SQLite, or the corpus passes ~50 M events. Whichever comes first.
Record the measurement; do not build on the estimate.

Rules that hold whenever it is built: derived from canonical ingest data only;
rebuildable from scratch; versioned; no information exists *only* there; SQLite
and the raw cache remain the authoritative provenance. `pyarrow` is already an
optional extra; DuckDB would be a second one. Neither may become a hard
dependency of collection, and the offline test suite must keep passing without
either installed.

**Determinism is semantic, not byte-level.** Revision 1 required byte-identical
rebuilds, which was brittle and would have caused real damage: Parquet embeds
writer version, codec settings, row-group boundaries and dictionary page layout,
so a routine `pyarrow` bump would fail a correctness gate with no row changed —
and a gate that fails for non-reasons is a gate people learn to ignore.

Required instead: identical logical rows, identical ordering under a declared
total order, identical partitioning, identical source manifest, and an identical
**logical fingerprint** — a hash over canonically serialised, sorted,
explicitly-typed rows, computed without reference to the file format. The
fingerprint is what the test asserts, and it is stable across writer versions
because it never sees the writer. Byte-identity under a pinned environment stays
a warning: worth noticing, not worth failing.

---

## 5. Interface layer

### 5.1 `export-context` (brief §42)

> **Component** new `export/`; new CLI command
> **Disposition** extend
> **Schema** none
> **Migration** none
> **Tests** a manifest lists every source run; a scope with insufficient N says so rather than exporting
> **Compatibility** additive
> **Existing corpus** exportable
> **Backfill** n/a

The first Shifu interface is a directory of files the user uploads to ChatGPT by
hand. No API client, no hosting, no cost. It validates whether the whole idea is
useful before anything is built to serve it.

```
export/<scope-hash>/
  manifest.json      scope, filters, versions, coverage, run IDs, exclusions
  summary.json       cohort size, key range, dungeons, N per claim
  distributions.json mechanic timings and damage distributions with N
  examples.jsonl     one state/action/outcome per line, with provenance
  timeline.scl       experimental compact timeline + its legend
  report.md          methodology, limitations, findings, legend
```

`manifest.json` is not optional decoration. It is what makes a number in
`summary.json` checkable: which runs, which streams, which versions, and which
runs were *excluded* for lacking coverage.

### 5.2 CLI

> **Component** `cli.py`
> **Disposition** preserve; remove analysis from it
> **Schema** none

`cli.py` renders. It owns no analysis. `packs` is the existing violation and
moves to the repository in Phase 2; its output does not change.

---

## 6. Explicit flags (brief §68)

The brief requires that parts of it which cannot be built, or should not be
built yet, are named rather than quietly dropped.

### 6.1 Cannot be established from Warcraft Logs data

| Brief asks for | Why not | What is possible instead |
|---|---|---|
| Continuous player position / movement paths (§30, §54) | Coordinates ride on events, irregularly. Between two events a player's position is unknown. | Timestamped position observations, tagged `observed`, with explicit gaps. Never a path. |
| Enemy health at arbitrary *t* (§30) | Only present on events involving that enemy. | Last-observed HP with its timestamp and staleness. |
| A player's current target (§30) | No target field for "who am I facing". | `inferred` from their own damage/heal events; states its rule and its ambiguity. |
| Cooldown availability (§§30-31) | The API reports no cooldown timers. | `unknown` until ability metadata exists; then `probable` from casts, charges and known resets. Never from a tooltip in memory. |
| "What a good player *would* have done" (§64) | Counterfactual. Nothing in a log records unchosen options. | What matched players *did* in similar observed states, with N. The counterfactual belongs to the LLM plus expert knowledge. |
| Avoidable-damage exposure (§34) | Requires knowing which abilities were avoidable — a game-knowledge claim. | Damage taken per ability with N; avoidability stays an external label. |
| Enemy threat (§30) | **Measured:** the `Threat` stream carries no threat value. | Nothing. Do not model threat. |
| Enemy resources (§53) | **Measured:** `Resources@Enemies` returns zero rows. | Nothing; removed from `sampling.yml` already. |
| Player intent, GCD pressure, latency, reaction time | Not in the data. | Nothing. Do not infer them. |

### 6.2 Requires external game metadata (not in scope here)

Tooltip cooldowns and charges · talent-tree structure and node semantics ·
hero-talent identity · spell categories (defensive / interrupt / dispel /
cooldown) · trinket and item effects · mechanic avoidability · spec role beyond
what WCL itself reports.

Every one of these is needed for the *teaching* half of Shifu and none of them
can come from the corpus. §40's guide/knowledge system is the right home, kept
separate from the combat database, with source, author, URL, date and patch
recorded per claim. **Until it exists, the affected fields are `unknown`, not
guessed.** Hardcoding any of this from memory is the one thing the project has
consistently refused to do and this plan does not start.

### 6.3 Technically possible but probably not worth it

- **Candidate E (bare integer rows) as an LLM format** (§18). Keep it as a
  storage lower bound; it is what Parquet already does better.
- **Repeat opcodes and template encoding** (§16). Saving two tokens by making
  the model hold hidden state is a bad trade. Benchmark it, expect to reject it.
- **Hierarchical ability category codes** (§11). The brief already warns not to
  assume they tokenize better. They may well tokenize *worse* than short names.
- **ML clustering of pull archetypes** (§29) before interpretable features are
  measured. Same for embeddings in similarity search (§37).

### 6.4 Reserved, no implementation now

**Export-scoped pseudonyms** (review amendment 16). Internal pseudonyms are
stable by design so longitudinal analysis works, and that same stability makes
them unsafe to publish: two snapshots sharing an ID are trivially correlated.

An export pseudonym **cannot** derive from `IDENTITY_SALT` — that salt is public
in this repository, so anyone could recompute the mapping. It must be
`HMAC(random_per_export_salt, internal_pseudonym)`, with the salt generated at
export time and **not included in the export**. Whether the operator retains it
privately is a real choice with a real consequence: keep it and you can
re-identify your own snapshot later; discard it and the mapping is gone for
everyone, including you. The manifest records which was done, never the salt.

### 6.5 Premature

- **MCP, HTTP API, hosting, accounts, payments** — §62 already rules these out.
- **Standardizing `SCL/1`** — §23's gate is not met. Everything stays
  `experimental-<n>` until candidates are measured and reviewed.
- **The Parquet projection** — §4.7. Designed now, built on a measured trigger.
- **Any empirical conclusion about Holy Priest, Murder Row, or any spec** —
  §7 below.

---

## 7. Evidence depth, and a retraction

Revision 1 said numbers from the current 94-run corpus would be "statistically
meaningless". **That was wrong, and wrong in a way that would have hidden real
evidence.** Evidence quality depends on the claim and on which unit is
independent for it — not on a single corpus-wide threshold.

Some claims are well supported by the corpus that exists today:

| Claim | Independent unit | Current corpus |
|---|---|---|
| "This NPC casts ability X" | one observation | **supported now** |
| "Its recast interval is 2.97-4.93 s" | NPC instance | **supported now** — 12 independent copies |
| "This pack appears at this route position" | pull | **likely supported** |
| "This mechanic targets non-tanks ~30% of the time" | cast | supported for common abilities |
| "Holy Priests press Apotheosis in this window" | **player** | not supported — too few distinct players |
| "Apex outperforms non-Apex" | player, matched | not supported |

The pattern: **enemy-behaviour claims are cheap and player-behaviour claims are
expensive.** The independent unit for the first is an NPC instance — hundreds per
run. For the second it is a person, of whom there are at most five per run, often
the same people across a report. 94 runs is at most 470 player-slots spread over
every spec and dungeon collected, and the effective number of distinct players is
smaller still.

So there is **no global minimum run count** anywhere in this design. Each claim
declares its independent unit, reports the count of that unit, and reports
concentration — the largest share contributed by one player and by one run — so
that a distribution dominated by one person is visible as such.

### What it costs to close the gap

Using the corpus's own measurements (13.19 points/run; 3,600 points/hour
documented; ~23 MB/run):

| Corpus | Points | Quota-limited time | Storage |
|---|---|---|---|
| 94 runs (now) | ~1,240 | — | 2.18 GB |
| 500 runs | ~6,600 | ~2 h | ~11 GB |
| 2,000 runs | ~26,400 | ~7 h | ~46 GB |
| 10,000 runs | ~132,000 | ~37 h | ~230 GB |

Quota is not the binding constraint — wall clock is, and the per-run worked
seconds should be read from a fresh `validate` rather than estimated here.
Storage is not a constraint; the operator has confirmed capacity.

**Implication for sequencing.** Phases 0-4 are infrastructure and are testable
today against the simulator and the existing corpus. Phase 5 onward produces
numbers, and which of those numbers are trustworthy is decided per claim by the
evidence block — not by waiting for a threshold. Collection should run in
parallel throughout, because player-behaviour claims are the ones that need it
and they are the ones the product is built on.

---

## 7a. `reference_player` contradicts its own purpose

> **Component** `config/sampling.yml`
> **Disposition** replace (the profile's stream list)
> **Schema** none — `run_stream_coverage` already keys on `(run_id, data_type, hostility, source_id, target_id)`
> **Migration** none
> **Tests** the benchmark's measurements, then profile tests
> **Existing corpus** unaffected; it was collected under `mechanics`
> **Backfill** n/a

The profile as configured **is not a focus-player profile.** It collects
`DamageDone` and `Healing` party-wide *and* defines focus streams, so the focus
narrowing saves nothing on the two most expensive streams in the API.
`DamageDone` alone is 89,703 events/run — 1.65× the entire current corpus, per
run.

The reasoning behind that choice (decision D13: collecting one player of five
means any question about the other four needs the run re-fetched) is sound, but
it is the reasoning for a *different profile*. Applied inside `reference_player`
it produced `mechanics_research` plus everything, under a name promising the
opposite. The reasoning splits into two profiles instead of contradicting itself
inside one:

- `mechanics_research` — party-wide, ask-anything-later, expensive per run.
- `reference_player` — rich environment and party context, maximal telemetry for
  **one** player. Party-wide `DamageDone`/`Healing` leave it.

**Measurement before the profile is finalised.** The benchmark must answer,
against the live API:

| Question | Why it is not answerable from here |
|---|---|
| Does `sourceID` narrowing reduce *points*, or only rows? | Cost is per page; a narrowed stream may cost the same if the server filters after paging |
| Does the API accept `sourceID` and `targetID` together? | Unverified — the template carries both, no live request has used both |
| What does `Buffs` + `sourceID` return? | **Buffs the focus player applied** — not buffs *on* them, which need `targetID`. Different data; the profile currently assumes one word covers both |
| What does `Healing` + `targetID` return? | Healing *received* — needed to tell "this player was being kept alive" from "this player was fine" |
| Is `DamageTaken` + `targetID` equivalent to `DamageDone` + `targetID`? | Unverified; different `EventDataType` values that may overlap |

That last group is why `focus_event_types` cannot remain a flat list: a focus
stream must say *which* narrowing it wants, and a healer's useful focus set is
not a DPS's. **Role-specific focus sets** are proposed, gated on the benchmark.

Bookkeeping cost: zero. The coverage manifest's unique index already
distinguishes a source-narrowed stream from a target-narrowed one. It was built
for this.

---

## 8. Decisions that need the operator

These are the points where the plan branches and the choice is not technical.

1. **Which spec and dungeon does the reference corpus target first?** Depth in
   one spec/dungeon/bracket beats breadth for every question in §1 of the brief.
   A corpus spread evenly over ten dungeons answers nothing about any of them.
2. **Does the Parquet projection get built on the §4.7 trigger, or earlier?**
   Earlier costs work now and risks tuning for the wrong shape.
3. **`hotfix_epochs.yml` is still empty.** Real Blizzard patch dates are needed
   and will not be invented. Without them every run sits in one undifferentiated
   epoch, and a mechanic that was changed mid-season reads as one noisy
   distribution instead of two clean ones. This is a five-minute data-entry task
   with a large effect on correctness.
4. **Two `overlapping_pulls` warnings** in the current corpus are unexplained.
   Cheap to investigate; they bear on whether pull-local timings are sound.

---

## 9. Acceptance gates

No phase is complete until its gate closes. Gates are properties, not opinions.

| Gate | Phase | Closes when |
|---|---|---|
| **P0** | 0 | Focus resolution proven by test · identity scheme pinned · `dedupe` run on the live corpus · fresh `validate` · stale docs corrected |
| **P1** | 1 | Track A encode→decode round-trips a real pull exactly · a retrieved chunk decodes standalone · token costs measured with a named tokenizer, never estimated · Track B evaluated on its own metrics |
| **P2** | 2 | Every analytical read goes through the repository · a coverage-missing stream cannot produce a zero · dedupe policy is a required argument · `packs` unchanged in output |
| **P3** | 3 | A known CombatantInfo fixture produces known columns · identical talents dedupe regardless of gear · hero-talent status still `unknown` |
| **P4** | 4 | Projection rebuilds to an identical logical fingerprint · deleting it loses nothing · offline tests pass without pyarrow or DuckDB |
| **P5** | 5 | Every statistic carries its evidence block and concentration · a paired ability is not double-counted · no statistic from runs lacking its stream |
| **P6** | 6 | A hand-authored timeline reconstructs a known state · every field carries status and age · a stale field degrades on its horizon · a truncated outcome horizon is marked |
| **P7** | 7 | Every cohort reports what it excluded and why · an evaluation reproduces from its manifest alone |
| **P8** | 8 | An export is reproducible from its manifest alone |
| **PB** | any | The analytical store can be deleted and rebuilt with no change to the ingest database |

---

## 10. The principle underneath all of it

The collector's discipline is the reason this can be built on top of it rather
than beside it: raw responses are kept, normalization loses nothing, coverage
records what was *asked for* and not merely what came back, and unknowns stay
unknown.

Everything above is derived. All of it can be deleted and rebuilt. None of it
may ever become the only place a fact lives.

> The analytical engine does the counting. The LLM does the explaining.
> Neither invents.
