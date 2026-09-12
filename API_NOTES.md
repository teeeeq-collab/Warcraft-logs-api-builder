# API notes

Verified Warcraft Logs v2 API behaviour.

> ## ⚠️ NOTHING IN THIS FILE IS VERIFIED YET
>
> Live verification could not be performed where this code was written: no
> credentials were available, and network egress to `warcraftlogs.com` was
> denied by the environment's egress proxy (`CONNECT tunnel failed, response
> 403`, while control hosts returned 200 in the same test). See blocker **B1**
> in `PROJECT_STATE.md`.
>
> Every entry below is therefore a **HYPOTHESIS** — mostly taken from the
> research brief, which the brief itself says must not be trusted for field
> names. Each carries a status tag:
>
> - `HYPOTHESIS` — believed, never observed
> - `VERIFIED` — confirmed by a recon run, with evidence cited
> - `CORRECTED` — observed to differ from the hypothesis
>
> To fill this file in:
>
> ```bash
> wclmplus recon --report <PUBLIC_MYTHIC_PLUS_REPORT_CODE>
> ```
>
> then transcribe `data/exports/recon/recon_findings.json` into the sections
> below, changing each tag and citing the step that proved it. Until then, no
> analysis conclusion may rest on anything here.

---

## 1. Authentication

**Status: HYPOTHESIS**

- Token endpoint: `https://www.warcraftlogs.com/oauth/token`
- Grant: `client_credentials`
- Credentials sent as HTTP Basic auth, not in the body
- API endpoint: `https://www.warcraftlogs.com/api/v2/client`
- Bearer token in `Authorization`

Implemented in `src/wcl_mplus/auth.py`. Verify with `wclmplus auth-check`.

**Open questions:** actual token lifetime; whether `expires_in` is always
present; whether any scope parameter is required.

---

## 2. Rate limiting

**Status: HYPOTHESIS**

Believed to be a points-per-hour budget exposed on a `rateLimitData` object:

| Concept | Candidate field name |
| --- | --- |
| Points available per hour | `limitPerHour` |
| Points spent this hour | `pointsSpentThisHour` |
| Seconds until reset | `pointsResetIn` |

`src/wcl_mplus/ratelimit.py` matches these **case- and underscore-insensitively
against a candidate list** and logs a warning naming the keys actually present
if none matches, so a rename degrades loudly instead of silently reporting a
zero budget.

**Open questions:** real point cost per query shape; whether cost scales with
event page size; whether HTTP 429 is used at all, and whether it sends
`Retry-After`.

Cost measurement is implemented (`GraphQLClient.measure_query_cost`) and
reports an **upper bound**, because it includes the two budget reads used to
bracket the measurement.

---

## 3. Report and fight fields

**Status: HYPOTHESIS**

The wish-lists live in `src/wcl_mplus/querybuild.py` (`WANTED_REPORT_FIELDS`,
`WANTED_FIGHT_FIELDS`, `WANTED_PULL_FIELDS`, `WANTED_PULL_NPC_FIELDS`) and are
checked field by field at runtime. Queries are then generated from the subset
that exists, so this project can never send a query naming an absent field.

Mythic+ fields the research design depends on:

`keystoneLevel`, `keystoneAffixes`, `keystoneBonus`, `keystoneTime`, `rating`,
`countReached`, `countRequired`, `averageItemLevel`, `friendlyPlayers`,
`dungeonPulls`, `gameZone`, `npcCountMap`, `startTime`, `endTime`

**Critical open question:** is `keystoneLevel` really the reliable marker of a
Mythic+ fight? `wclmplus inspect-report` and the `fights` recon step both
assume it and report how many fights carried it.

---

## 4. Dungeon pulls

**Status: HYPOTHESIS**

Believed: `ReportFight.dungeonPulls` yields `ReportDungeonPull` with pull id,
name, start/end time, encounter ID, kill state, x/y, bounding box, maps, and an
`enemyNPCs` list of `ReportDungeonPullNPC`.

WCL's own pull boundaries are treated as authoritative. No combat-gap detector
is implemented, by design (brief section 18).

**Open questions:** are pull intervals ever overlapping or non-contiguous; do
boss encounters appear as pulls; what fraction of events fall outside every
pull.

---

## 5. NPC instance identity — the single most important unknown

**Status: HYPOTHESIS**

Believed fields on `ReportDungeonPullNPC`: `id` (report actor ID), `gameID`
(NPC species), `minimumInstanceID`, `maximumInstanceID`,
`minimumInstanceGroupID`, `maximumInstanceGroupID`.

**Why this matters most:** if two copies of one caster in a pull cannot be told
apart, their cast timelines merge and every recast-interval statistic becomes
wrong — a fabricated "recast" that is really two mobs casting once each.

Two separate things must hold, and only the first is checked so far:

1. **Pull-level identity** — the instance ID range shows more than one copy.
   The recon `dungeon_pulls` step reports pulls where this is true, as
   `duplicate_npc_species_candidates`.
2. **Event-level identity** — individual events carry `sourceInstance` /
   `targetInstance` so a cast can be attributed to copy 1 vs copy 2. The recon
   `event_samples` step records whether any sampled event has these fields and
   raises a limitation if none does.

If (2) fails, per-instance mechanic timing is **not supported by the API** and
that must be recorded rather than worked around.

---

## 6. Events

**Status: HYPOTHESIS**

Believed: `Report.events(...)` returns an object with `data` (a JSON array of
raw events) and `nextPageTimestamp`. Because `data` is JSON rather than a typed
selection set, event shape is preserved verbatim — which is what a research
dataset needs.

Believed `EventDataType` values: `All`, `Buffs`, `Casts`, `CombatantInfo`,
`DamageDone`, `DamageTaken`, `Deaths`, `Debuffs`, `Dispels`, `Healing`,
`Interrupts`, `Resources`, `Summons`, `Threat`. The recon `field_verification`
step records the real enum and flags any configured value the API rejects.

**Open questions:** the real page-size ceiling (10,000 is the documented figure
but unconfirmed); whether `hostilityType` is needed to get enemy casts;
per-event-type field sets; whether timestamps are always report-relative
milliseconds.

---

## 7. Pagination semantics — MEASURED, NOT ASSUMED

**Status: HYPOTHESIS, with the measurement automated**

The brief forbids guessing this. The recon `pagination_probe` step:

1. requests one page with a deliberately tiny limit, to force several pages;
2. records the last timestamp of page 1 and the returned `nextPageTimestamp`;
3. fetches page 2 and compares its head against page 1's tail;
4. classifies the cursor as **inclusive**, **exclusive**, or **unexpected**;
5. counts how many boundary events were actually re-sent;
6. runs a full traversal with the real paginator and reports the de-duplicated
   event total plus diagnostics.

**The collector does not depend on the answer.** `src/wcl_mplus/paginate.py` is
correct either way: events at the previous page's final timestamp are matched
against what was already emitted at that exact timestamp as a **multiset**, so
a re-sent copy is dropped while a genuinely identical second event is kept.
Progress is verified on every page; a cursor that does not advance, or a page
consisting entirely of boundary repeats, raises rather than looping or
silently truncating.

One real API limitation is detected explicitly: if a single timestamp holds
more events than one page can carry, pagination cannot make progress and
`PaginationStall` is raised with that explanation.

---

## 8. Report discovery

**Status: HYPOTHESIS**

There is **no** documented query meaning "give me N random public logs matching
these filters". Discovery is therefore abstracted (`src/wcl_mplus/reportsource.py`).

The recon `discovery_paths` step probes for, and records the arguments of:

`ReportData.reports`, `ReportData.report`, `Character.recentReports`,
`Guild.attendance`, `GuildData.guild`, `WorldData.zones`, `WorldData.encounter`,
`Zone.encounters`, `Encounter.characterRankings`, `Encounter.fightRankings`.

Presence of a rankings field does **not** mean it returns report codes — the
recorded return type must be inspected before relying on it.

`ManualReportSource` is always available and needs no API support at all, so
the collector remains usable whatever the probe finds. No HTML scraping and no
undocumented endpoints are used.

---

## 9. Zones and encounters

**Status: HYPOTHESIS**

Believed: `worldData.zones` yields zones with `id`, `name`, `encounters`,
`difficulties`, `partitions`, `expansion`.

No zone or encounter ID is hardcoded. `wclmplus discover-dungeons` resolves the
configured dungeon names against the live list and writes
`config/dungeons.discovered.yml`. Two zones sharing a name are reported as
ambiguous rather than resolved arbitrarily.

**Open question:** the eight Midnight Season 2 dungeons. Four are named in the
brief; the rest are unknown and deliberately not guessed.

---

## 10. Access limitations

**Status: HYPOTHESIS**

Handled in code, unverified in behaviour:

- **Private reports** — out of scope for v1 (public API, client credentials
  only). HTTP 403 is reported with that explanation and never retried.
- **Archived reports** — `archiveStatus` is requested if present; an archived
  report raises a limitation, because it may have no event data. Such a run
  must never enter an event-frequency denominator.
- **Deleted or wrong codes** — a null report is reported explicitly, not
  treated as an empty result.

---

## 11. Observed API cost

**Status: NOT MEASURED**

To be filled from the recon `query_cost` and `api_usage` steps. Needed before
choosing a default event profile for large samples, and before Phase 2.

| Query | Points (upper bound) | Bytes | Notes |
| --- | --- | --- | --- |
| _pending recon run_ | | | |
