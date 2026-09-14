# Implementation Phases

Ordering, acceptance and effort for the work proposed in
[`SHIFU_ARCHITECTURE_PLAN.md`](SHIFU_ARCHITECTURE_PLAN.md),
[`ANALYTICAL_DATA_MODEL.md`](ANALYTICAL_DATA_MODEL.md) and
[`SCL_EXPERIMENT_PLAN.md`](SCL_EXPERIMENT_PLAN.md).

Per brief §68 the design package stops here and waits for review before any
large architectural work begins. Phase 0 items marked **DONE** were authorised
as small critical fixes.

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
| 0.8 | Investigate 2 `overlapping_pulls` warnings | **open** — cheap; bears on pull-local timing |

**Doc corrections made in 0.6.** `API_NOTES.md` concluded "quota, not wall clock,
is the binding constraint" from a ~37 points/run estimate. The measured cost is
**13.19 points/run**, which reverses the conclusion: wall clock binds, not quota.
The estimate is kept next to the measurement rather than deleted, because the
reason the estimate was wrong is itself useful.

**Gate P0:** focus resolution proven by test · identity pinned · dedupe run ·
fresh validation report · no doc asserting a number the corpus contradicts.

---

## Phase 1 — compression experiment framework

Independent of every other phase. It reads the corpus and writes nothing to it,
so it can run in parallel with collection and with Phase 2.

1.1 Select and freeze 20-50 fixture pulls covering the §20 case list, grouped by
stream coverage so format comparisons are not secretly coverage comparisons.
1.2 Measure redundancy: `P(enemy melee target = tank)` by dungeon, species, pull
and bracket; ability-vocabulary size per spec and per species; event-type
distribution.
1.3 Implement candidates A-F, each with an encoder **and** a decoder.
1.4 Measure size and tokens with a named tokenizer; label proxy counts as proxy.
1.5 Build comprehension fixtures with expected answers computed from the
database.
1.6 Write `SCL_BENCHMARK.md` + `scl_benchmark.json`.

**Do not standardize.** Output is a table and a recommendation.

**Gate P1:** every candidate round-trips a real pull exactly (or documents its
loss exactly) · no token count is an estimate.

---

## Phase 2 — query / repository layer

Build this before anything else in Layer 2. Everything downstream is a client of
it, and adding six analytical modules onto scattered SQL would make the corpus's
access rules unenforceable.

2.1 `repository/` with run, pull, event, player, build and mechanic access.
2.2 **Canonical-by-default** filtering, with an explicit `include_duplicates`.
2.3 **Coverage-checked reads**: a stream query takes the stream it needs and
excludes runs that never requested it, returning the exclusion count.
2.4 Streaming/batched iteration — never materialise millions of rows.
2.5 Move `packs` SQL out of `cli.py`; output stays identical.

**Gate P2:** no analytical read touches SQLite directly · a missing stream cannot
produce a zero · `packs` byte-identical before and after.

---

## Phase 3 — normalized build model

Cheapest high-value schema work in the package: the data is already collected,
the backfill is offline, and it unblocks every cohort question.

3.1 CombatantInfo parser with its own version.
3.2 Migration 005; deterministic `build_id`.
3.3 Backfill the 460 existing CombatantInfo events — no API calls.
3.4 `run_player_builds` linkage.
3.5 Keep `hero_talent_status = 'unknown'`; a cohort filtered on hero talent
refuses to run rather than returning the populated subset.

**Gate P3:** known fixture → known build · identical builds dedupe · a respec
stays two builds · raw payload retained.

---

## Phase 4 — analytical projection

**Build on the measured trigger, not on schedule** (architecture plan §4.7): a
representative cross-run query exceeding ~30 s on SQLite, or ~50 M events.
Neither holds today at 4.97 M events.

4.1 Parquet writer with the §6 partition layout.
4.2 DuckDB query surface behind the same repository interface.
4.3 Incremental projection, plus a full deterministic rebuild.
4.4 Dictionary tables for actors, NPCs, abilities, items, builds, archetypes.

**Gate P4:** same input, same bytes · deleting `data/analytics/` loses nothing ·
the offline suite passes with neither pyarrow nor DuckDB installed.

---

## Phase 5 — first empirical analysis

**Blocked on corpus size, not on code.** See architecture plan §7: 94 runs cannot
support a cohort claim about any spec in any dungeon. Building the machinery is
fine; publishing numbers from it is not.

5.1 Mechanic model: first-cast timing, recast intervals and distributions,
target distribution, per-pull and per-instance counts, damage distributions,
interrupt and dispel relationships.
5.2 Canonical action mapping (migration 006), from `paired_abilities` evidence.
5.3 Pull archetypes (migration 007), interpretable features first.
5.4 Every output carries N, coverage, run provenance, hotfix epoch, key bracket.

**Gate P5:** no statistic without N and coverage · a 4:1 paired ability is not
counted as four actions · no statistic drawn from runs that never collected the
stream it needs.

---

## Phase 6 — gameplay state engine

6.1 State schema (migration 008) with the four-way observability tag.
6.2 Reconstruction from events, per player, per moment of interest.
6.3 Action extraction and outcome windows — components, never a score.
6.4 Cooldown availability stays `unknown` until external metadata exists.

**Gate P6:** a hand-authored timeline reconstructs a known state exactly · every
field carries observability · `unknown` survives into every export.

---

## Phase 7 — cohorts, sequences, similarity

7.1 Cohort definitions (migration 009) with mandatory `required_streams`.
7.2 Action sequences with their initial state and outcome.
7.3 Interpretable similarity features with configurable weights; evaluate
nearest-neighbour methods only after exact and feature matching are measured.
7.4 Representative example retrieval with run/pull/time provenance.

**Gate P7:** every cohort reports what it excluded and why · no "user vs top
parse" comparison exists anywhere in the code.

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
P0 ──┬─> P2 ──> P3 ──> P5 ──> P6 ──> P7 ──> P8
     │          │                      ^
     ├─> P1 ────┴──────────────────────┘   (SCL feeds P8's timeline export)
     │
     └─> P4  (triggered by measurement, consumed by P5-P7)

COLLECTION ──────────────────────────────> gates P5 onward on sample size
```

P1 is genuinely independent — it can start today.
P4 is triggered by a measurement, not by a predecessor.
**Collection is the long pole for everything from P5 on, and no amount of
engineering shortens it.** It should run in parallel from now.

---

## Effort

Rough, and deliberately so. Each is one focused work session unless noted.

| Phase | Size | Risk | Note |
|---|---|---|---|
| 0 | done, plus two operator commands | low | 0.7 blocked on external dates |
| 1 | large — 3-4 sessions | medium | the benchmark may reject the hypothesis, which is a result |
| 2 | medium | low | mostly relocation of existing SQL |
| 3 | medium | low | parser shape depends on live CombatantInfo, which is stored |
| 4 | large | medium | new dependencies; must stay optional |
| 5 | medium | **high** | risk is statistical, not technical: N |
| 6 | large | high | observability tagging is where correctness lives |
| 7 | medium | high | cohort matching is easy to get subtly wrong |
| 8 | medium | low | rendering over finished analysis |

---

## What is being asked for at this gate

Per §68, this package stops here. What would help most:

1. **Which spec and dungeon does the reference corpus target first?** Depth beats
   breadth for every question in the brief.
2. **Phase 1 or Phase 2 first?** P1 is independent and answers a question nothing
   else can. P2 unblocks the rest of the stack. Recommendation: **start P2**, run
   collection alongside, and take P1 when there are enough fixtures to make the
   benchmark representative.
3. **Real patch dates for `hotfix_epochs.yml`** — five minutes of typing, large
   effect on the correctness of every recurrence distribution.
