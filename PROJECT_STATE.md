# Project state

Authoritative summary of where this project is. Updated at every gate.

- **Last updated:** 2026-09-12 (after fourth live recon run — Gate A)
- **Software version:** 0.1.4
- **Database schema version:** 0 (no schema implemented yet)
- **Normalizer version:** 1
- **Query set version:** 4

---

## Current milestone

**Phase 0 — API reconnaissance. COMPLETE. Gate A passed.**

The fourth live run finished all 16 steps with 0 failures and one recorded
limitation (`Report.gameVersion` does not exist, and nothing needs it).

### Gate A — API reconnaissance: **PASSED**

| Gate A question | Answer |
| --- | --- |
| Are the required fields available? | **Yes.** ReportFight 23/23, ReportDungeonPull 10/10, ReportDungeonPullNPC 6/6, Report 11/12. All 14 `EventDataType` values. |
| How does pagination work? | **Measured.** Cursor is **exclusive**. A 316-page traversal returned 7,920 events and emitted 7,920 — nothing lost, nothing double-counted, no out-of-order timestamps, no warnings. |
| How can reports be discovered? | **Broadly.** `ReportData.reports` answers unscoped *and* zone-scoped with no guild or user seed. Zone-scoped is the one to build on: it returns exactly the Season 2 population. |
| What is the observed API cost? | **Measured, and not the constraint.** The whole run cost ~29 points of 3600/hour; 316 event pages ≈ 0.05 points each. Time and bytes bind first: 97 s and 3.3 MB. |
| Are there access limitations? | **Yes, and detectable.** `archiveStatus` works; `User.avatar` and `User.battleTag` are permission-gated and are no longer requested. |
| Are dungeon pulls sufficiently detailed? | **Yes.** 13 pulls on one +10 run, each with its enemy NPC list, coordinates, bounding box and map. |

### What the data turned out to support

Three findings materially exceed what the brief assumed possible:

- **`maxHitPoints` is on every damage event**, so damage as a fraction of
  player health is directly reconstructible. The brief said not to guess at
  this; no guessing is needed.
- **`buffs` is on every damage event** — the aura IDs active on the target at
  the moment of the hit. "Was a defensive up for this tankbuster" becomes a
  lookup rather than a correlation across timelines.
- **`unmitigatedAmount` alongside `mitigated`** separates what the mob swung
  for from what the tank actually took — the difference between measuring
  danger and measuring mitigation.

Plus `extraAbilityGameID` on both interrupts and dispels, naming *what was
interrupted* and *what was removed* — the two links the brief's interrupt and
dispel questions depend on.

### The one open item

The Casts sample returned 50 events, **every one from a player**. Five players
out-cast the trash in raw event count, and players are not instanced, so it
carried no `sourceInstance` and said nothing about NPC cast timelines.

Enemy instance identity is confirmed on Debuffs, DamageTaken, Interrupts,
Buffs, Deaths and Summons, so the expectation for casts is strong — but
expectation is not evidence, and per-NPC recast timing is the fact this
project's headline statistics rest on. Recon now samples enemy casts separately
with `hostilityType: Enemies`; one short run closes it.

**Lesson carried into Phase 1:** an unfiltered event query is dominated by
players. `hostilityType` is not an optimisation here; it is what makes the
enemy side visible at all.

---

## Blockers

### B1 — Confirm enemy-cast instance identity (open, one short run)

```
.venv\Scripts\wclmplus.exe recon --report "<PUBLIC_MYTHIC_PLUS_REPORT_URL>"
```

Look for `Casts_enemies` in the findings with
`events_with_source_instance > 0`. If it is zero, per-instance recast timing is
**not supported by the API** and must be recorded as such rather than inferred
from cast ordering.

### B2 — Season dungeon list — **RESOLVED**

Zone 55, all eight dungeons, persisted to `config/dungeons.discovered.yml`.

### B3 — Live verification cannot be done in this environment (permanent)

No credentials, and egress to `warcraftlogs.com` is denied by the build
environment's proxy. All live evidence comes from the user's machine, by
design — their Client Secret never leaves it.

---

## Completed

- Repository scaffold, packaging, git hygiene (`.env` ignored, data ignored).
- Secret redaction layer: exact-value and pattern-based, applied to logs,
  exceptions and cache writes. 13 tests.
- OAuth2 client-credentials authentication with in-memory token caching,
  early-refresh margin, and every failure mode mapped to an actionable error.
  18 tests.
- Rate-limit awareness: defensive field parsing, hourly budget guard with a
  reserve, `Retry-After` handling, exponential backoff with jitter.
- GraphQL client: retry classification (transient vs. permanently invalid),
  401-triggered single token refresh, cache-backed idempotent execution. 26 tests.
- Raw cache: versioned identity, atomic gzip writes, corruption tolerance, and
  a hard refusal to write any payload containing a registered credential. 22 tests.
- **Event paginator**: correct under inclusive *or* exclusive cursor semantics,
  multiset boundary matching, progress verification, page ceiling, resumable
  checkpoints, gap diagnostics. 22 tests.
- Schema introspection and field verification; queries generated only from
  fields confirmed to exist.
- Recon engine writing `RECON_REPORT.md` + `recon_findings.json`, including
  empirical pagination-semantics measurement. 22 tests.
- Report discovery abstraction with `ManualReportSource`, URL/code parsing, and
  provenance records. API-backed sources refuse to run unverified. 21 tests.
- Config layer: dungeon registry with a generated discovery overlay, sampling
  profiles, hotfix epochs with overlap rejection. 27 tests.
- Fixture sanitizer: player names pseudonymized, NPC names preserved.
- CLI with 10 commands, 5 of which need no credentials. 23 tests.
- **216 tests, all passing, none requiring credentials or network.**

---

## Active work

None. Awaiting the user's `wclmplus recon` run to unblock Gate A.

---

## Important decisions

| # | Decision | Reason |
| --- | --- | --- |
| D1 | No schema field is asserted anywhere. Queries are generated from introspected fields. | The brief requires the live schema to win. A missing field must be a recorded absence, not a mid-run crash. |
| D2 | Paginator is correct under both inclusive and exclusive cursor semantics. | Cursor semantics were unverifiable. Handling both removes the guess entirely. |
| D3 | Boundary de-duplication uses **multiset** matching, not a set. | Two byte-identical events can occur at one millisecond. A set would silently delete real observations. |
| D4 | Dungeon IDs live in a generated overlay file, not written back into `dungeons.yml`. | Round-tripping YAML would strip the authored comments and validation targets. |
| D5 | No database schema in Phase 0. | Phase 0 is reconnaissance. Storage design belongs to Phase 1, informed by the real event shapes. |
| D6 | Synthetic API simulator for tests, clearly separated from `tests/fixtures/`. | Offline tests need something to run against, but synthetic data must never be mistaken for API evidence. |
| D7 | Raw cache blocks writes containing credentials, rather than relying on redaction alone. | Defence in depth: turns a possible leak into a loud failure. |
| D8 | No worker subagents were used for Phase 0. | The brief says not to over-orchestrate. Once live verification was blocked, Phase 0 reduced to one coherent scaffold task, and no worker could have unblocked the network. |
| D9 | Report-code parsing errs toward acceptance. | Code length is not pinned. A wrong code fails loudly at the API; rejecting by a guessed length would silently drop valid runs. `report-list-check` catches typos first. |

---

## Validation status

| Case | Status |
| --- | --- |
| V1 NPC instance identity | **Confirmed for Debuffs, DamageTaken, Interrupts, Buffs, Deaths, Summons.** Pull level confirmed on a real fixture: 18 copies of NPC 236085 in one pull. Enemy *casts* pending one probe. |
| V2 Pagination completeness | **PASSED live.** Exclusive cursor; 316 pages, 7,920 in, 7,920 out, no warnings. Plus 22 offline tests covering both cursor semantics. |
| V3-V6 Mechanic timelines | Ready to attempt — every event shape they need is confirmed present. |
| V7 Priority interrupt | **Reconstructible.** `interrupt` events carry `extraAbilityGameID` (what was interrupted) and `targetInstance` (which copy). |
| V8 CC stop inference | Ready to attempt; still needs the confidence-scored inference rules. |
| V9 Event-to-pull assignment | NOT STARTED — Phase 1. |
| V10 Credential safety | **PASSED** across four live runs. No credential in any output, report or fixture. |

## Next actions

1. **User:** one more `recon` run to confirm enemy-cast instance identity (B1).
2. **Coordinator:** close B1 in `API_NOTES.md` and `VALIDATION.md`.
3. **Phase 1 — can begin in parallel**, since the event shapes that determine
   the storage schema are confirmed:
   - SQLite schema and migrations, built against the *observed* event fields
   - Ingestion: report → run → pulls → NPC instances → events
   - Event-to-pull assignment with diagnostics
   - Checkpoint/resume, idempotent re-ingest, duplicate-run detection
   - Validation report, then 5-10 Murder Row runs

Phase 1's schema design no longer rests on guesses: `DATA_DICTIONARY.md` can be
rewritten against real field lists.
