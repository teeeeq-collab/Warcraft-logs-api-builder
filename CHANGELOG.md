# Changelog

Collector, schema, normalization and analysis changes.

Version stamps that matter for reproducibility: `software_version`,
`schema_version`, `normalizer_version`, `query_version`. Any change that alters
what gets collected or how it is interpreted must bump the relevant one and be
recorded here.

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
