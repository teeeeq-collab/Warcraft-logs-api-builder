# Validation

Mechanic test cases, expected evidence, observed evidence, and limitations.

> **Overall status: machinery validated; game behaviour not yet.**
>
> A live recon run on 2026-09-12 confirmed that every field the research design
> needs exists in the API, and found two defects in this project (see
> `API_NOTES.md` section 12), both fixed. It did **not** reach event data: the
> run aborted at `report_metadata`, so no mechanic has been observed.
>
> 232 offline tests cover pagination completeness, NPC-instance separation,
> secret redaction, cache integrity, error classification and both live
> defects. That proves the collector behaves correctly on well-shaped input.
> It proves nothing about the game.

## How to run validation

```bash
wclmplus recon --report <PUBLIC_MYTHIC_PLUS_REPORT_CODE>
```

Then fill in the **Observed** column of each case below from
`data/exports/recon/recon_findings.json`, and record any discrepancy. A case
whose observation contradicts the hypothesis is a **finding, not a failure** —
the log is the authority, not the dungeon guide.

---

## V1 — Duplicate NPC instance identity

**The most important case.** If it fails, per-mob mechanic timing is
unsupported and every recast statistic in the project is invalid.

| | |
| --- | --- |
| **Goal** | Two copies of one NPC species in one pull keep separate cast timelines. |
| **Expected evidence** | (a) A pull whose `enemyNPCs` instance-ID range covers more than one copy. (b) Events carrying `sourceInstance` so each cast attributes to copy 1 or copy 2. |
| **Observed (a)** | **CONFIRMED AVAILABLE.** All six identity fields exist on `ReportDungeonPullNPC`: `id`, `gameID`, `minimumInstanceID`, `maximumInstanceID`, `minimumInstanceGroupID`, `maximumInstanceGroupID`. |
| **Observed (b)** | **STILL UNVERIFIED.** No event has been fetched. Encouraging sign: `Report.events` accepts `sourceInstanceID` and `targetInstanceID` as **filter arguments**, which implies per-instance data exists — but a filter argument is not proof the field is returned on each event. |
| **Status** | HALF CONFIRMED — (a) yes, (b) pending the next recon run |

Automated support in place:

- Recon reports `duplicate_npc_species_candidates` — pulls usable as this fixture.
- Recon raises a limitation if no sampled event carries `sourceInstance` /
  `targetInstance`.
- `test_fingerprint_distinguishes_npc_instances` proves the paginator never
  conflates two instances.
- `test_duplicate_npc_species_pull_is_detected` proves detection works.

**If (b) fails:** record that per-instance timing is not supported by the API.
Do not infer instance identity from cast ordering — that would fabricate data.

## V2 — Pagination completeness

| | |
| --- | --- |
| **Goal** | Every event downloaded exactly once, across pages and across an interruption. |
| **Expected evidence** | Cursor semantics classified; boundary-repeat count recorded; a full traversal terminating with a de-duplicated total. |
| **Observed** | _pending_ (live). **Offline: PASSED** — 22 tests, both cursor semantics. |
| **Status** | PARTIAL — machinery proven, live behaviour unverified |

Offline cases proven: single page; multi-page; a full 10,000-event page;
exact multiple of the page size; several events sharing one timestamp;
byte-identical events at one timestamp both kept; a timestamp holding more
events than one page raises rather than looping; repeated cursor; backwards
cursor; page ceiling; interruption and resume with no loss or duplication;
transient failure propagating instead of truncating.

---

## V3 — Murder Row: Shivan Punisher

| | |
| --- | --- |
| **Goal** | Reconstruct a trash-mob mechanic timeline. |
| **Hypotheses** | Whirlwind is castable and observable; a sub-50% execute state exists (Demonic Frenzy). |
| **Expected evidence** | Cast events attributable to one NPC instance; first-cast delay; recurrence intervals; completed vs interrupted; mob lifetime; pull duration; tank damage around each cast. |
| **Observed** | _pending_ |
| **Status** | NOT STARTED — blocked on B1 |

**Known risk:** timing the execute phase needs enemy *health* over time. It is
unverified whether the API exposes enemy resources reliably enough. If not,
record that "time spent below 50%" is not measurable and fall back to what is:
cast counts and mob lifetime.

---

## V4 — Ruby Life Pools: Thunderhead, Rolling Thunder

| | |
| --- | --- |
| **Goal** | Reconstruct a multi-target debuff and dispel timeline. |
| **Expected evidence** | Cast timestamp; every affected player; aura application timestamps; first removal and whether it was a dispel or an expiry; associated damage; second removal; delay between removals; overlapping mechanics within ±1/±3/±5s. |
| **Observed** | _pending_ |
| **Status** | NOT STARTED — blocked on B1 |

**Deliberately not encoded:** the target count is *not* hardcoded as two
anywhere. Ability IDs are `null` in `config/dungeons.yml` and must be
discovered empirically. The point of this case is to prove the *generic* event
model reconstructs the mechanic — not to special-case it.

**Required distinction:** an aura removal is not automatically a dispel. A
`dispel` event and a `removedebuff` event mean different things, and conflating
them would corrupt every dispel-delay statistic.

---

## V5 — The Blinding Vale: Lightwarden Ruia

| | |
| --- | --- |
| **Goal** | Boss phase and timing validation. |
| **Expected evidence** | Cast sequence over the fight; phase transitions; whether the structured late phase external guides describe is visible in the log. |
| **Observed** | _pending_ |
| **Status** | NOT STARTED — blocked on B1 |

**Method note:** do not force events to match the guide. If the log disagrees,
investigate and record the discrepancy. The guide is a hypothesis.

---

## V6 — Den of Nalorakk: Hoardmonger

| | |
| --- | --- |
| **Goal** | Health-threshold / phase-change validation. |
| **Expected evidence** | Enemy health transitions correlated with mechanic activations. |
| **Observed** | _pending_ |
| **Status** | NOT STARTED — blocked on B1, and contingent on enemy resource data being exposed |

---

## V7 — Priority interrupt

| | |
| --- | --- |
| **Goal** | Link a cast start to the interrupt that stopped it. |
| **Expected evidence** | A `begincast` and an `interrupt` event sharing ability, target actor **and target instance**, with the interrupt inside the cast window; `extraAbilityGameID` naming the interrupted spell. |
| **Observed** | _pending_ |
| **Status** | NOT STARTED — blocked on B1 |

---

## V8 — CC stop versus interrupt

| | |
| --- | --- |
| **Goal** | Determine whether a crowd-control "stop" can be inferred reliably. |
| **Expected evidence** | A cast start with no completion, a CC application inside the cast window, and no interrupt event. |
| **Observed** | _pending_ |
| **Status** | NOT STARTED — blocked on B1 |

**Inference rules (to be implemented in Phase 1, with confidence scores):**

An incomplete cast has several possible causes and must not be assumed to be a
CC stop:

| Cause | Evidence | Confidence |
| --- | --- | --- |
| True interrupt | `interrupt` event in the cast window | high — raw fact |
| Mob death | mob death before cast completion | high |
| CC stop | CC aura applied in the window, no interrupt event | medium — inferred |
| Target loss / movement / phase / other AI | none of the above | unknown — must stay unknown |

The last row is the important one: an unexplained incomplete cast is recorded
as unexplained. Labelling every one a CC stop would invent data.

---

## V9 — Event-to-pull assignment

| | |
| --- | --- |
| **Goal** | Events map onto WCL's own pull boundaries, and unassigned events are accounted for. |
| **Expected evidence** | Percentage of events assigned; count and character of unassigned events; any overlapping pull intervals; NPC/event mismatches. |
| **Observed** | _pending_ |
| **Status** | NOT STARTED — Phase 1 |

Events outside every pull are **retained**, not dropped, and reported as a
diagnostic.

---

## V10 — Credential safety

| | |
| --- | --- |
| **Goal** | No credential can reach a log, a cache file, an exception or an export. |
| **Observed** | **PASSED, now including a real live run.** The user ran auth, recon and discovery against the live API with real credentials. No credential appeared in terminal output, `recon_findings.json`, `RECON_REPORT.md` or any fixture. Terminal output was safe to paste verbatim into a chat. |
| **Status** | PASSED for the implemented surface |

Proven by: exact-value and pattern redaction; a planted-leak test confirming
the cache audit actually detects a secret rather than always passing; a cache
write containing a credential being refused with nothing reaching disk;
`Authorization` params stripped before storage; token `repr`/`str` never
exposing the value; an error response echoing a token back not leaking it;
recon output files asserted free of the token.

Note the live run's token had a **360-day** lifetime, not the assumed hour.
That makes in-memory-only handling more important, not less — it is never
written to disk.

## Known limitations (current)

1. **No event data has been observed.** Event shape, pagination semantics and
   per-instance identity in events remain unverified. (B1)
2. **No pull data has been observed.** Field availability is confirmed;
   interval contiguity, boss representation and event-assignment rates are not.
3. **The season dungeon list is RESOLVED.** Midnight Season 2 is zone 55 with
   eight encounters, confirmed from the API and by the project owner. Three
   names are reused from earlier seasons and are resolved by two-pass matching,
   never by name alone.
4. **`Report.gameVersion` does not exist.** Unused by the research design.
   **`User.avatar` is permission-gated** and is not available to this client;
   it is excluded from queries.
5. **Enemy health availability is unknown**, which would make execute-phase
   duration (V3) and health-threshold phases (V6) unmeasurable.
6. **Event-page cost is unmeasured.** Schema work costs ~2 points per recon, so
   the budget will be dominated by event pages. No default profile can be
   recommended for large samples yet.
7. **Discovery reach is CONFIRMED BROAD.** `ReportData.reports` answers both
   unscoped and zone-scoped with no guild or user seed, so representative
   season-wide sampling is available rather than leaderboard-only. Note
   `total` is `-1`, so sample size must be counted, not read.
8. **No database exists.** Schema version 0. `DATA_DICTIONARY.md` is a design.
9. **Report-code parsing is permissive** by design: any 8–32 character
   alphanumeric token is accepted. Use `wclmplus report-list-check` first.
10. **No hotfix epochs are declared.** Every run classifies as `unclassified`
    until real dates are known. Absolute run dates are retained so epochs can
    be applied retroactively.

## Unresolved questions

1. Is `keystoneLevel` the reliable marker of a Mythic+ fight?
2. Do events carry `sourceInstance` / `targetInstance` in practice?
3. What is the real event page ceiling, and is the cursor inclusive?
4. Is `hostilityType` required to retrieve enemy casts?
5. Can enemy health be reconstructed well enough to time an execute phase?
6. Do dungeon pull intervals ever overlap, and do bosses appear as pulls?
7. What fraction of events fall outside every pull, and what are they?
8. Which discovery mechanisms return usable report codes?
9. What does a run actually cost in API points, by event profile?
10. Can max player HP be reconstructed robustly enough to express damage as a
    fraction of health? If not, that metric must be dropped rather than guessed.
