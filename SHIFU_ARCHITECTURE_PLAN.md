# Shifu Architecture Plan

**Status:** proposal for review. Nothing in this document has been built except
the two items marked **DONE**, which were small enough that the master brief
authorised them as immediate work (§68).

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
║ SQLite (migrations 001-004) · run_stream_coverage           ║
╚══════════════════════════════╪══════════════════════════════╝
                               │  canonical runs only
╔══════════════════════════════╪══════════════════════════════╗
║ LAYER 2 — ANALYSIS            NEW                           ║
║ repository/  query boundary over SQLite                    ║
║ builds/      CombatantInfo → player_build_snapshots         ║
║ actions/     canonical action mapping (paired abilities)    ║
║ archetypes/  pull identity beyond the two signatures        ║
║ state/       State(t) reconstruction, observability-tagged  ║
║ cohorts/     matched comparison sets                        ║
║ projection/  Parquet + DuckDB, derived and rebuildable      ║
╚══════════════════════════════╪══════════════════════════════╝
                               │
╔══════════════════════════════╪══════════════════════════════╗
║ LAYER 3 — INTERFACE           NEW (CLI exists)              ║
║ export/   JSON · JSONL · Markdown · experimental SCL        ║
║ cli.py    renders; owns no analysis                         ║
║ LATER:    MCP · HTTP · hosted Shifu                         ║
╚═════════════════════════════════════════════════════════════╝
```

The one-way arrow matters. Layer 2 reads Layer 1 and never writes to it. Layer 3
reads Layer 2 and never queries SQLite directly. That is what makes the whole
derived stack disposable: delete `data/analytics/` and it rebuilds.

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

### 4.2 Player build snapshots (brief §6)

> **Component** new `builds/`; `normalize.py` unchanged
> **Disposition** extend
> **Schema** `player_build_snapshots`, `run_player_builds`; raw JSON retained
> **Migration** `005_player_builds.sql`
> **Tests** known CombatantInfo fixture → expected snapshot; identical builds dedupe; changed build stays distinct
> **Compatibility** additive; `events.extra` keeps the raw payload
> **Existing corpus** 460 CombatantInfo events across 94 runs, parseable now
> **Backfill** **yes, offline, zero API cost** — the events are already stored

This is the highest-value schema extension and the cheapest. CombatantInfo is
five events per fight, ~36 KB, 2 points — the best value in the API — and it
carries `talents`, `talentTree`, `gear`, `specID`, `auras`, `itemLevel` and every
stat. It is currently collected and then left entirely inside `events.extra` as
JSON: no columns, no build table, no index. "Every Mistweaver running Apex"
means a JSON extraction across millions of rows.

The parser is a derived layer with its own version, so reinterpreting a field
whose meaning was not understood at ingest costs a re-derivation, not a
re-collection. **Hero-talent semantics stay explicitly unknown** until an
external source establishes them (§6 below).

### 4.3 Canonical action mapping (brief §28)

> **Component** new `actions/`; `validate.py::paired_abilities` is the evidence source
> **Disposition** extend; raw events untouched
> **Schema** `canonical_actions`, `action_ability_map`
> **Migration** `006_canonical_actions.sql`
> **Tests** a 4:1 paired ability counts once as an action and four times as events
> **Compatibility** additive; nothing reads it until analysis does
> **Existing corpus** mapping rows are derived; no run changes
> **Backfill** n/a — derived from events already present

The project already detects ability pairs that fire together and are plausibly
one game action. Nothing stops a future analysis double-counting them. The
mapping layer is versioned, records provenance and confidence per mapping,
allows manual override, and **never merges on name alone**. Unmapped abilities
stay separate — the default is "these are two things" and merging requires
evidence.

### 4.4 Pull archetypes (brief §29)

> **Component** new `archetypes/`; `pulls.species_signature` / `composition_signature` preserved
> **Disposition** extend
> **Schema** `pull_archetypes`, `pull_archetype_members`
> **Migration** `007_pull_archetypes.sql`
> **Tests** the two existing signatures keep their exact behaviour; an overpull joins the archetype its species match implies
> **Compatibility** additive
> **Existing corpus** derivable now
> **Backfill** yes, offline

Both signatures exist and are indexed. What is missing is the third level: a
pull a tank chained, overpulled or partly skipped is *semantically the same
pull* with a different signature. **Interpretable features first** — species,
counts, map coordinates, sequence position, neighbours, duration. Clustering is
evaluated only after those are measured, and only if it adds something they
cannot.

### 4.5 Gameplay state reconstruction (brief §30)

> **Component** new `state/`
> **Disposition** extend
> **Schema** `gameplay_states`, `state_actions`, `state_outcomes` (see data model)
> **Migration** `008_gameplay_state.sql`
> **Tests** a hand-authored 20-event timeline reconstructs a known state exactly
> **Compatibility** additive
> **Existing corpus** partially reconstructible — depends per-field on which streams each run collected
> **Backfill** yes for collected streams; **no** for streams never requested

This is the centre of Shifu and the place where honesty about observability
decides whether the whole thing is trustworthy. Every field in a reconstructed
state carries one of four tags:

| Tag | Meaning | Example |
|---|---|---|
| `observed` | present in an event | `amount` on a damage event |
| `derived` | computed from observed values by a documented rule | party HP distribution at *t* from the last HP seen per player |
| `inferred` | a model, with stated assumptions | which enemy a player is attacking |
| `unknown` | not establishable from this corpus | cooldown remaining without ability metadata |

`unknown` must be a first-class value that survives into exports. A state with
six unknowns is useful; a state with six silently-defaulted zeros is poison.

### 4.6 Cohorts (brief §§35-36)

> **Component** new `cohorts/`
> **Disposition** extend
> **Schema** `cohort_definitions` (the filter set + version, not the members)
> **Migration** `009_cohorts.sql`
> **Tests** a cohort excludes runs lacking a required stream and says how many it excluded
> **Compatibility** additive
> **Existing corpus** definable; **not yet populatable at useful N** (see §7)
> **Backfill** yes

Cohort infrastructure is built now; the *definition* of "high-performing" is
deliberately left out of the schema (§36). Storing a performance score as a
column would bake a research judgement into the data format and make it
un-revisable. Cohorts store their filters and their version; the judgement stays
in the research layer where it can be argued with.

### 4.7 Analytical projection: Parquet + DuckDB (brief §7)

> **Component** new `projection/`; SQLite preserved as the source of truth
> **Disposition** extend
> **Schema** none in SQLite; Parquet files under `data/analytics/`
> **Migration** none
> **Tests** deterministic rebuild — same SQLite in, byte-identical partition out; a dropped projection rebuilds
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

### 6.4 Premature

- **MCP, HTTP API, hosting, accounts, payments** — §62 already rules these out.
- **Standardizing `SCL/1`** — §23's gate is not met. Everything stays
  `experimental-<n>` until candidates are measured and reviewed.
- **The Parquet projection** — §4.7. Designed now, built on a measured trigger.
- **Any empirical conclusion about Holy Priest, Murder Row, or any spec** —
  §7 below.

---

## 7. The sample-size problem, stated plainly

This is the most important limitation in the document and it is not a
software problem.

The corpus is **94 runs from 10 reports spanning 1.5 days**, never deduplicated,
so the analysable count is an upper bound. The brief's north star (§64) is
"hundreds of Holy Priest Murder Row runs across low, medium and high keys".

94 runs contain, at most, 94 × 5 = 470 player-slots spread across every spec and
every dungeon collected. One spec in one dungeon in one key bracket is a handful
of observations. **No cohort comparison in this brief is answerable from the
current corpus**, and the architecture cannot fix that — only collection can.

What the measurements say about closing the gap, using the corpus's own numbers
(13.19 points/run measured; 3,600 points/hour documented; ~23 MB/run):

| Corpus | Points | Quota-limited time | Storage |
|---|---|---|---|
| 94 runs (now) | ~1,240 | — | 2.18 GB |
| 500 runs | ~6,600 | ~2 h | ~11 GB |
| 2,000 runs | ~26,400 | ~7 h | ~46 GB |
| 10,000 runs | ~132,000 | ~37 h | ~230 GB |

Quota is not the binding constraint — wall clock is, and the per-run worked
seconds should be read from a fresh `validate` rather than estimated here.
Storage is not a constraint; the operator has confirmed capacity.

**The implication for sequencing:** Phases 0-4 are worth doing now because they
are infrastructure and they are testable against the simulator and the existing
94 runs. Phases 5-7 produce *numbers*, and numbers from 94 runs would be
statistically meaningless while looking authoritative — which is the single
worst outcome this project could produce. Collection should run in parallel with
Phases 1-4, so that when the analytical engine is ready there is something for
it to be right about.

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
| **P1** | 1 | Encode→decode round-trips a real pull exactly · token costs measured with a named tokenizer, never estimated |
| **P2** | 2 | Every analytical read goes through the repository · a coverage-missing stream cannot produce a zero · `packs` unchanged in output |
| **P3** | 3 | A known CombatantInfo fixture produces a known build · identical builds dedupe · hero-talent semantics still `unknown` |
| **P4** | 4 | Projection rebuilds deterministically · deleting it loses nothing · offline tests pass without pyarrow or DuckDB |
| **P5** | 5 | Every statistic carries N, coverage and provenance · a paired ability is not double-counted |
| **P6** | 6 | A hand-authored timeline reconstructs a known state · every field carries observability · `unknown` survives export |
| **P7** | 7 | Every cohort reports what it excluded and why |
| **P8** | 8 | An export is reproducible from its manifest alone |

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
