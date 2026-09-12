# Project state

Authoritative summary of where this project is. Updated at every gate.

- **Last updated:** 2026-09-12 (after first live recon run)
- **Software version:** 0.1.0
- **Database schema version:** 0 (no schema implemented yet)
- **Normalizer version:** 1
- **Query set version:** 1

---

## Current milestone

**Phase 0 — API reconnaissance. First live run done; one more needed.**

The user ran `wclmplus recon` against the live API on 2026-09-12. It answered
about half of Gate A and found two defects in this project, both now fixed.

### Gate A — API reconnaissance: **PARTIALLY PASSED**

| Gate A question | Status |
| --- | --- |
| Are the required fields available? | **YES.** ReportFight 23/23, ReportDungeonPull 10/10, ReportDungeonPullNPC 6/6, Report 11/12 (`gameVersion` absent, unused). EventDataType all 14 values. |
| What is the observed API cost? | **Cheap.** 17 requests ≈ 2 points of 3600/hour. Event-page cost still unmeasured. |
| How can reports be discovered? | **Arguments confirmed; behaviour being probed.** `ReportData.reports` takes `zoneID`/`gameZoneID` with `guildID`/`userID` optional — potentially a broad sampling path. A probe now tests it. |
| Are there access limitations? | **Detectable.** `archiveStatus` exposes `isArchived`/`isAccessible`. Not yet exercised on a real archived report. |
| How does pagination work? | **Still unknown.** Blocked by the defect below. |
| Are dungeon pulls sufficiently detailed? | **Fields yes, content unknown.** No pull was fetched. |

The run aborted at `report_metadata`, so fights, pulls, NPC identity, events,
pagination and query cost were never probed. Cause was ours, not the API's.

### Defects the live run found — both fixed

1. **Composite fields selected without a sub-selection.** Sub-selections came
   from a hardcoded map of field names assumed to be objects; `archiveStatus`
   was missing from it, so the query was invalid and six steps were skipped.
   Now derived from introspection: `SchemaIntrospector.build_selection` reads
   each field's type kind and expands composites automatically. No hardcoded
   object-field list remains on the query path.
2. **Mythic+ dungeons are encounters, not zones.** `discover-dungeons`
   compared dungeon names against zone names and matched 0 of 4. A Mythic+
   season is one zone whose `encounters` are the dungeons. Matcher rewritten;
   ambiguous names are reported rather than resolved.

Plus a reporting bug: expansions come back newest first, and the summary took
the last six entries — printing the six *oldest* and hiding the current
expansion, which made the season look absent from the API.

**232 tests pass**, including regression tests for all three. The simulator now
enforces the server's sub-selection rule and models a season zone with dungeon
encounters, so none of these can regress silently.

---

## Blockers

### B1 — Second recon run needed (open, requires user action)

Everything that follows `report_metadata` is unverified: pull content, NPC
event-level identity, event shape, pagination semantics, event-page cost.

The user re-runs, after `git pull`:

```
.venv\Scripts\wclmplus.exe recon --report "<PUBLIC_MYTHIC_PLUS_REPORT_URL>"
.venv\Scripts\wclmplus.exe discover-dungeons
```

### B2 — Season dungeon list still unknown (open)

The first run recorded a zone *count* (44) but not zone or encounter names, so
the eight Season 2 dungeons remain unidentified. Recon now writes a full
`zone_inventory` — every zone with its encounter names — into the findings, so
the next run resolves this without another round trip.

### B3 — Live verification cannot be done in this environment (permanent)

No credentials, and egress to `warcraftlogs.com` is denied by the build
environment's proxy (403 on CONNECT; control hosts returned 200). All live
evidence therefore comes from the user's machine, by design — their Client
Secret never leaves it.

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
| V1 NPC instance identity | **Half confirmed.** All 6 pull-NPC identity fields exist. Event-level `sourceInstance` unverified; `Report.events` accepts `sourceInstanceID`/`targetInstanceID` filters, which suggests it exists. |
| V2 Pagination completeness | Offline PASSED (22 tests, both cursor semantics). Live unverified. |
| V3–V6 Mechanic timelines | NOT STARTED — needs event data. |
| V7 Priority interrupt | NOT STARTED. |
| V8 CC stop inference | NOT STARTED. |
| V9 Event-to-pull assignment | NOT STARTED — Phase 1. |
| V10 Credential safety | **PASSED**, now including a real live run: no credential appeared in any output, report or fixture. |

## Next actions

1. **User:** `git pull`, then re-run `recon --report <CODE>` and
   `discover-dungeons`. Send back `recon_findings.json`.
2. **Coordinator:** confirm event-level NPC instance identity, pagination
   semantics and event-page cost; fill in `API_NOTES.md` sections 5–7 and 11;
   identify the eight Season 2 dungeons from `zone_inventory`.
3. **Coordinator:** close Gate A.
4. **Then Phase 1:** SQLite schema and migrations, ingestion, pull assignment,
   checkpoint/resume, dedupe, validation report — 5–10 Murder Row runs.

Phase 1 must not start before step 3. The storage schema depends on the real
event field names, which remain the single biggest unknown.
