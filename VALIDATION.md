# Validation

Mechanic test cases, expected evidence, observed evidence, and limitations.

> **Overall status: the instrument is validated; the game is next.**
>
> Four live runs completed Phase 0. The fourth ran all 16 steps with no
> failures, confirming that every event shape the research design needs exists
> and that pagination is lossless. **No mechanic has been measured yet** — that
> is Phase 1 onward — but nothing now blocks it.
>
> 247 offline tests cover pagination, NPC-instance separation, secret
> redaction, cache integrity, error classification and all seven defects the
> live runs exposed.

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
| **Goal** | Two copies of one NPC species in one pull keep separate timelines. |
| **Observed (a) pull level** | **CONFIRMED.** All six identity fields populate. One +10 Murder Row run yielded 20 duplicate-species pulls, the largest being **18 copies** of NPC 236085 in a single pull. |
| **Observed (b) event level** | **CONFIRMED for Debuffs, DamageTaken, Interrupts, Buffs, Deaths, Summons.** Enemy actors carry `sourceInstance` (e.g. enemy 11 instance 2 applying a debuff); interrupts carry `targetInstance`; deaths carry `killerInstance`. |
| **Observed (c) enemy casts** | **CONFIRMED.** `hostilityType: Enemies` on the same fight returned 11 distinct NPC actors with **36 of 50** events carrying `sourceInstance`, and both `cast` and `begincast`. The unfiltered sample had returned 5 players and 0 instance markers. |
| **Status** | **PASSED** |

**Open nuance:** 14 of 50 enemy casts carried no `sourceInstance`, almost
certainly because only one copy of that NPC existed. That is an inference. It
is handled by storing NULL and resolving against the pull's instance range at
analysis time — if the pull holds one copy of that `gameID` the null maps to
it; if it holds several, the attribution is unknown and must be recorded as
unknown rather than defaulted to instance 1.

## V2 — Pagination completeness

| | |
| --- | --- |
| **Goal** | Every event downloaded exactly once, across pages and an interruption. |
| **Observed** | **PASSED LIVE.** Cursor is **exclusive**. 316 pages at a 25-event limit: 7,920 events returned, **7,920 emitted**, 0 boundary repeats dropped, 0 out-of-order timestamps, largest inter-event gap 17.8 s (pull downtime), no warnings. |
| **Status** | PASSED |

Offline cases also proven: single page; multi-page; a full 10,000-event page;
exact multiple of page size; several events sharing one timestamp;
byte-identical events at one timestamp both kept; a timestamp holding more
events than one page raises rather than looping; repeated cursor; backwards
cursor; page ceiling; interruption and resume with no loss or duplication;
transient failure propagating instead of truncating.

Because the live cursor is exclusive, the multiset boundary matching never
fires in practice. It stays: it costs nothing here, and it is what keeps the
collector correct if the behaviour differs by event type or changes later.

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

1. **A missing `sourceInstance` is not yet interpretable with certainty.**
   Believed to mean "only one copy exists". Stored as NULL and resolved against
   the pull's instance range at analysis time, never defaulted.
2. **No mechanic has been measured.** Phase 1 onward.
3. **`Report.gameVersion` does not exist** (unused). **`User.avatar` and
   `User.battleTag` are permission-gated** and are not requested.
4. **`ReportPagination.total` is `-1`** — not a usable denominator. Sample size
   must be counted from what is fetched.
5. **The real event page ceiling is untested.** 25 was used to force many pages.
6. **Complete per-run event cost is unmeasured.** Points are demonstrably not
   the constraint (~29 of 3600 for a full recon); time and bytes are.
7. **Enemy health reconstruction is untested.** Player `hitPoints`/
   `maxHitPoints` are on damage events; whether enemies expose the same as
   damage *targets* needs a `DamageDone` sample.
8. **Pull-interval behaviour is unmeasured**: overlaps, boss representation,
   and the share of events falling outside every pull.
9. **No database exists.** Schema version 0.
10. **Report-code parsing is permissive** by design; use
    `wclmplus report-list-check` first.
11. **No hotfix epochs are declared.** Every run classifies as `unclassified`
    until real dates are known; absolute dates are retained so epochs can be
    applied retroactively.

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
