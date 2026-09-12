# API notes

Verified Warcraft Logs v2 API behaviour.

**Evidence:** live recon runs 2026-09-12 19:21 and 19:56 UTC, retail
`www.warcraftlogs.com/api/v2/client`. Second run: 22 requests, 110 KB, 5.9 s,
~23 points of 3600. Raw evidence: `data/exports/recon/recon_findings.json`.

Status tags: `VERIFIED` (observed live, evidence cited) · `CORRECTED`
(observed to differ from the hypothesis) · `HYPOTHESIS` (not yet observed).

> **Two recon runs so far, both partial.** Each failed at `report_metadata`
> for a *different* reason, and both causes were defects in this project, not
> the API (sections 12.1 and 12.3). Both are fixed. Sections 5–7 and 11 stay
> unverified until a third run reaches event data.

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

**VERIFIED — broad discovery works.** On `ReportData.reports`, `guildID` and
`userID` are optional, and a real call was made both ways:

| Scope | Result |
| --- | --- |
| Unscoped (`limit: 3`) | **OK**, 3 reports returned, `has_more_pages: true`. `zone` was `null` on each. |
| Zone-scoped (`zoneID: 55`) | **OK**, 3 reports returned, all `zone: {id: 55, name: "Mythic+ Season 2"}`, `has_more_pages: true`. |

**This is the single most consequential finding for sampling.** The corpus is
not confined to leaderboard entries or one guild's logs; runs can be drawn
broadly across the season zone, which is what the brief's warnings about
leaderboard, uploader and guild bias require.

**CORRECTED — `total` is `-1`.** The pagination object does not report a real
count, so sample size must be tracked by what is actually fetched. Do not use
`total` as a denominator.

`fightRankings` returning `JSON` means its contents must be inspected before
any claim that rankings yield report codes. Not attempted; ranking-derived
sampling would be leaderboard-biased anyway.

---

## 9. Zones and encounters — VERIFIED, with a structural correction

`worldData.zones` returned **44 zones**; `worldData.expansions` returned 8,
newest first: Midnight (7), The War Within (6), Dragonflight (5), Shadowlands
(4), Battle for Azeroth (3), Legion (2), Warlords of Draenor (1), Mists of
Pandaria (0).

**CORRECTED — the structural mistake that mattered most.** A Mythic+ season is
**one zone whose `encounters` are the individual dungeons**, not one zone per
dungeon. Matching dungeon names against *zone* names found **0 of 4**. So, for
a Mythic+ dungeon:

- `wcl_zone_id` = the **season zone** that contains it
- `encounter_ids` = `[the dungeon's own encounter ID]`

### VERIFIED — Midnight Season 2 is zone 55

`Mythic+ Season 2`, expansion 7 (Midnight), `frozen: false`, partition S2
(default), difficulty 10 "Dungeon", size 5. Its eight encounters, confirmed
both from the API and independently by the project owner:

| Encounter ID | Dungeon |
| --- | --- |
| 12993 | Altar of Fangs |
| 12825 | Den of Nalorakk |
| 61762 | Kings' Rest |
| 12813 | Murder Row |
| 112521 | Ruby Life Pools |
| 61877 | Temple of Sethraliss |
| 12859 | The Blinding Vale |
| 12923 | Voidscar Arena |

Note the Warcraft Logs spelling **"Kings' Rest"** (apostrophe after the s);
other spellings are configured as aliases.

### CORRECTED — three dungeon names are reused across seasons

A name alone does not identify a dungeon:

| Dungeon | Also appears in |
| --- | --- |
| Ruby Life Pools | Dragonflight S1 (zone 32, encounter 12521), S4 (zone 37, encounter 62521) |
| Kings' Rest | Battle for Azeroth (zone 20, encounter 11762) |
| Temple of Sethraliss | Battle for Azeroth (zone 20, encounter 11877) |

Zone *names* repeat too: there are two zones called "Mythic+ Season 2"
(43 = The War Within, 55 = Midnight) and two called "Mythic+ Season 1"
(39 = TWW, 47 = Midnight).

`match_zones` therefore runs two passes: match the dungeons unique to the
season, infer the season zone from where they agree, then resolve the reused
names inside that zone. All eight now resolve with no ambiguity. A name still
ambiguous after pass 2 is reported, never resolved — picking arbitrarily would
tie the corpus to another season's mechanics. `season.wcl_mplus_zone_id` in
`config/dungeons.yml` overrides the inference if ever needed.

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
entries and so printed the six *oldest* (Mists of Pandaria … Dragonflight) and
hid the current expansion entirely — making the season look absent from the
API. Now sorted by ID descending, with nothing dropped.

### 12.3 A permission-gated field inside an auto-expanded selection — FIXED

```
You do not have permission to view the avatar for this user.
(partial data was returned and discarded)
```

Fixing 12.1 by expanding every composite to *all* its scalar fields went too
far: `owner` expanded to include `User.avatar`, which is permission-gated in a
way introspection does not reveal. The API returned partial data plus an
error, and the client correctly refused to treat half an answer as complete —
so the report step failed again, on a different cause.

Two-layer fix:

1. **Deny-list** (`SchemaIntrospector.RISKY_LEAF_NAMES`): `avatar` plus media
   and URL names, which carry the same risk and have no research value. These
   are *observed* failures, not guesses.
2. **Drop-and-retry** (`Recon._execute_with_leaf_retry`): any leaf the server
   names in an error is dropped and the query retried, up to twice. The
   deny-list can only hold fields already known to fail; this handles the rest,
   so an unanticipated gated field costs one extra request instead of a whole
   round trip. Dropped fields are recorded as limitations, never silently
   swallowed.

### 12.4 Reused dungeon names reported as unresolvable — FIXED

Name-only matching found Ruby Life Pools in three zones and refused to choose
— safe, but it left a season dungeon unidentified. Two-pass matching (see
section 9) resolves it from the rest of the season's evidence.

## 13. Open questions

Answered since the first run: the season dungeon list (section 9) and whether
`ReportData.reports` works unscoped (section 8).

1. Do events carry `sourceInstance` / `targetInstance`? *(the project's core
   requirement; filter arguments suggest yes, unproven)*
2. Is the event pagination cursor inclusive, and what is the real page ceiling?
3. Is `keystoneLevel` non-null a reliable Mythic+ marker?
4. Is `hostilityType` required to retrieve enemy casts?
5. What does an event page cost in points, by category?
6. Do pull intervals overlap, and do bosses appear as pulls?
7. Can enemy health be reconstructed well enough to time an execute phase?
8. What does `allowUnlisted` change, and does it stay within v1's
   public-only scope?
9. Why did unscoped `reports` return `zone: null`? If unscoped results skip
   Mythic+ reports, zone-scoped discovery is the one to build sampling on.
