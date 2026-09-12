# API notes

Verified Warcraft Logs v2 API behaviour.

**Evidence:** live recon run 2026-09-12 19:21 UTC, retail
`www.warcraftlogs.com/api/v2/client`, 17 requests, 105 KB, 4.8 s, ~2 points.
Raw evidence: `data/exports/recon/recon_findings.json`.

Status tags: `VERIFIED` (observed live, evidence cited) · `CORRECTED`
(observed to differ from the hypothesis) · `HYPOTHESIS` (not yet observed).

> **First recon run was partial.** `report_metadata` failed, which aborted the
> run before fights, pulls, NPC identity, events, pagination and query cost
> were probed. The cause was a defect in this project, not the API — see
> section 12 — and it is fixed. Sections 5–7 and 11 stay unverified until a
> second run.

---

## 1. Authentication — VERIFIED

- Token endpoint `https://www.warcraftlogs.com/oauth/token`, grant
  `client_credentials`, credentials as HTTP Basic. Returned `200 OK`.
- API endpoint `https://www.warcraftlogs.com/api/v2/client`, bearer token.

**CORRECTED — token lifetime.** Assumed ~1 hour; observed **31,104,000 s
(360 days)**. The 120-second early-refresh margin is harmless but effectively
never fires. Do not treat a long lifetime as licence to persist the token: it
is still held in memory only.

---

## 2. Rate limiting — VERIFIED

The hypothesised field names were all correct:

| Concept | Field | Observed |
| --- | --- | --- |
| Points per hour | `limitPerHour` | `3600` |
| Points spent | `pointsSpentThisHour` | `2.0` after a full recon |
| Seconds to reset | `pointsResetIn` | `3142` |

Type `RateLimitData`, reached at `rateLimitData` on the query root.

**Cost is far lower than assumed.** A 17-request recon — full introspection,
zone registry, discovery probes — cost about **2 points of 3600**. Schema and
metadata work is effectively free; the budget will be spent almost entirely on
event pages. `min_points_reserve: 200` is very conservative and can be
revisited once event-page cost is measured.

---

## 3. Report fields — VERIFIED (11 of 12)

Present: `code` `String!`, `title` `String!`, `startTime` `Float!`,
`endTime` `Float!`, `revision` `Int!`, `segments` `Int!`,
`visibility` `String!`, `zone` `Zone`, `owner` `User`, `region` `Region`,
`archiveStatus` `ReportArchiveStatus`.

**Absent: `gameVersion`.** Nothing in the research design depends on it.

`ReportData.report` takes `code` and **`allowUnlisted`** — the latter was not
anticipated and may matter for unlisted reports. Not yet explored; v1 remains
public-reports-only.

---

## 4. Fight fields — VERIFIED (23 of 23)

**Every Mythic+ field the research design needs exists.** `keystoneLevel`,
`keystoneAffixes` `[Int]`, `keystoneBonus`, `keystoneTime`, `countReached`,
`countRequired`, `averageItemLevel` `Float`, `friendlyPlayers` `[Int]`,
`gameZone` `GameZone`, `npcCountMap` `JSON`, plus `id`, `name`, `startTime`,
`endTime`, `encounterID`, `difficulty`, `kill`, `lastPhase`, `size`,
`fightPercentage`, `bossPercentage`, `completeRaid`.

**CORRECTED:** `rating` is `Float`, not `Int`.

`Report.fights` takes `difficulty`, `encounterID`, `fightIDs`, `killType`,
`translate`.

`keystoneLevel` being non-null is still the assumed Mythic+ marker — see the
open questions.

---

## 5. Dungeon pulls — fields VERIFIED, content UNVERIFIED

All 10 wanted fields exist on `ReportDungeonPull`: `id`, `name`, `startTime`,
`endTime`, `encounterID`, `kill`, `x`, `y`, `boundingBox`
`ReportMapBoundingBox`, `maps` `[ReportMap]`.

No pull data has been fetched yet (the run aborted first), so pull *content* —
interval contiguity, whether bosses appear as pulls, what share of events fall
outside every pull — is still unknown.

---

## 6. NPC instance identity — fields VERIFIED, event-level UNVERIFIED

All 6 fields exist on `ReportDungeonPullNPC`: `id`, `gameID`,
`minimumInstanceID`, `maximumInstanceID`, `minimumInstanceGroupID`,
`maximumInstanceGroupID`. **The pull-level half of the project's core
requirement is confirmed available.**

The event-level half is not yet confirmed: whether individual events carry
`sourceInstance` / `targetInstance` so a cast can be attributed to copy 1 vs
copy 2. `Report.events` does accept **`sourceInstanceID` and
`targetInstanceID` as filter arguments**, which strongly suggests per-instance
data exists, but a filter argument is not proof that the field is returned on
each event. The next recon run settles it.

If it fails, per-instance mechanic timing is unsupported by the API and must be
recorded as such rather than inferred from cast ordering.

---

## 7. Events — enum VERIFIED, shape UNVERIFIED

**`EventDataType` — all 14 values exist, exactly as hypothesised:**
`All`, `Buffs`, `Casts`, `CombatantInfo`, `DamageDone`, `DamageTaken`,
`Deaths`, `Debuffs`, `Dispels`, `Healing`, `Interrupts`, `Resources`,
`Summons`, `Threat`. Every event profile in `config/sampling.yml` validates.

**`HostilityType`:** `Friendlies`, `Enemies`.

**`Report.events` arguments — 28, far more than assumed:**
`abilityID`, `dataType`, `death`, `difficulty`, `encounterID`, `endTime`,
`fightIDs`, `filterExpression`, `hostilityType`, `includeResources`,
`killType`, `limit`, `sourceAurasAbsent`, `sourceAurasPresent`, `sourceClass`,
`sourceID`, `sourceInstanceID`, `startTime`, `targetAurasAbsent`,
`targetAurasPresent`, `targetClass`, `targetID`, `targetInstanceID`,
`translate`, `useAbilityIDs`, `useActorIDs`, `viewOptions`, `wipeCutoff`.

Several are directly useful later: `filterExpression` and the aura-presence
filters could answer overlap questions server-side, and `useAbilityIDs` /
`useActorIDs` affect whether IDs or names come back. **None is used yet** —
server-side filtering would discard the raw stream this project exists to
preserve, so it stays a Phase-5 optimisation, not an ingestion shortcut.

Event *shape* (field names per event type, timestamp units, page ceiling) is
still unverified.

---

## 8. Report discovery — arguments VERIFIED, behaviour BEING PROBED

Every probed path exists:

| Path | Returns | Notable arguments |
| --- | --- | --- |
| `ReportData.report` | `Report` | `code`, `allowUnlisted` |
| `ReportData.reports` | `ReportPagination` | `zoneID`, **`gameZoneID`**, `startTime`, `endTime`, `guildID`, `guildName`, `guildServerSlug`, `guildServerRegion`, `guildTagID`, `userID`, `limit`, `page` |
| `Character.recentReports` | `ReportPagination` | `limit`, `page` |
| `Guild.attendance` | `GuildAttendancePagination!` | `guildTagID`, `limit`, `page`, `zoneID` |
| `Encounter.fightRankings` | `JSON` | `difficulty`, `partition`, `metric`, `page`, … |
| `Encounter.characterRankings` | `JSON` | 18 arguments |

**The important find:** on `ReportData.reports`, `guildID` and `userID` are
**optional**, and `zoneID` / `gameZoneID` / `startTime` / `endTime` exist
independently. If the API honours a zone-scoped query with no guild or user
scope, this project has a broad sampling path and is not confined to
leaderboard or single-guild logs — which speaks directly to the sampling bias
the research brief warns about.

Argument presence is not behaviour, so a `reports_probe` recon step now makes
the actual call, unscoped and zone-scoped, and records what comes back. Result
pending the next run.

`fightRankings` returning `JSON` means its contents must be inspected before
any claim that rankings yield report codes. Not attempted; ranking-derived
sampling would be leaderboard-biased anyway.

---

## 9. Zones and encounters — VERIFIED, with a structural correction

`worldData.zones` returned **44 zones**; `worldData.expansions` returned the
full expansion list.

**CORRECTED — the structural mistake that matters most.** A Mythic+ season is
**one zone whose `encounters` are the individual dungeons**, not one zone per
dungeon. `discover-dungeons` compared dungeon names against *zone* names and
matched **0 of 4**. So, for a Mythic+ dungeon:

- `wcl_zone_id` = the **season zone** that contains it
- `encounter_ids` = `[the dungeon's own encounter ID]`

`match_zones` now searches encounters first and falls back to zone names,
recording which way each match was found. A name appearing in two seasons is
reported as ambiguous rather than resolved, since picking one would tie the
corpus to the wrong season.

**Still unresolved:** the eight Season 2 dungeons. The first run's report
recorded only a zone *count*, not the names, so recon now saves a full
`zone_inventory` (every zone with its encounter names) into the findings.

---

## 10. Access limitations — partially VERIFIED

`archiveStatus` exists as `ReportArchiveStatus` with `isArchived`,
`isAccessible`, `archiveDate`, so archived reports can be detected and kept out
of event-frequency denominators. No archived or private report has been
encountered yet, so the handling paths are implemented but unexercised.

---

## 11. Observed API cost — PARTIALLY MEASURED

| Operation | Requests | Points | Bytes |
| --- | --- | --- | --- |
| Full schema recon (introspection, zones, discovery probes) | 17 | ~2 | 105 KB |
| Report metadata / fights / pulls / events | — | not yet measured | — |

Event-page cost is the figure that decides the default profile for large
samples, and it is still unknown.

---

## 12. Defects this recon run found in *this project*

Both were exactly the kind of assumption Phase 0 exists to catch.

### 12.1 Composite fields selected without a sub-selection — FIXED

```
Field "archiveStatus" of type "ReportArchiveStatus" must have a sub selection.
```

Sub-selections came from a **hardcoded map** of field names believed to be
objects. `archiveStatus` was not in it, so the query was invalid and the whole
`report_metadata` step failed — cascading into six skipped steps.

Fixed at the root: `SchemaIntrospector.build_selection` asks introspection for
each field's type **kind** and expands any composite to its scalar and enum
fields. No hardcoded list of object fields remains on the query path. A
composite whose type offers no scalars is skipped and recorded, never sent
bare.

Classification reads the kind off the field's own type reference rather than
looking the name up in the schema type list — built-in scalars are not reliably
enumerated there, and a miss would misclassify a plain `String` field.

### 12.2 Expansion list truncated from the wrong end — FIXED

The API returns expansions **newest first**. Reporting code took the last six
entries, so it printed the six *oldest* (Mists of Pandaria … Dragonflight) and
hid the current expansion entirely — making the season look absent from the
API. Now sorted by ID descending, with nothing dropped.

---

## 13. Open questions

1. Do events carry `sourceInstance` / `targetInstance`? *(the project's core
   requirement; filter arguments suggest yes, unproven)*
2. Is the event pagination cursor inclusive, and what is the real page ceiling?
3. Is `keystoneLevel` non-null a reliable Mythic+ marker?
4. What are the eight Midnight Season 2 dungeons, and which zone holds them?
5. Does `ReportData.reports` answer without a guild or user scope?
6. Is `hostilityType` required to retrieve enemy casts?
7. What does an event page cost in points, by category?
8. Do pull intervals overlap, and do bosses appear as pulls?
9. Can enemy health be reconstructed well enough to time an execute phase?
10. What does `allowUnlisted` change, and does it stay within v1's
    public-only scope?
