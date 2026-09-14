# Shifu Combat Language — Experiment Plan

**Status:** experiment design. Nothing is standardized. Nothing is built.

Per brief §23, the name `SCL/1` is reserved and must not be used until the gate
at the end of this document closes. Everything produced before then is versioned
`experimental-<n>` and every decoder rejects a version it does not know.

---

## 1. What is being optimized

Not this:

> make the text smaller.

This:

> **maximize useful gameplay information per LLM token while keeping the model's
> comprehension reliable.**

These are different objectives and they diverge. `2F81A01950` is smaller than
anything proposed here and is a terrible representation for a model. A format
that is 15% larger and read correctly beats one that is 40% smaller and read
wrongly 10% of the time — because a wrong answer delivered confidently is worse
than a longer prompt.

Both halves get measured. Size in §5, comprehension in §6. Neither alone decides.

---

## 2. The hypothesis

WoW combat has a large event count and a small vocabulary. Within one pull:

- five players, fixed for the run;
- a handful of NPC species;
- a bounded ability set per species and per spec;
- a bounded set of event types;
- one dungeon, one key level, one patch.

So the verbose form repeats a great deal of information that a legend could
establish once.

```json
{"timestamp":18492,"source":"Street Sneak","sourceInstance":2,
 "event":"damage","ability":"Melee","target":"Holy Priest","amount":38127}
```

against, with a legend nearby:

```
+842 E2 a>P1 38127
```

**This is a hypothesis about token economics, not a conclusion.** It might be
wrong: a tokenizer may split `E2` and `a>P1` into more tokens than the English
words they replace, and short symbols can be harder for a model to track. That is
what the benchmark is for.

---

## 3. Fixture set (brief §20)

Drawn from the real corpus, never synthesised, because synthetic data cannot
have the redundancy structure the hypothesis is about.

**Target: 20-50 pulls**, selected to cover every case below at least twice:

ordinary trash pull · large pull · boss · duplicate enemy species (two of one
NPC in one pull) · heavy autoattack pressure · a player-targeted enemy mechanic ·
party AoE · interrupt · dispel · buff application · debuff · periodic damage ·
healing · absorb · death · resource-intensive play · proc-heavy spec · tank ·
healer · DPS · at least three key levels · a quiet period · a high-density period.

Fixtures are pseudonymized by the existing `sanitize.py` before being committed,
and each carries provenance: run, pull, time window, streams collected.

**Constraint to respect:** a fixture from a `mechanics_research` run does not
contain Healing. Comparing formats across fixtures with different coverage
measures coverage, not format. Group fixtures by stream set.

---

## 4. Candidate formats

| | Name | Example | Purpose |
|---|---|---|---|
| A | verbose JSON | `{"timestamp":18492,"source":"Street Sneak",...}` | baseline; every ratio is against this |
| B | compact JSON | `{"dt":842,"s":"E2","e":"d","a":"A1","t":"P1","v":38127}` | keeps JSON's parseability, drops its verbosity |
| C | line symbolic | `+842 E2 D A1>P1 38127` | one line per event, explicit fields |
| D | domain-aware | `@MELEE>P1` header, then `+842 E2 38127` | default-suppression under a header |
| E | integer rows | `842,2,1,1,38127` | storage lower bound **only** — not an LLM candidate |
| F | state/action/outcome | `S +18.492 hp=[83,91,44,71,38] ...` / `A Ser>P5` / `O +2.8 stable` | semantic, not event compression |

**Prediction, recorded now so the measurement can contradict it:** F wins for
teaching and comparison questions, C or D wins for forensic timeline questions,
and B is the safest general default. If the data says otherwise, the data wins.

### Sub-experiments inside the candidates

- **Actor dictionaries** (§10). `P1-P5` and `E1..En` per pull. Open question:
  role-ordered (`P1`=tank) or group-ordered? Role-ordered is more legible; group
  order is what the log gives. Test both; a role-ordered legend must still record
  the WCL actor ID, game ID and source instance, so nothing is lost.
- **Ability dictionaries** (§11). `P2:a0=Serenity` vs `P2.00` vs categorised
  `P2 H0`. The brief warns against assuming hierarchy tokenizes better. It may
  cost more, not less.
- **Item/trinket effects** (§12). Are they player abilities, a separate `G0`
  namespace, or ordinary abilities with `source=item` metadata? The semantic
  distinction probably matters later even if the token cost is identical.
- **Autoattack default suppression** (§13). Measure `P(enemy melee target = tank)`
  by dungeon, species, pull, key bracket. Suppress only if overwhelming, and
  **only where reconstruction stays unambiguous**.
- **General default suppression** (§14). For each field, measure
  `P(field=value | actor, ability, event type)`. Suppress only where the omitted
  value is recoverable by a documented rule. Usually-true is not a licence.
- **Time deltas** (§15). Absolute first, deltas after. Expected to be a clear win.
- **Repeat/template opcodes** (§16). Expected to *lose*: it trades tokens for
  hidden model state. Measured so the rejection is evidence-based.
- **Event-family separation** (§17). Does `@ENEMY_MELEE` / `@PLAYER_ACTION`
  grouping beat one chronological stream? It should help size and may hurt
  temporal reasoning. Both get measured; §6 is what decides.

---

## 5. Size metrics (brief §21)

Per candidate, per fixture:

bytes · bytes/event · characters/event · lines/event · **tokens** · legend
overhead in tokens · tokens including legend · tokens at 10 / 100 / 1,000 events
· ratio vs Candidate A · reversibility (exact / lossy / lossy-documented) ·
decode complexity (rules a reader must hold) · human readability (1-5, stated as
a judgement, not a measurement).

**On tokenization.** The target model's exact tokenizer may not be available
locally. If it is not, that is stated in the output, the proxy is named, and the
numbers are labelled as proxy counts. **Token counts are never estimated and
never invented.** A measurement whose instrument is unnamed is not a measurement.

The 10 / 100 / 1,000 breakdown exists because legend overhead is fixed and
per-event cost is not: a format that is terrible for ten events can be the best
for a thousand. The crossover point is the interesting number.

---

## 6. Comprehension benchmark (brief §22)

The smallest format does not win by default. It wins only if the model still
reads it correctly.

A fixture set of questions whose answers are **determinable from the timeline
alone** and verifiable from normalized rows:

- Which enemy hit the tank immediately before ability X?
- How long between the first and second cast of Y?
- Which player had defensive Z active when the damage landed?
- Which target was attacked when E2 stopped attacking the tank?
- What happened immediately after the healer used Apotheosis?
- Which player received the dispel?
- Did the interrupt land before the cast completed?
- Which cooldown was available before the death? *(only where derivable)*
- Which action sequence occurred inside the party-damage window?

Format:

```json
{"question_id":"q0007","fixture":"pull-mr-014",
 "question":"Which enemy hit the tank immediately before Shadow Bolt?",
 "expected":{"actor":"E2","game_id":218455,"instance":2},
 "derivable_from":["Casts@Enemies","DamageTaken"],
 "source_events":[8814423,8814431]}
```

`expected` is computed from the database, not written by hand, so the benchmark
cannot drift from the corpus.

**No OpenAI API integration is built for this** (§22). Fixtures and the
expected-answer format come first; the operator can paste candidate exports into
ChatGPT and record answers manually. A scored harness can be added later without
changing the fixtures.

**Watch for the failure this benchmark exists to catch:** a model that answers
correctly on 10 events and drifts at 500, because it lost track of a dictionary
or an implicit default. Test at all three scales, not just the small one.

---

## 7. Reversibility

Every candidate needs an encoder and a decoder, and the round-trip is a test:

```
normalized events → encode → decode → normalized events
```

must be **equal on every field the format claims to carry**. Where a format is
deliberately lossy — suppressed defaults, dropped precision — the loss is
enumerated in the format's own specification and the round-trip asserts exactly
that difference and no other.

A format whose decoder is not written is not a candidate. This is the rule that
stops a plausible-looking notation from being adopted on the strength of a
hand-written example.

---

## 8. Representation levels (brief §19)

One format will not serve every purpose.

| Level | What | Consumer | Optimize for |
|---|---|---|---|
| 0 | Parquet, numeric IDs, dictionary encoding | the analytical engine | scan speed, size |
| 1 | compact exact events | forensic review, representative pulls | exactness, then tokens |
| 2 | state → action → outcome | teaching, comparison, similarity | meaning per token |
| 3 | analytical summaries (N, distributions, percentiles) | most Shifu questions | legibility |

The LLM should be able to descend:

```
summary → representative states → compact timeline → normalized rows → raw payload
```

with provenance intact at every step. That chain is the reason nothing may be
thrown away (§58), and it is the actual product requirement behind all of this.

---

## 9. Standardization gate (brief §23)

`SCL/1` may be declared only when all five hold:

1. candidates exist, with working encoders **and** decoders;
2. size and token cost are measured with a named tokenizer;
3. reversibility is measured, and any loss is enumerated;
4. comprehension fixtures exist and have been run at 10, 100 and 1,000 events;
5. **the operator has reviewed the trade-offs and chosen.**

Until then: `experimental-<n>`, and every decoder rejects a version marker it
does not recognise rather than guessing. A guessing decoder turns a format
mismatch into silently wrong data, which is the same failure this project
designed `run_stream_coverage` to prevent.

---

## 10. Deliverables

```
src/wcl_mplus/scl/
  fixtures.py     select and freeze pulls from the corpus
  candidates/     one encoder + decoder per candidate
  measure.py      size and token metrics
  comprehension.py  question fixtures and expected answers
tests/test_scl.py   round-trip per candidate; version rejection
data/exports/scl/
  SCL_BENCHMARK.md   the table
  scl_benchmark.json the numbers
```

The output of this phase is **a table and a recommendation**, not a standard.
