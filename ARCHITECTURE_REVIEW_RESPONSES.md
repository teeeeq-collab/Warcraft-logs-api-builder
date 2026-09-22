# Responses to the architecture review

One response per amendment: **agree**, **agree with a refinement**, or
**disagree**, with the technical reason. Where a refinement is proposed, the
four design documents carry the refined version, not the original.

Summary: **16 agree, 1 agree-with-a-correction-to-my-own-error, 0 disagree.**
Six amendments found real defects in the package. Three of those were mine to
begin with and are corrected here rather than quietly patched.

| # | Amendment | Response |
|---|---|---|
| 1 | Layer 1/2 storage boundary | agree + refinement (split build normalization from build derivation) |
| 2 | Split build into dimensions | **agree — this was a design error** |
| 3 | Expand observability metadata | agree + refinement (staleness horizons; payload-cost mitigation) |
| 4 | Multiple outcome horizons | agree + refinement (truncation must be explicit) |
| 5 | Immutable cohort evaluations | agree + refinement (store the source set, not the state set) |
| 6 | Pseudoreplication | **agree — and I retract "statistically meaningless"** |
| 7 | Dedupe policies | agree + refinement (no default policy at the research boundary) |
| 8 | Revisit `reference_player` | **agree — the profile contradicted its own purpose** |
| 9 | Two SCL tracks | agree |
| 10 | Multi-level dictionaries | agree |
| 11 | Chunk-local legends | agree + note (this is a retrieval requirement, not a size one) |
| 12 | Frequency-aware shorthand | agree + refinement (the code table must be versioned and travel) |
| 13 | Default-suppression gate | **agree — my gate conflated two different tests** |
| 14 | Pack decomposition | agree + note (N-hungry; named as a goal, not scheduled) |
| 15 | Relax byte-identical Parquet | **agree — my requirement was brittle and wrong** |
| 16 | Export-scoped pseudonyms | agree + note (the export salt must not be the project salt) |
| 17 | Phase ordering | agree |

---

## 1. Layer 1 / Layer 2 storage boundary — agree, with a refinement

The contradiction is real and it is mine. I wrote "Layer 2 reads Layer 1 and
never writes to it" and then put four derived tables in the ingest database.
Those cannot both be true.

**Adopted:** a second database file, `data/analytics/<corpus>.analysis.sqlite`,
with its own migration directory (`migrations/analytics/`), its own
`ANALYTICS_SCHEMA_VERSION`, and its own lifecycle. Deleting it costs nothing but
compute. Later it is projected to Parquet/DuckDB; the boundary does not move
when that happens, which is the point of drawing it now.

**A constraint the review should know about:** SQLite cannot enforce foreign
keys across attached databases. A `gameplay_states.run_id` in the analytical
store therefore cannot reference `dungeon_runs.run_id` in the ingest store. That
is not a reason to keep them in one file — it is a reason to replace the FK with
something stronger for this purpose: every analytical table records the
**corpus fingerprint** it was derived from (§5), and a `verify` pass checks
referential integrity against the ingest database on demand. An FK guarantees
"this run exists"; a fingerprint guarantees "this run exists *and has not been
recollected since this row was derived*", which is the failure that would
actually corrupt a conclusion.

**Refinement to the preferred direction.** The review suggests build snapshots
"can plausibly remain in Layer 1". Half of that is right and the split matters.
The test I propose for which layer anything belongs in:

> Could a future version of this code change what an existing row *means*,
> without any new data from the API?

- **Mapping CombatantInfo's fields into columns** — `specID`, `itemLevel`, the
  gear array, the talent payload — is the same class of operation as
  `normalize_event`. It is transcription. **Layer 1**, migration 005, table
  `combatant_info`.
- **Deciding what constitutes a "build"** — which fields enter an identity
  hash, how gear is canonicalised, whether two loadouts are the same — is a
  model. It will change. **Layer 2.**

Putting the whole thing in Layer 1 would mean a build-model revision requires a
migration against the canonical corpus, which is exactly the coupling the layer
boundary exists to prevent. Putting the whole thing in Layer 2 would mean the
raw CombatantInfo stays trapped in `events.extra` as JSON, which is the defect
the audit found in the first place.

---

## 2. Split "player build" into independent dimensions — agree; this was a design error

The review is right and the failure mode is worse than "fragments cohorts". A
composite hash over talents *and* gear makes **item level a talent variable**.
Two Mistweavers running identical talents and identical hero talents, one at
ilvl 681 and one at 684, get different `build_id`s. A cohort query for "Apex
Mistweavers" over a few hundred runs would return a scatter of one-member
builds and could report `N=1` for a configuration that dozens of players ran.

That is not a performance problem. It is a correctness problem: it would produce
confident statistics with the wrong denominator.

**Adopted:** five independent dimensions plus an optional composite.

```
talent_loadouts      talent_hash       spec + talent tree, nothing else
hero_talents         hero_talent_id    external; status-tagged, never guessed
equipment_snapshots  equipment_hash    items + enchants + gems, item level EXCLUDED from the hash
trinket_configs      trinket_config_id the unordered pair
stat_snapshots       stat_hash         secondaries, bucketed
run_player_config    config_id         the composite, referencing all five
```

Each question in the review becomes one join on one dimension:

| Question | Dimension |
|---|---|
| Apex vs no-Apex | `hero_talent_id` |
| talent build A vs B | `talent_hash` |
| trinket X vs Y | `trinket_config_id` |
| similar gear | `equipment_hash`, or item level as a range filter |
| same talents regardless of equipment | `talent_hash` alone |

**Two details worth stating.** Item level is a *column*, never part of the
equipment hash — it is continuous and would fragment equipment identity the same
way it would have fragmented builds. And trinkets are an **unordered** pair:
which one sits in slot 13 versus 14 is arbitrary, and treating it as meaningful
would split one configuration into two.

**A limitation the review did not raise.** CombatantInfo is a snapshot taken at
fight start. Gear swapped mid-dungeon is invisible. Every equipment and stat
snapshot is therefore "as at pull 1", and the schema says so rather than
implying the configuration held for the whole run.

---

## 3. Expand observability metadata — agree, with a refinement

The review is right that four tags are insufficient, and the example is exactly
the failure: HP observed 40 ms ago and HP observed 8 s ago are both "observed"
under my design, and only one of them is worth anything.

**Adopted** — per field: `value`, `status`, `observed_at_ms`, `age_ms`,
`method`, `model_version`, `confidence`.

**Refinement: recording age is not enough.** A consumer that is handed
`age_ms: 8300` can still ignore it, and eventually one will. So staleness gets a
**horizon per field kind**, in configuration rather than in code:

```yaml
hp:              2000     # HP moves constantly
resource:        3000
aura_active:     null     # auras have explicit apply/remove; age is not staleness
position:        1500
enemies_alive:   null     # derived from deaths; exact between them
build:           null     # constant within a run
```

Past its horizon, a field's status degrades from `observed` to `stale`
automatically, carrying the age. `null` means the field does not go stale, which
is a real category — an aura's state is known exactly between its apply and
remove events, and calling that "stale" after two seconds would be wrong in the
other direction.

**A cost the review should weigh.** Seven metadata keys × ~30 state fields is
roughly 200 extra JSON keys per state. For a state table that is acceptable; for
an LLM export it is not — it would dwarf the data. Two mitigations, both in the
data model: fields that are constant within a run (spec, build, key level,
dungeon) carry a single block-level tag rather than a per-field record, and the
exporter emits metadata only for fields whose status is not `observed`-and-fresh.
Exceptions get described; the ordinary case stays quiet.

---

## 4. Multiple outcome horizons — agree, with a refinement

Straightforwardly right. `state_id` alone as the primary key was a mistake: it
forces a single window to be chosen before anyone knows which window answers the
question, and "did the player survive" has different answers at +1 s and +10 s.

**Adopted:** primary key `(state_id, horizon_id)`, with both fixed windows
(`+1s`, `+3s`, `+5s`, `+10s`) and semantic ones (`mechanic_window`, `pull_end`).

**Refinement:** a horizon that runs past the end of available data must be
marked, not silently shortened. A state 2 s before the pull ends has no +10 s
outcome — it has a **truncated** +10 s outcome, and the distinction decides
whether it belongs in a distribution. `truncated` and `actual_window_ms` are
columns, and any aggregate over a horizon reports how many of its members were
truncated. Without that, "deaths within 10 s" is systematically understated for
every state near a pull boundary — a bias that points the same direction every
time, which is the kind that survives review.

---

## 5. Immutable cohort evaluations — agree, with a refinement

The distinction is right and the package was missing it. "Who matches now?" and
"who produced this published number?" are different questions and only the
second is reproducible.

**Adopted:** `cohort_evaluations` + `cohort_evaluation_members`, carrying the
definition version, corpus fingerprint, evaluation timestamp, every analytical
model version in force, and the exclusions with their reasons.

**Refinement: store the source set, not the derived set.** An evaluation records
`(run_id, actor_id)` pairs — tens of thousands of rows at worst, which is
nothing. It must **not** record the state IDs: at millions of states per cohort
that becomes larger than the analysis it documents, and it is redundant, because
states are deterministically re-derivable from the source set given the model
versions the evaluation already records. The source set plus the versions *is*
the reproducible identity.

The corpus fingerprint is **content-derived**, per `ANALYTICAL_DATA_MODEL.md`
§6a. An earlier draft of this response proposed hashing only
`(run_id, normalizer_version, coverage_status)`; a follow-up review correctly
rejected that as insufficient, since it proves the labels unchanged while the
evidence underneath could differ. The digest now covers raw-cache page identity,
page counts and statuses, cursors and the coverage manifest — with a `deep`
grade adding a hash of every normalized event row. Changing the evidence
necessarily changes the fingerprint.

---

## 6. Pseudoreplication — agree, and I retract an overstatement

This is the most important amendment in the set, and the review is also right to
push back on how I framed the sample size.

**I wrote that numbers from 94 runs would be "statistically meaningless". That
was wrong, and wrong in a way that would have hidden real evidence.** Evidence
quality depends on the claim and on which unit is independent for that claim.
Some claims are well supported by the current corpus today:

| Claim | Independent unit | Current corpus |
|---|---|---|
| "This NPC casts ability X" | one observation | **supported now** |
| "Its recast interval is 2.97-4.93 s" | NPC instance | **supported now** — 12 independent copies |
| "This pack appears at this route position" | pull | **likely supported** |
| "This mechanic targets non-tanks 30% of the time" | cast | supported for common abilities |
| "Holy Priests press Apotheosis in this window" | **player** | not supported — too few distinct players |
| "Apex outperforms non-Apex" | player, matched | not supported |

The pattern is that **enemy-behaviour claims are cheap and player-behaviour
claims are expensive**, because the independent unit for the first is an NPC
instance (hundreds per run) and for the second is a person (at most five, and
often the same people across a report).

**Adopted:** every statistic carries a multi-level evidence block —
`n_events`, `n_states`, `n_pulls`, `n_runs`, `n_players`, `n_reports`,
`n_parties` — plus a **concentration** measure: the largest share contributed by
any one player and by any one run. A distribution where one player supplies 60%
of the observations is not a population distribution, and printing `N=100,000`
beside it would be a lie of composition rather than of arithmetic.

No global minimum run count. Each claim class declares which unit is independent
for it, and the reported N for that claim is the count of *that* unit. Where an
analysis pools non-independent observations, it says so and reports both counts.

---

## 7. Dedupe policies — agree, with a refinement

Right, and it closes a hole I left open: I made "canonical by default" a
repository rule but never said what to do when nothing has been classified,
which is the actual state of the live corpus.

**Adopted:** `permissive` (browsing, debugging, retrieval — `NULL` allowed and
reported) and `strict` (statistics — every included run must carry a dedupe
classification; otherwise the result is marked `provisional` and
publication-quality export refuses).

**Refinement: no default at the research boundary.** `permissive` as a default
means a statistics path silently gets the lax policy; `strict` as a default
means an interactive query fails for no good reason. So the policy is a
**required argument** on every analytical entry point and a default only on the
browsing ones. Requiring the caller to say which kind of question they are
asking is cheap; guessing it for them is how an undeduplicated corpus ends up
underneath a published number.

---

## 8. Revisit `reference_player` — agree; the profile contradicts its own purpose

The review is right, and this is worth stating plainly: **the profile as
configured is not a focus-player profile.** It collects `DamageDone` and
`Healing` party-wide *and* defines focus streams, so the focus narrowing saves
nothing on the two most expensive streams in the API. `DamageDone` alone is
89,703 events/run — 1.65× the entire current corpus, per run.

I chose that deliberately (decision D13: collecting one player of five means any
question about the other four needs the run re-fetched) and I still think the
reasoning is sound. But it is the reasoning for a *different profile*. Applying
it inside `reference_player` produced a profile that is `mechanics_research`
plus everything, wearing a name that promises the opposite.

**Adopted:** the reasoning splits into two profiles instead of contradicting
itself inside one.

- `mechanics_research` — party-wide, ask-anything-later, expensive per run,
  which is the right trade when the corpus must answer questions nobody has
  thought of yet.
- `reference_player` — rich environment and party *context*, maximal telemetry
  for **one** player. Party-wide `DamageDone`/`Healing` leave it.

**Measurement first, per the review's own instruction.** The benchmark must
answer, against the live API, before the profile is finalised:

| Question | Why it is not answerable from here |
|---|---|
| Does `sourceID` narrowing actually reduce points, or only rows? | Cost is per page; a narrowed stream may cost the same if the server filters after paging |
| Does the API accept `sourceID` and `targetID` together? | Unverified. The query template now carries both, but no live request has used both |
| What does `Buffs` + `sourceID` return? | **Buffs the focus player *applied*** — not buffs *on* them. Those need `targetID`. The two are different data and the profile currently assumes one word covers both |
| What does `Healing` + `targetID` return? | Healing *received* — needed to answer "was this player being kept alive by someone else" |
| Is `DamageTaken` + `targetID` equivalent to `DamageDone` + `targetID`? | Unverified; they are different `EventDataType` values that may overlap |

That last group is why `focus_event_types` cannot stay a flat list. A focus
stream needs to say *which* narrowing it wants, and a healer's useful focus set
is not a DPS's. **Role-specific focus sets** are proposed, gated on the
benchmark.

Bookkeeping cost of all this: zero. `run_stream_coverage`'s unique index is
already `(run_id, data_type, hostility, source_id, target_id)`, so a
source-narrowed stream and a target-narrowed one are already distinct rows with
distinct coverage. The manifest was built for this.

---

## 9. Two SCL tracks — agree

Correct, and it fixes a category error in my plan. Candidate F does not encode
the same information as A-E, so a token-per-event comparison between them is
meaningless — F has fewer "events" because it is answering a different question,
not because it compresses better.

Track A (exact/forensic) is measured on tokens, reversibility, chronological
comprehension and forensic QA accuracy. Track B (semantic) is measured on
teaching information per token, comparison quality, pattern recognition, and
whether a decision can be explained from it. Two winners, or none.

---

## 10. Multi-level dictionaries — agree

This improves on what I wrote; my plan only had pull-local dictionaries, and the
review is right that giving a player's abilities new codes every pull is both
wasteful and confusing. Three scopes, tested independently:

| Scope | Contents | Stable across |
|---|---|---|
| dungeon/spec | NPC species, NPC abilities, spec ability vocabulary | the whole export |
| run | the five players, their abilities, their trinkets | the run |
| pull | enemy instances `E1..En` | the pull |

Only enemy *instances* are genuinely pull-local — the same species in two pulls
is two different sets of creatures and must not share instance codes. Everything
else is stable at a wider scope and should be defined once.

`T/H/D1/D2/D3` versus `P1-P5` gets benchmarked on both axes. Role aliases carry
meaning a model already has priors about, which may help comprehension more than
it costs in tokens — but that is a hypothesis, and the same hypothesis that
makes hierarchical ability codes attractive and possibly wrong.

---

## 11. Dictionary refresh and self-contained chunks — agree, with a note

Adopted: legend-once, legend-every-N, block-local and chunk-local variants, all
measured.

**The note is that this is not primarily a compression question.** It is a
retrieval requirement. If Shifu is to answer "find historical situations similar
to this moment" (§37 of the brief) it must be able to hand an LLM *one pull from
one run*, decodable on its own. A format whose chunk 40 cannot be read without
chunks 1-39 cannot serve that at all, regardless of how well it compresses.

That reframing settles a sub-experiment I had left open: **repeat opcodes whose
meaning crosses a chunk boundary are excluded on correctness grounds**, not
merely predicted to lose on tokens. They break decodability of a retrieved
fragment, which is a requirement rather than a metric.

---

## 12. Frequency-aware shorthand — agree, with a refinement

Adopted as Candidate G: the most frequent abilities and templates receive the
shortest codes, with **measured LLM token cost** as the assignment criterion —
not characters, not bits. The three assumptions the review names (one char = one
token; two digits beat a short word; hierarchical coding is cheaper) are all
plausible and all unverified, and a tokenizer will settle them in an afternoon.

**Refinement: the code table is corpus-derived, so it must be versioned and
travel with the export.** Frequency ranks shift as the corpus grows; a decoder
holding last month's table would silently misread this month's file — every
symbol resolving to a *valid but wrong* ability, which produces plausible
nonsense rather than an error. So the table is content-hashed, the hash is in
the export header, and a decoder that cannot match it refuses. This is the same
rule as SCL version rejection and for the same reason: a format mismatch must
never degrade into quietly wrong data.

---

## 13. Default-value suppression — agree; my gate was wrong

The review is technically correct and my §14 conflated two independent tests.

Reversibility is a property of **encoding every exception**, not of the default
being common. `@MELEE default_target=T` with 20% exceptions is exactly as
lossless as one with 2% exceptions, provided every exception carries its target.
My "do not suppress merely because something is usually true" was aimed at a real
danger — suppressing something *unrecoverable* — but I attached it to the wrong
variable. Probability determines **savings**, not correctness.

**Adopted gate**, in order:

1. **Reconstruction unambiguous?** Hard pass/fail. Every exception explicitly
   encoded, decoder rule documented. Nothing else is considered if this fails.
2. **Net token savings**, including exception overhead. A 20% exception rate
   where each exception costs 3 extra tokens may still win over spelling the
   target every time — or may not. Measure.
3. **Comprehension.** A high exception rate can be lossless and still hurt, since
   the model must track "default or not" on every line. This is where a
   *technically fine* encoding can still lose.
4. **Decoder simplicity.**

---

## 14. Pack decomposition — agree, with a note

Preserved as an explicit analytical goal. It is the right target: "the tank
combined pack A with pack B" is the question players actually ask, and "this is a
fuzzy variant of archetype X" is a weaker restatement of it.

**The note is about what it costs.** Decomposition is a latent-structure problem
— observed pulls are unions of unobserved packs, and recovering the parts means
finding species sets that recur together across many independent pulls. That
needs the *combinations* to vary, which needs many routes from many groups. It is
among the most N-hungry things in the package, more so than most cohort
questions, because it needs variation in how packs are combined rather than
repetition of one combination.

So: named as a goal, designed for (the archetype tables carry a `components`
column from the start so decomposition does not require a migration later), and
scheduled for nothing.

---

## 15. Relax byte-identical Parquet — agree; my requirement was brittle

The review is right and my gate P4 would have caused real damage. Parquet files
embed the writer's library version, compression codec settings, row-group
boundaries and dictionary page layout. A byte-identical requirement pins
`pyarrow` forever: a routine dependency bump would fail a correctness gate
without a single row having changed. That trains people to ignore the gate, which
is worse than not having one.

**Adopted:** determinism is asserted on semantics, not bytes.

- identical logical rows;
- identical ordering, under a declared total order;
- identical partitioning;
- identical source manifest;
- identical **logical fingerprint** — a hash over canonically serialised, sorted,
  explicitly-typed rows, computed independently of the file format.

The fingerprint is what the test compares, and it is stable across writer
versions because it never sees the writer. Byte-identity is kept as a *warning*
under a pinned environment, since a byte change with an unchanged fingerprint is
worth noticing, and worth noticing is not the same as worth failing.

---

## 16. Export-scoped pseudonyms — agree, with a note

Adopted as an interface, no implementation. The distinction is right: the
internal pseudonym is stable *by design* so that longitudinal analysis works, and
that same stability is exactly what makes it unsafe to publish — two snapshots
sharing an ID are trivially correlated.

**The note matters for how it must be built.** The export pseudonym cannot be
derived from `IDENTITY_SALT`, because that salt is public in this repository —
anyone could recompute the mapping. It must be `HMAC(random_per_export_salt,
internal_pseudonym)`, with the salt generated at export time and **not included
in the export**. Whether the operator retains the salt privately is then a real
choice with a real consequence: keep it and you can re-identify your own
snapshot later; discard it and the mapping is gone for everyone including you.
The interface records which was done.

---

## 17. Phase ordering — agree

Adopted. P1 and P2 run in parallel — they share no files and no schema. Build
work follows the query boundary so it has somewhere to be read from. Collection
runs throughout. Nothing waits on the SCL standard, which the two-track split
(§9) makes easier: Track B's representation is downstream of the state engine,
so freezing an event notation was never on the critical path anyway.
