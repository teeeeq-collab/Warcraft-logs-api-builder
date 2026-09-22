# Implementation Phases

Ordering, acceptance and effort for the work proposed in
[`SHIFU_ARCHITECTURE_PLAN.md`](SHIFU_ARCHITECTURE_PLAN.md),
[`ANALYTICAL_DATA_MODEL.md`](ANALYTICAL_DATA_MODEL.md) and
[`SCL_EXPERIMENT_PLAN.md`](SCL_EXPERIMENT_PLAN.md).

**Revision 2** — incorporates the architecture review; per-amendment reasoning is
in [`ARCHITECTURE_REVIEW_RESPONSES.md`](ARCHITECTURE_REVIEW_RESPONSES.md).

The package stops here and waits for review again before large implementation
begins. Phase 0 items marked **DONE** were authorised as small critical fixes.

---

## Phase 0 — repo stabilization

*Everything here is either done, or a command the operator runs.*

| # | Task | State |
|---|---|---|
| 0.1 | Read the current-state audit | **DONE** — `CURRENT_STATE_ARCHITECTURE_AUDIT.md` |
| 0.2 | Fix `--focus-player` resolution | **DONE** — commit `bd753bb`, 15 new tests |
| 0.3 | Version the identity scheme | **DONE** — migration 004, `corpus_identity` |
| 0.4 | Run `dedupe` on the live corpus | **operator** — never run; `analysable: 94` is an upper bound |
| 0.5 | Regenerate `validate` | **operator** — after 0.4, with the corrected worked-seconds metric |
| 0.6 | Correct stale docs | **DONE** — see below |
| 0.7 | Fill `config/hotfix_epochs.yml` | **blocked** — needs real Blizzard patch dates; will not be invented |
| 0.8 | Investigate 2 `overlapping_pulls` warnings | **DONE (code)** — classified as touching/nested/partial; `validate` now reports which, and how many events each puts in doubt. The live corpus's two are answered by the next validation run |

**Doc corrections made in 0.6.** `API_NOTES.md` concluded "quota, not wall clock,
is the binding constraint" from a ~37 points/run estimate. The measured cost is
**13.19 points/run**, which reverses the conclusion: wall clock binds, not quota.
The estimate is kept next to the measurement rather than deleted, because the
reason the estimate was wrong is itself useful.

**Gate P0:** focus resolution proven by test · identity pinned · dedupe run ·
fresh validation report · no doc asserting a number the corpus contradicts.

---

## Phases 1 and 2 — run in parallel

They share no files and no schema. P1 reads the corpus and writes only to
`data/exports/scl/`; P2 builds a read boundary over it. Collection runs
alongside both.

### Phase 1 — compression experiments

1.1 Select and freeze 20-50 fixture pulls covering the §20 case list, **grouped
by stream coverage** so format comparisons are not secretly coverage
comparisons.
1.2 Measure redundancy: `P(enemy melee target = tank)` by dungeon, species, pull
and bracket; ability vocabulary per spec and per species; event-type
distribution. These feed the suppression gate; they are not themselves the gate.
1.3 Build **three dictionary scopes** — dungeon/spec, run, pull — and test
`P1-P5` against `T/H/D1/D2/D3`.
1.4 Implement Track A candidates (A-E, G) with encoders **and** decoders, and
Track B (F).
1.5 Test legend placement: once, every N, block-local, chunk-local. **A retrieved
chunk must decode standalone** — this is a retrieval requirement, not a size
preference, and it excludes repeat opcodes that cross chunk boundaries.
1.6 Measure size and tokens with a named tokenizer; label proxy counts as proxy.
1.7 Comprehension fixtures with expected answers computed from the database,
tested at 10, 100 and 1,000 events.
1.8 Write `SCL_BENCHMARK.md` + `scl_benchmark.json` — **two tables, one per
track**.

**Do not standardize.** Output is two tables and a recommendation.

**Gate P1:** every Track A candidate round-trips a real pull exactly (or
documents its loss exactly) · a retrieved chunk decodes standalone · no token
count is an estimate · Track B judged on its own metrics, not tokens per event.

### Phase 2 — query / repository layer

Everything in Layer 2 is a client of this, and the audit found SQL spread across
four modules. Adding six analytical modules onto that pattern would make the
corpus's access rules unenforceable.

2.1 `repository/` over the **ingest** store: runs, pulls, events, players,
CombatantInfo, packs.
2.2 **Canonical-by-default** filtering, with an explicit `include_duplicates`.
2.3 **Dedupe policy as a required argument** on every analytical entry point —
no default, so neither laxity nor strictness is acquired by accident.
2.4 **Coverage-checked reads**: a stream query names the stream it needs,
excludes runs that never requested it, and returns the exclusion count.
2.5 Streaming/batched iteration — never materialise millions of rows.
2.6 The **evidence block** (events/states/pulls/runs/players/reports/parties +
independent unit + concentration) produced by the repository, so no analytical
module has to remember to compute it.
2.7 Analytical store scaffolding: `data/analytics/<corpus>.analysis.sqlite`, its
own migration runner and version constant, read-only ATTACH to the ingest store,
**content-derived corpus fingerprinting** (`page` and `deep` grades), and
`analytics verify`.
2.8 Move `packs` SQL out of `cli.py`; output stays identical.

**Gate P2:** no analytical read touches SQLite directly · a missing stream cannot
produce a zero · **an analytical entry point without an explicit dedupe policy
raises** · the analytical store can be deleted and rebuilt with no change to the
ingest database · `packs` byte-identical before and after · **changing any
page's cached content, count, status or cursor changes the fingerprint; a `deep`
fingerprint additionally changes when a normalized event row changes.**

---

## Phase 3 — CombatantInfo normalization and build dimensions

Cheapest high-value work in the package: the data is already collected, the
backfill is offline, and it unblocks every cohort question.

3.1 **Ingest migration 005** — `combatant_info`, transcription only: columns
mapped directly from the payload, raw JSON retained, no identity decisions.
3.2 Backfill the 460 existing CombatantInfo events — **no API calls**.
3.3 **Analytics migration 001** — the five separable dimensions (talent loadout,
hero talent, equipment snapshot, trinket config, stat snapshot) plus
`run_player_config`.
3.4 Item level is a **column**, never part of the equipment hash. Trinkets are an
**unordered** pair.
3.5 `hero_talent.status` stays `unknown`; a cohort filtered on it **refuses to
run** rather than returning the populated subset.
3.6 Document that CombatantInfo is a fight-start snapshot, so gear swapped
mid-run is invisible and every equipment row means "as at the start".

**Gate P3:** known fixture → known columns · identical talents dedupe regardless
of gear · a respec stays two loadouts · raw payload retained · hero-talent status
still `unknown`.

---

## Phase 4 — analytical projection

**Build on the measured trigger, not on schedule:** a representative cross-run
query exceeding ~30 s on SQLite, or ~50 M events. Neither holds today at 4.97 M.

4.1 Parquet writer with the dungeon/epoch/stream partition layout.
4.2 DuckDB query surface behind the same repository interface.
4.3 Incremental projection, plus a full rebuild.
4.4 Dictionary tables for actors, NPCs, abilities, items, talents, archetypes.
4.5 **Logical fingerprint** — a hash over canonically serialised, sorted, typed
rows, computed without reference to the file format.

**Gate P4:** same source in, identical logical fingerprint out · deleting
`data/analytics/parquet/` loses nothing · the offline suite passes with neither
pyarrow nor DuckDB installed.

Byte-identity is **not** required. Parquet embeds writer version, codec settings
and row-group boundaries, so pinning bytes would pin `pyarrow` forever and fail
the gate on a dependency bump with no row changed. Byte-identity under a pinned
environment stays a warning.

---

## Phase 5 — first empirical analysis

**Not blocked wholesale on corpus size.** Which claims are supportable is decided
per claim by the evidence block, not by a global threshold. Enemy-behaviour
claims — recast intervals, target distributions, pack occurrence — have NPC
instances and casts as their independent unit and are supportable now.
Player-behaviour claims have *people* as their unit and are not.

5.1 Mechanic model: first-cast timing, recast intervals and distributions, target
distribution, per-pull and per-instance counts, damage distributions, interrupt
and dispel relationships.
5.2 Canonical action mapping (**analytics migration 002**) from
`paired_abilities` evidence. Default separate; merging carries its evidence.
5.3 Pull archetypes (**analytics migration 003**), interpretable features first.
The `components` column is present from the start so pack decomposition never
needs a migration.
5.4 Every output carries the **evidence block** — all seven counts, the declared
independent unit, and concentration — plus coverage, provenance, hotfix epoch and
key bracket.

**Gate P5:** no statistic without its evidence block · a 4:1 paired ability is not
counted as four actions · no statistic from runs that never collected the stream
it needs · a distribution dominated by one player says so.

---

## Phase 6 — gameplay state engine

6.1 State schema (**analytics migration 004**). `payload` is the authoritative
field→value mapping; `field_meta` carries metadata only under the same keys —
status, `observed_at_ms`, `age_ms`, method, model version, confidence — and
never repeats the value.
6.2 Staleness horizons in `config/state_horizons.yml`; degradation to `stale` is
automatic. `null` horizons are a real category, not a missing value.
6.3 `block_meta` for run-constant fields, so 30 fields do not carry 200 metadata
keys per state.
6.4 Reconstruction from events, per player, per moment of interest.
6.5 Action extraction; outcomes keyed `(state_id, horizon_id)` across fixed and
semantic windows, with `truncated` and `actual_window_ms`.
6.6 Cooldown availability stays `unknown`; `inferred` with a stated method once
casts and charges support it; `derived` only with external metadata.

**Gate P6:** a hand-authored timeline reconstructs a known state exactly · every
field carries status and age · a field past its horizon degrades to `stale` · a
horizon running past the data is marked truncated and counted in aggregates ·
`unknown` survives into every export · **`field_meta` carries no `value` key, and
a key present in `field_meta` but absent from `payload` is a verify failure.**

---

## Phase 7 — cohorts, sequences, similarity

7.1 Cohort definitions **and immutable evaluations** (**analytics migration
005**). A definition answers "who matches now"; an evaluation answers "who
produced this published number".
7.2 Evaluations store the **source set** — runs and players, never states, which
are re-derivable and would otherwise exceed the analysis in size.
7.3 `required_streams` is mandatory with no default; exclusions are recorded with
reasons and counted.
7.4 Action sequences with their initial state and outcome.
7.5 Interpretable similarity features with configurable weights. Nearest-neighbour
and clustering evaluated only after exact and feature matching are measured;
embeddings only if they beat them.
7.6 Representative example retrieval with run/pull/time provenance.

**Gate P7:** every cohort reports what it excluded and why · an evaluation
reproduces from its manifest alone · no "user vs top parse" comparison exists
anywhere in the code.

---

## Phase 8 — LLM export interface

8.1 `export-context` writing the six-file bundle.
8.2 JSON for summaries with human-readable keys; JSONL for observations;
Markdown for human inspection; experimental SCL only where thousands of repeated
events make verbose JSON wasteful.
8.3 `manifest.json` carrying scope, filters, every model version, coverage, the
source run set and the exclusions.

**Gate P8:** an export is reproducible from its manifest alone.

---

## Later only

MCP · HTTP API · public Shifu · hosting · accounts · payments. §62 rules these
out for now; the interface boundary exists so they can be added later without
rewriting the analysis.

---

## Dependencies

```
P0 ─┬──> P2 ──> P3 ──> P5 ──> P6 ──> P7 ──> P8
    │           │                      ^
    ├──> P1 ────┴──────────────────────┘   (Track A feeds P8's timeline export,
    │      (parallel with P2)                Track B is downstream of P6)
    │
    └──> P4  (triggered by measurement, consumed by P5-P7)

COLLECTION ─────────────────────────────>  decides which P5+ claims are supportable
```

P1 and P2 **run in parallel** — no shared files, no shared schema.
P4 is triggered by a measurement, not by a predecessor.
Nothing waits on an SCL standard: Track B's representation is downstream of the
state engine, so freezing an event notation was never on the critical path.
**Collection runs throughout.** It does not gate phases wholesale — it decides,
per claim, which conclusions the evidence block will support.

---

## Effort

Rough, and deliberately so. Each is one focused work session unless noted.

| Phase | Size | Risk | Note |
|---|---|---|---|
| 0 | done, plus two operator commands | low | `hotfix_epochs` blocked on external dates |
| 1 | large — 3-4 sessions | medium | the benchmark may reject the hypothesis, which is a result |
| 2 | medium-large | low | mostly relocation, plus the analytical store scaffolding |
| 3 | medium | low | parser shape depends on live CombatantInfo, which is stored |
| 4 | large | medium | new dependencies; must stay optional |
| 5 | medium | medium | risk is per-claim evidence depth, reported rather than assumed |
| 6 | large | **high** | per-field observability and staleness are where correctness lives |
| 7 | medium | high | cohort matching is easy to get subtly wrong |
| 8 | medium | low | rendering over finished analysis |

Phase 5's risk dropped from **high** in revision 1: the evidence block reports
depth per claim instead of the design relying on a corpus-wide threshold that
would have been wrong in both directions.

---

## Benchmark work that must precede the `reference_player` profile

Not a phase — a prerequisite, and it needs the live API.

| Measurement | Decides |
|---|---|
| Does `sourceID` narrowing reduce points or only rows? | whether focus narrowing saves anything at all |
| Does the API accept `sourceID` and `targetID` together? | whether a focus set can ask for both directions |
| `Buffs` + `sourceID` vs `Buffs` + `targetID` | applied-by vs active-on — different data under one word |
| `Healing` + `targetID` | healing received, needed to separate "kept alive" from "fine" |
| `DamageTaken` + `targetID` vs `DamageDone` + `targetID` | whether the two streams overlap |
| Per-role focus sets | whether a healer's useful focus set differs from a DPS's |

Until these land, `reference_player` keeps its current definition and is **not
recommended for a large corpus**, because it collects party-wide `DamageDone`
and `Healing` while calling itself a focus profile.

---

## What is being asked for at this gate

1. **Which spec and dungeon does the reference corpus target first?** Depth beats
   breadth for every question in the brief, and player-behaviour claims need many
   distinct *people*, not many runs.
2. **P1 and P2 both start now?** They are independent. The alternative is P2
   alone, which reaches a useful analytical layer sooner and leaves the format
   question open longer.
3. **Real patch dates for `hotfix_epochs.yml`** — minutes of typing, large effect
   on the correctness of every recurrence distribution.
4. **Does the benchmark run before more collection?** It is cheap (a handful of
   reports) and it decides whether `reference_player` is worth collecting at all.
