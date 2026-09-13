# API notes

Verified Warcraft Logs v2 API behaviour.

**Evidence:** four live recon runs on 2026-09-12, retail
`www.warcraftlogs.com/api/v2/client`, against a public Murder Row report.
Fourth run: 16/16 steps OK, 355 requests, 3.3 MB, 97 s, **~29 points of 3600**.
Raw evidence: `data/exports/recon/recon_findings.json`.

Status tags: `VERIFIED` (observed live, evidence cited) · `CORRECTED`
(observed to differ from the hypothesis) · `HYPOTHESIS` (not yet observed).

> **Fourth run completed in full: 16 steps, 0 failures.** Pulls, NPC identity,
> event shape, pagination semantics and cost are all now observed rather than
> assumed. The three earlier runs each failed at `report_metadata` for a
> different reason, every cause a defect in this project rather than the API
> (sections 12.1, 12.3, 12.5).
>
> **Gate A is closed.** The final unknown — whether enemy *cast* events carry
> instance identity — was confirmed on a fifth run (section 6).

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

## 5. Dungeon pulls — VERIFIED

All 10 wanted fields exist and return data: `id`, `name`, `startTime`,
`endTime`, `encounterID`, `kill`, `x`, `y`, `boundingBox`, `maps`.

Observed on one +10 Murder Row run: **13 pulls**, each with an `enemyNPCs`
list. The fight itself carried `npcCountMap` with 25 distinct NPC game IDs and
their counts, which is a useful cross-check on pull composition.

WCL's own pull boundaries are the source of truth for this project. No
combat-gap detector is implemented, by design.

**Still unmeasured:** whether pull intervals ever overlap, whether bosses
appear as pulls, and what share of events fall outside every pull. Those are
Phase 1 diagnostics, not schema questions.

## 6. NPC instance identity — VERIFIED (the project's core requirement)

**Pull level.** All six fields exist and populate: `id`, `gameID`,
`minimumInstanceID`, `maximumInstanceID`, `minimumInstanceGroupID`,
`maximumInstanceGroupID`.

One +10 Murder Row run gave 20 pulls-with-duplicates, including:

| Pull | NPC game ID | Copies (instance span) |
| --- | --- | --- |
| 1 | 236085 | **18** |
| 4 | 253324 | 16 |
| 5 | 255050 | 11 |
| 3 | 253324 | 8 |

Eighteen copies of one species in a single pull. Merging those into one
timeline would have manufactured seventeen phantom "recasts".

**Event level — instance identity is present on enemy events:**

| Category | Instance fields observed |
| --- | --- |
| Buffs | `sourceInstance`, `targetInstance` |
| Debuffs | `sourceInstance` (e.g. enemy 11, instance 2) |
| DamageTaken | `sourceInstance` (e.g. enemy 12, instance 1) |
| Interrupts | `targetInstance` (which copy was interrupted) |
| Deaths | `killerInstance` |
| Summons | `targetInstance` |

So a debuff application, a damage hit, an interrupt and a death can each be
attributed to a specific copy of an NPC.

**Enemy casts — CONFIRMED.** The unfiltered Casts sample returned 50 events
from source IDs `[1, 2, 3, 7, 8]` — the five players — with **0** carrying
`sourceInstance`. Players are not instanced, so that sample could not have
answered the question either way.

Filtering to `hostilityType: Enemies` on the same fight:

| | Unfiltered | `Enemies` |
| --- | --- | --- |
| Distinct source actors | 5 (all players) | **11 NPCs** |
| Events with `sourceInstance` | 0 of 50 | **36 of 50** |
| Event types | `cast` 48, `begincast` 2 | `cast` 44, **`begincast` 6** |

`begincast` and `cast` both appear on the enemy side, so cast **start** and
cast **completion** are separately observable — which is what lets an
interrupted cast be told from a completed one without inference.

**The 14 enemy casts with no `sourceInstance`** are, almost certainly,
single-copy NPCs: Warcraft Logs appears to omit the field when there is only
one instance of a species. That is an inference, not an observation, and it
must not be resolved at ingestion time. **Store the field as NULL when absent
and resolve it during analysis** against the pull's NPC instance range: if a
pull contains exactly one copy of that `gameID`, the null maps to that copy;
if it contains several, the attribution is genuinely unknown and must be
recorded as such rather than defaulted to instance 1.

**Lesson for collection:** an unfiltered event query is dominated by players.
`hostilityType: Enemies` is not an optimisation here, it is what makes the
enemy-side data visible at all.

## 7. Events — VERIFIED

**`EventDataType`** — all 14 values exist, as hypothesised. Every event profile
in `config/sampling.yml` validates. **`HostilityType`**: `Friendlies`,
`Enemies`.

Timestamps are **integer milliseconds relative to report start** (a fight ran
91,929 → 1,749,021, about 27.6 minutes). `log_version` 17, `game_version` 1.

### Observed event shapes

`Report.events(...).data` is raw JSON, so shape is preserved verbatim.

| Category | Event types seen | Notable fields |
| --- | --- | --- |
| Casts | `cast`, `begincast` | `abilityGameID`, `targetInstance`, plus full resource block when `includeResources: true` |
| Debuffs | `applydebuff`, `removedebuff` | `sourceInstance`, `targetID` |
| Buffs | `applybuff`, `refreshbuff`, `removebuff`, `applybuffstack`, `removebuffstack` | `stack`, both instance fields |
| Interrupts | `interrupt`, `applydebuff` | `abilityGameID` (the interrupt), `extraAbilityGameID` (**what was interrupted**), `targetInstance` |
| Dispels | `dispel` | `abilityGameID` (the dispel), `extraAbilityGameID` (**what was removed**), `isBuff` |
| Deaths | `death` | `killerID`, `killerInstance`, `killingAbilityGameID` |
| Summons | `summon` | `targetInstance` |
| DamageTaken | `damage` | see below |

`Interrupts` returns `applydebuff` events alongside `interrupt` — the
interrupt-lockout debuff. Worth knowing before counting rows as interrupts.

### DamageTaken carries more than hoped

`amount`, `absorbed`, `absorb`, `blocked`, `mitigated`, **`unmitigatedAmount`**,
`hitType`, **`isAoE`**, `tick`, `sourceInstance`, `hitPoints`,
**`maxHitPoints`**, `armor`, `attackPower`, `spellPower`, `versatility`,
`avoidance`, `classResources`, `x`, `y`, `facing`, `mapID`, **`buffs`**.

Three of these settle open questions from the brief:

- **`maxHitPoints` is present**, so damage as a fraction of player health *is*
  reconstructible. The brief said not to guess at this; no guessing needed.
- **`buffs`** is a dot-separated list of aura IDs active on the target at the
  moment of the hit (`"384072.386208.132404."`). Defensive uptime can be read
  straight off a damage event rather than reconstructed by correlating aura
  timelines — which makes "was a defensive up for this tankbuster" a direct
  lookup.
- **`unmitigatedAmount` alongside `mitigated`** separates what the mob swung
  for from what the tank actually took. That is the difference between
  measuring danger and measuring mitigation.

### Arguments

28 on `Report.events`, including `sourceInstanceID`, `targetInstanceID`,
`filterExpression`, `sourceAurasPresent`/`Absent`, `useAbilityIDs`,
`useActorIDs`, `wipeCutoff`. Only `hostilityType` is used so far, and only to
make enemy events visible. Server-side filtering stays a Phase-5 optimisation:
it would discard the raw stream this project exists to preserve.

**Page limit:** 25 was requested for the probe; the documented 10,000 ceiling
is still untested.

### Pagination semantics — MEASURED, NOT ASSUMED

The brief forbids guessing this. It was measured, on a full traversal of one
+10 Murder Row fight:

| | |
| --- | --- |
| **Cursor** | **Exclusive** — `nextPageTimestamp` is past the last timestamp of the previous page |
| Boundary events repeated | **0** |
| Pages traversed | 316, at a deliberately tiny 25-event limit |
| Events returned by API | 7,920 |
| Events emitted after de-duplication | **7,920** |
| Boundary repeats dropped | 0 |
| Out-of-order timestamps | 0 |
| Largest gap between events | 17.8 s (downtime between pulls; under the 60 s warning threshold) |
| Warnings | none |

Returned equals emitted: nothing lost, nothing double-counted, across 316
page boundaries.

**The multiset boundary matching is therefore not load-bearing on this
endpoint** — an exclusive cursor never re-sends an event. It stays in place
anyway. It costs nothing when the cursor is exclusive, it is the difference
between correct and corrupt if the behaviour ever changes or differs by event
type, and its cost was one design decision rather than an ongoing tax. The
paginator was built correct under either semantics precisely so this answer
could be a *measurement* rather than a prerequisite.

Still untested: the real page ceiling (25 was used to force many pages), and
whether a single timestamp can hold more events than one page — the one case
that would make pagination genuinely impossible, which the paginator detects
and raises on rather than silently truncating.

## 8. Report discovery — VERIFIED

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

## 11. Observed API cost — MEASURED

| Operation | Requests | Points | Bytes | Seconds |
| --- | --- | --- | --- | --- |
| Full recon incl. 316-page event traversal | 355 | **~29** of 3600/hour | 3.3 MB | 97 |
| One `report_fights` query | 1 | ≤ 2 (incl. two budget reads) | — | — |
| Schema-only recon (no report) | 17 | ~2 | 105 KB | 5 |

**Points are not the binding constraint.** 316 event pages cost roughly 0.05
points each; the hourly budget of 3600 is nowhere near threatened by event
collection. The real limits are **wall-clock time and bandwidth**: 97 seconds
and 3.3 MB for what was still only a sample of one fight.

That reverses the planning assumption. `min_points_reserve: 200` is
over-cautious and can be revisited. What Phase 2 must budget for is runtime and
disk, not quota — which makes the raw cache more valuable, not less, since a
re-parse costs nothing while a re-download costs minutes.

**Not yet measured:** the cost and size of a *complete* event download for one
run at each profile. That is the number that sizes Phase 2.

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

### 12.5 Permission-gated fields, again — FIXED PROPERLY

```
You do not have permission to view the battle tag for this user.
(partial data was returned and discarded)
```

The third run failed on `User.battleTag`, having failed on `User.avatar` the
run before. Adding names to a deny-list one at a time was losing a race
against a type with several gated fields.

**Two compounding faults:**

1. **The strategy was wrong.** Expanding a composite to *every* scalar asks for
   far more than the research needs. A nested object is wanted here only to
   identify something — which zone, which uploader, which region.
2. **The safety net never fired.** `dropped_fields` came back empty, because
   the retry matched the schema spelling `battleTag` against prose that says
   "battle tag". A word-boundary search finds nothing there.

**Fixes:**

- **Identity-first expansion** (`SchemaIntrospector.PREFERRED_LEAF_NAMES`):
  when a type exposes `id`, `name`, `slug` or `compactName`, the expansion
  takes only those. `User` reduces to `{ id name }`, so gated extras are
  unreachable *whatever they are called*. A type with no identity leaves
  (`ReportArchiveStatus`, `ReportMapBoundingBox`) still expands fully, because
  there the state is the point — archived-report detection depends on it.
- **Normalized blame matching**: both sides are reduced to letters and digits,
  so `battleTag` matches "battle tag", "Battle-Tag" and "battletag" alike.
  Names under four characters are still matched strictly, or a normalized `id`
  would match "invalid" and "identity".
- **Most-specific match only**: "compact name" literally contains the word
  `name`, so a naive scan would drop a field the server never objected to.

### 12.6 Cache identity ignored the query text — FIXED

Cached responses were keyed on the logical query *name*, variables and
`query_version`. But queries here are **generated** from introspection, so the
same name can legitimately produce a different document between runs — after a
schema change, or after narrowing a sub-selection as in 12.5. A stale response
answering a different question could be served. The rendered query's hash is
now part of the cache key; `query_version` remains a coarse manual override.

## 13. Open questions

**Gate A is closed.** Five live runs answered: field availability, rate-limit
shape and cost, the season dungeon list, report discovery reach, pull detail,
event shapes, pagination semantics, whether max player HP is reconstructible
(it is), and whether enemy casts carry instance identity (they do).

Remaining, none of them blocking Phase 1:

1. **What does a missing `sourceInstance` mean?** Believed to be "only one copy
   of this NPC exists", but unconfirmed. Handled by storing NULL and resolving
   against the pull's instance range at analysis time, never by defaulting.
2. What is the real event page ceiling? 10,000 is documented, untested.
3. What does a *complete* event download cost per run, per profile? Points are
   demonstrably not the constraint; time and bytes are.
4. Do pull intervals overlap, and do bosses appear as `dungeonPulls`?
5. What share of events falls outside every pull, and what are they?
6. Can enemy health be reconstructed well enough to time an execute phase?
   Player `hitPoints`/`maxHitPoints` are on damage events; enemy casts also
   carry them (an enemy cast showed `hitPoints` 1,432,221 of the same
   `maxHitPoints`), so enemy health may be readable straight off enemy cast
   events. Untested.
7. What does `allowUnlisted` change, and does it stay within v1's public-only
   scope?
8. Is `keystoneLevel` non-null a reliable Mythic+ marker? It held on this
   report (13 of 15 fights, levels 8 and 10), but one report is one report.

---

## 8. Stream benchmark — VERIFIED (10 fights, 10 reports)

Measured by `wclmplus benchmark`, one Mythic+ fight per report. Medians.

| Stream | Events/fight | Exhausted | KB/event | Points | Seconds |
| --- | ---: | :---: | ---: | ---: | ---: |
| `DamageDone` (=@Friendlies) | ≥24,048 | 2/10 | 0.53 | 13.2 | 6.9 |
| `Healing` (=@Friendlies) | ≥24,004 | 4/10 | 0.42 | 13.0 | 6.1 |
| `Threat@Enemies` | 10,090 | 10/10 | 0.38 | 6.5 | 2.5 |
| `DamageDone@Enemies` | 7,204 | 10/10 | 0.40 | 5.0 | 1.9 |
| `Resources@Friendlies` | 7,014 | 10/10 | 0.47 | 5.0 | 1.7 |
| `Threat@Friendlies` | 1,941 | 10/10 | 0.22 | 2.0 | 0.4 |
| `Healing@Enemies` | 956 | 10/10 | 0.21 | 2.0 | 0.4 |
| `CombatantInfo` | 5 | 10/10 | 7.34 | 2.0 | 0.3 |
| `Resources@Enemies` | **0** | 10/10 | — | 2.0 | 0.3 |

`DamageDone` and `Healing` hit the 12-page cap on most fights: those figures are
**lower bounds**, not totals.

### hostilityType defaults to Friendlies. It never means "both".

Confirmed on every stream tested — `Casts`, `Buffs` (earlier), and now
`DamageDone`, `Healing`, `Threat`. In each case an unfiltered request returned
**exactly** the `@Friendlies` count, with the `@Enemies` events absent entirely.
Treat this as a property of the API, not a per-stream quirk: name the hostility
on every stream, always.

`Threat` is the starkest case — its enemy side is roughly five times its
friendly side, so an unfiltered request drops ~84% of the stream.

### `Resources` does not expose NPC energy

Zero events across all ten fights, every probe exhausted. The hope that enemy
resource events would reveal progress toward energy-gated mechanics is not
supported. `Resources@Friendlies` is the whole of this stream.

### `Threat` carries no threat value

No field in the stream names threat. It returns `cast` (134,768 observed),
`death` (2,559) and `applydebuff` (370) events carrying `melee`, `fake` and
`feign` flags. It shows what was swung and when, not who held aggro. Whether
threat numbers exist on another endpoint is untested.

### `CombatantInfo` ignores hostility

All three requests returned byte-identical results on 9 of 10 fights. It is
per-player data that exists once per fight. Never request it split — that
collects the same rows twice.

It is also the best value in the API: five events per fight, ~36 KB, 2 points,
0.3 s, carrying `talents`, `talentTree`, `gear`, `specID`, `auras`, `itemLevel`
and every stat. Everything class-specific depends on it.

### Cost per page

About 1.1 points per page across every stream (a 12-page probe cost 13 points,
a 1-page probe 2). Quota, not wall clock, is the binding constraint: at 34
pages/run the existing `mechanics` profile costs ~37 points/run, so 3,600
points/hour allows ~97 runs/hour while 17.5 s/run would allow ~205.
