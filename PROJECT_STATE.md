# Project state

Authoritative summary of where this project is. Updated at every gate.

- **Last updated:** 2026-09-12
- **Software version:** 0.1.0
- **Database schema version:** 0 (no schema implemented yet)
- **Normalizer version:** 1
- **Query set version:** 1

---

## Current milestone

**Phase 0 — API reconnaissance. Implementation complete; live verification blocked.**

Everything Phase 0 asks for that does not require network access to
warcraftlogs.com is built and tested. The live verification steps (brief
Phase 0 items 6–18) are implemented as a single command the user runs locally,
`wclmplus recon`, which writes its own evidence files.

### Gate A — API reconnaissance: **NOT PASSED**

| Gate A question | Status |
| --- | --- |
| Are the required fields available? | **Unknown.** Verification implemented (`schema-check`), not executed. |
| How does pagination work? | **Unknown.** Measurement implemented; paginator already correct under either semantics. |
| How can reports be discovered? | **Unknown.** Probe implemented. `ManualReportSource` works regardless. |
| What is the observed API cost? | **Unknown.** Measurement implemented (`measure_query_cost`). |
| Are there access limitations? | **Unknown.** Archived/private handling implemented. |
| Are dungeon pulls sufficiently detailed? | **Unknown.** Query and NPC-identity diagnostics implemented. |

Gate A cannot be approved from this repository alone. It requires one
`wclmplus recon --report <CODE>` run against the live API.

---

## Blockers

### B1 — No live API verification was possible (open, requires user action)

Two independent causes, both established by direct test rather than assumed:

1. **No credentials.** `WCL_CLIENT_ID` / `WCL_CLIENT_SECRET` were not present
   in the build environment. By design they never leave the user's machine.
2. **Network egress to warcraftlogs.com denied.** Every connection attempt was
   refused by the environment's egress proxy:
   `CONNECT tunnel failed, response 403` for `www.warcraftlogs.com`,
   `warcraftlogs.com` and `classic.warcraftlogs.com`. Control hosts
   (`pypi.org`, `api.github.com`) returned HTTP 200 in the same test, so this
   was organization network policy, not a transient fault.

**Consequence:** every statement about the Warcraft Logs schema in this
repository is a *hypothesis*, labelled as such. None has been confirmed.

**Resolution:** the user runs, on their own machine:

```bash
wclmplus auth-check
wclmplus recon --report <PUBLIC_MYTHIC_PLUS_REPORT_CODE>
wclmplus discover-dungeons --write
```

Then `API_NOTES.md` and `VALIDATION.md` are updated from
`data/exports/recon/recon_findings.json`, and Gate A is reassessed.

### B2 — Season dungeon list is incomplete (open)

Four of the eight Midnight Season 2 dungeons are named in the research brief
(Murder Row, Ruby Life Pools, The Blinding Vale, Den of Nalorakk). The other
four are unknown and are **not** guessed. `discover-dungeons` lists the live
zones so the remaining slots can be filled from the API.

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

No mechanic has been validated against real data. All validation cases are
defined in `VALIDATION.md` with status `NOT STARTED — blocked on B1`.

The *machinery* for validation is tested: NPC instance identity is preserved
and proven distinct by fingerprint, duplicate-species pulls are detected, and
pagination completeness is proven against a simulator under both cursor
semantics.

---

## Next actions

1. **User:** create an API client, fill `.env`, run `wclmplus auth-check`.
2. **User:** run `wclmplus recon --report <CODE>` with a public Mythic+ log.
3. **User:** run `wclmplus discover-dungeons --write`.
4. **Coordinator:** fold the findings into `API_NOTES.md` and `VALIDATION.md`,
   reassess Gate A.
5. **Then Phase 1:** SQLite schema and migrations, ingestion, pull assignment,
   checkpoint/resume, dedupe, validation report — 5–10 Murder Row runs.

Phase 1 must not start before step 4. The storage schema depends on the real
event field names, which are still unknown.
