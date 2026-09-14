# Project state

Authoritative summary of where this project is. Updated at every gate.

- **Last updated:** 2026-09-14 (design package revision 2; awaiting second review)
- **Software version:** 0.3.0
- **Database schema version:** 4 (`migrations/004_identity_scheme.sql`)
- **Normalizer version:** 2
- **Query set version:** 5
- **Identity scheme version:** 1
- **Tests:** 377, all offline, all passing

---

## Current milestone

**Architecture phase. The design package is written and waiting for review.**

Gate A closed on live data (five recon runs). Gate C closed on a real corpus:
94 Mythic+ runs from 10 reports, 4,973,745 events, 2.18 GB, 97.83% of events
assigned to a Warcraft Logs pull boundary, and twelve independent copies of one
NPC species converging on 2.97-4.93 s recast intervals — which is the
measurement that proves instance identity is real and not conflated.

The project has since been given a much larger scope: evolve the collector into
a layered research platform capable of supporting an LLM-facing gameplay teacher
("Shifu"). The response to that brief is a design package, not an
implementation:

| Document | Contents |
| --- | --- |
| [`SHIFU_ARCHITECTURE_PLAN.md`](SHIFU_ARCHITECTURE_PLAN.md) | layering, component dispositions, explicit limits, sample-size reality |
| [`ANALYTICAL_DATA_MODEL.md`](ANALYTICAL_DATA_MODEL.md) | schemas for builds, actions, archetypes, state, cohorts, projection |
| [`SCL_EXPERIMENT_PLAN.md`](SCL_EXPERIMENT_PLAN.md) | compression candidates, metrics, comprehension benchmark, gate |
| [`IMPLEMENTATION_PHASES.md`](IMPLEMENTATION_PHASES.md) | ordering, acceptance gates, effort, dependencies |
| [`ARCHITECTURE_REVIEW_RESPONSES.md`](ARCHITECTURE_REVIEW_RESPONSES.md) | per-amendment responses to the first review: 16 agree, 3 corrections to my own errors |
| [`CURRENT_STATE_ARCHITECTURE_AUDIT.md`](CURRENT_STATE_ARCHITECTURE_AUDIT.md) | 40-part inspection of the code as it stands |

Per the brief's §68, no large architectural work begins until that package is
reviewed.

---

## What exists and works

| Piece | State |
| --- | --- |
| Auth, redaction, raw cache | credentials never leave the operator's machine; a cache write containing one is refused |
| GraphQL client | retry classification, introspection-driven queries, no asserted field names |
| Pagination | cursor-exclusive, checkpointed in `event_pages`, exact resume |
| Normalization | **lossless** — unpromoted fields go to `events.extra`; raw payloads retained |
| Pull assignment | WCL boundaries authoritative; unassigned events kept and counted |
| NPC instance identity | `sourceInstance` never defaulted; a NULL stays unknown |
| Stream coverage | `run_stream_coverage` distinguishes "asked, none" from "never asked" |
| Collection profiles | seven, config-driven, from `metadata` to `forensic_full` |
| Cross-log pack identity | `species_signature` + `composition_signature`, both indexed |
| Focus-player collection | **fixed** — see below |
| Identity scheme | versioned and pinned; a mismatched corpus is refused |
| Duplicate detection | multi-field fingerprint, transitive grouping, **nothing deleted** |
| Validation | coverage, cost, pairing, dedupe state, per-copy timelines, limitations |

## Measured facts

Numbers, not estimates. Each was measured against the live API or the corpus.

| Fact | Value |
| --- | --- |
| API cost per run | **13.19 points** of 3,600/hour |
| Storage per run | ~23 MB |
| Bytes per event | 437 |
| `hostilityType` default | **Friendlies** — it never means "both" (verified on Casts, Buffs, DamageDone, Healing, Threat) |
| `Resources@Enemies` | returns zero rows; removed from the profiles |
| `Threat` | carries no threat value |
| `CombatantInfo` | ignores hostility; 5 events/fight, ~36 KB, 2 points — the best value in the API |
| `DamageDone` | 89,703 events/run — 1.65× the entire corpus, per run |
| `Buffs@Enemies` | 30,863 events that had never been collected before the split |

---

## Recent changes

**`--focus-player` could never match.** `normalize_actors` stores players as
`pseudonym(name)`, and resolution compared the typed character name against
that. Every `reference_player` collection reported success and collected no
focus stream. Fixed: the name goes through the same function before matching;
absence in one run stays a warning, matching nothing anywhere is now an error
with a non-zero exit; and the diagnostic records the pseudonym rather than
writing a real character name into the database.

**Identity is versioned.** Pseudonyms are the only handle the corpus has on a
person, so the pseudonym function is part of the data format. A database now
records the scheme its names were written under and refuses a job under a
different one.

---

## Open items

| # | Item | Owner |
| --- | --- | --- |
| O1 | `wclmplus dedupe` has **never been run** on the live corpus — `analysable: 94` is an upper bound | user |
| O2 | Regenerate `validate` after O1, with the corrected worked-seconds metric | user |
| O3 | `config/hotfix_epochs.yml` is empty — needs real Blizzard patch dates, which will not be invented | user |
| O4 | Two `overlapping_pulls` warnings unexplained | coordinator |
| O5 | Review design package revision 2 and choose the first reference spec/dungeon | user |
| O6 | `reference_player` collects party-wide DamageDone/Healing while calling itself a focus profile — **not recommended for a large corpus** until the focus-filter benchmark runs | coordinator |

---

## What the current corpus can and cannot support

Not a single threshold — it depends on which unit is independent for the claim.

| Claim | Independent unit | 94-run corpus |
| --- | --- | --- |
| this NPC casts ability X | observation | **supported** |
| its recast interval is 2.97-4.93 s | NPC instance | **supported** — 12 copies |
| this pack sits at this route position | pull | likely supported |
| this mechanic targets non-tanks ~30% | cast | supported for common abilities |
| Holy Priests press Apotheosis here | **player** | not supported |
| Apex outperforms non-Apex | player, matched | not supported |

Enemy-behaviour claims are cheap — hundreds of independent NPC instances per
run. Player-behaviour claims are expensive: at most five people per run, often
the same people across a report. Collection is the long pole for the second kind
only, and it should run in parallel with every phase from here.

---

## Important decisions

| # | Decision | Reason |
| --- | --- | --- |
| D1 | No schema field is asserted anywhere; queries are generated from introspection | the live schema wins; a missing field must be a recorded absence, not a crash |
| D2 | Paginator correct under inclusive *and* exclusive cursor semantics | removes the guess entirely |
| D3 | Boundary de-duplication uses multiset matching | two byte-identical events can occur in one millisecond |
| D7 | Raw cache refuses writes containing credentials | turns a possible leak into a loud failure |
| D10 | Events fetched per hostility | an unfiltered cast sample returned 50 player casts and zero NPC casts |
| D11 | `source_instance` never defaulted | resolving NULL to "copy 1" asserts a fact only the instance range can establish |
| D12 | `instance_count` carries a confidence | a derived multiplicity must not be mistaken for a reported one |
| D13 | `DamageDone` collected party-wide, not focus-only | collecting one player of five means every question about the other four needs the run fetched again |
| D14 | Identity scheme versioned and pinned by test | changing the salt renames every player in every corpus |
| D15 | Analytical projection designed now, built on a measured trigger | at 4.97 M events SQLite is not the bottleneck; tuning for the wrong shape is worse than waiting |
| D16 | Derived analysis lives in a **separate, disposable database** | "Layer 2 never writes to Layer 1" has to be physical or it is not true |
| D17 | Build identity is **five separable dimensions**, not one hash | a composite hash made item level a talent variable and would have reported N=1 for configurations dozens of players ran |
| D18 | State fields carry age and a staleness horizon, not just a tag | HP seen 40 ms ago and 8 s ago must not look equally trustworthy |
| D19 | Cohorts have both dynamic definitions and **immutable evaluations** | "who matches now" and "who produced this number" are different questions; only the second is reproducible |
| D20 | Every statistic reports **seven counts plus concentration** | N=100,000 states from ten players is a lie of composition |
| D21 | Projection determinism asserted on a **logical fingerprint**, not bytes | byte-identity pins pyarrow forever and fails on dependency bumps with no row changed |

---

## Validation status

| Case | Status |
| --- | --- |
| V1 NPC instance identity | **PASSED on real data.** Twelve independent copies converged on 2.97-4.93 s intervals |
| V2 Pagination completeness | **PASSED live.** Exclusive cursor; 316 pages, 7,920 in, 7,920 out |
| V9 Event-to-pull assignment | **PASSED.** 97.83% assigned; the remainder retained and counted |
| V10 Credential safety | **PASSED.** No credential or player name reaches the database, reports or exports |
| V3-V6 Mechanic timelines | reconstructible; **blocked on sample size**, not on code |
| V7 Priority interrupt | reconstructible — `interrupt` carries `extra_ability_game_id` and `target_instance` |
| V8 CC stop inference | not started; needs the confidence-scored inference rules |

---

## Next actions

1. **User:** run `dedupe`, then `validate` (O1, O2).
2. **User:** review the design package; answer the three questions at the end of
   `IMPLEMENTATION_PHASES.md`.
3. **User:** keep collecting. Everything from Phase 5 onward is gated on N.
4. **Coordinator:** on approval, begin Phase 2 (the repository layer), since
   every other analytical module is a client of it.
