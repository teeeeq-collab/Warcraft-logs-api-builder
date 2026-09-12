# Project state

Authoritative summary of where this project is. Updated at every gate.

- **Last updated:** 2026-09-12 (Phase 1 implementation complete)
- **Software version:** 0.2.0
- **Database schema version:** 1 (`migrations/001_initial.sql`)
- **Normalizer version:** 1
- **Query set version:** 4

---

## Current milestone

**Phase 1 — Murder Row pilot. Implementation complete; awaiting real runs.**

Phase 0 closed with Gate A passed after five live runs. Phase 1 is now built
and tested end to end against the synthetic API: schema, ingestion, pull
assignment, resume, duplicate detection, validation reporting and CLI.

**300 tests pass**, all offline.

### What Phase 1 delivers

| Piece | State |
| --- | --- |
| SQLite schema + migrations | 14 tables, schema version 1, built against **observed** event fields |
| Ingestion | report → run → pulls → NPC instances → roster → events |
| Pull assignment | WCL boundaries authoritative; unassigned events kept and counted |
| Resume | Checkpoint is the `event_pages` table, written in the same transaction as its events |
| Idempotent re-ingest | Re-running a job changes no row counts |
| Duplicate detection | Multi-field fingerprint, transitive grouping, **nothing deleted** |
| Validation report | JSON + Markdown, with a reconstructed per-NPC-copy timeline as evidence |
| CLI | `collect`, `dedupe`, `validate`, `stats` added |

### Design decisions the live data forced

1. **Events are fetched per hostility.** An unfiltered cast sample returned 50
   player casts and zero NPC casts. `Casts@Enemies` and `Casts@Friendlies` are
   separate streams in the mechanics profile, because unfiltered would have
   made NPC mechanic timelines invisible.
2. **`source_instance` is never defaulted.** A NULL means the API did not say.
   Resolving it to "copy 1" at ingestion would assert a fact about the pull
   that only the pull's instance range can establish.
3. **Re-ingest identity is `(page_id, seq_in_page)`, not a content hash.** Two
   genuinely identical events can occur at one millisecond; a content hash
   would silently merge them.
4. **`instance_count` carries a confidence.** It is derived from an ID range,
   and a derived multiplicity must never be mistaken for a reported one.

---

## Blockers

### B1 — Pilot runs needed (open, requires user action)

The pipeline has never ingested a real report. It needs 5–10 Murder Row runs
across key levels to prove the same on live data.

```
.venv\Scripts\wclmplus.exe collect --report-list reports.txt --dungeon "Murder Row"
.venv\Scripts\wclmplus.exe validate
```

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
| V1 NPC instance identity | **PASSED against the API** (pull level: 18 copies in one pull; event level: enemy casts, debuffs, damage, interrupts, deaths all carry instance markers). **Reconstructed from the database** in the validation report, so the claim is demonstrated end to end — but only on synthetic data so far. |
| V2 Pagination completeness | **PASSED live.** Exclusive cursor; 316 pages, 7,920 in, 7,920 out. Plus 22 offline tests over both cursor semantics, and a resume test that interrupts mid-stream and lands on identical data. |
| V3-V6 Mechanic timelines | Not started. Needs real pilot runs. |
| V7 Priority interrupt | Reconstructible: `interrupt` carries `extra_ability_game_id` and `target_instance`. Untested on real data. |
| V8 CC stop inference | Not started; needs the confidence-scored inference rules. |
| V9 Event-to-pull assignment | **Implemented and tested offline.** Unassigned events retained and reported. Real assignment rate unknown. |
| V10 Credential safety | **PASSED** across five live runs and the whole Phase 1 pipeline. No credential or player name reaches the database, reports or exports. |

## Next actions

1. **User:** assemble a list of 5-10 public Murder Row report URLs across key
   levels, then run `collect` and `validate`. Send back the validation report.
2. **Coordinator:** review Gate C questions against real data — is
   normalization stable, is resume reliable on a real report, are known
   mechanics visible, is deduplication plausible, is data volume manageable?
3. **Then Phase 2:** expand to 50-100 runs across brackets, measure per-run
   cost and growth, and produce the first empirical mechanic-frequency analysis.

Gate C must pass before Phase 2. The pipeline is proven against synthetic data
only; a real report is the thing that has never been ingested.
