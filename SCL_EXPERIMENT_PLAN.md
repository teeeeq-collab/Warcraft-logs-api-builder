# Shifu Combat Language — Experiment Plan

**Status:** experiment design, revision 2. Incorporates the architecture review
(amendments 9-13); see
[`ARCHITECTURE_REVIEW_RESPONSES.md`](ARCHITECTURE_REVIEW_RESPONSES.md). Nothing
is standardized and nothing is built.

Per brief §23 the name `SCL/1` is reserved and must not be used until the gate in
§11 closes. Everything before then is `experimental-<n>`, and every decoder
rejects a version marker it does not recognise.

---

## 1. What is being optimized

Not "make the text smaller". This:

> **maximize useful gameplay information per LLM token while keeping the model's
> comprehension reliable.**

Those diverge. `2F81A01950` is smaller than anything here and is a terrible
representation for a model. A format 15% larger and read correctly beats one 40%
smaller and misread 10% of the time, because a confident wrong answer is worse
than a longer prompt.

---

## 2. Two tracks, not one contest

Revision 1 put Candidate F (state → action → outcome) in the same table as A-E
and compared them on tokens per event. That was a category error: **F does not
encode the same information**, so it has fewer "events" because it answers a
different question, not because it compresses better.

| | Track A — exact / forensic | Track B — semantic gameplay |
|---|---|---|
| **Encodes** | an event timeline, exactly or explicitly lossily | reconstructed state, action, outcome |
| **Answers** | what happened, in order, precisely | what the situation was and what was done about it |
| **Candidates** | A, B, C, D, E, G | F, and later analytical summaries |
| **Measured on** | tokens · reversibility · chronological comprehension · forensic QA accuracy | teaching information per token · comparison quality · pattern recognition · whether a decision can be explained from it |
| **Feeds** | timeline drill-down, representative pulls, forensic review | teaching, cohort comparison, similarity |

Two winners, or none. There is no requirement that one format serve both, and
forcing one would damage both.

---

## 3. The compression hypothesis (Track A)

WoW combat has a large event count and a small vocabulary. Within one pull:
five players fixed for the run; a handful of NPC species; bounded ability sets;
bounded event types.

```json
{"timestamp":18492,"source":"Street Sneak","sourceInstance":2,
 "event":"damage","ability":"Melee","target":"Holy Priest","amount":38127}
```

against, with a legend in scope:

```
+842 E2 a>P1 38127
```

**A hypothesis about token economics, not a conclusion.** A tokenizer may split
`E2` and `a>P1` into more tokens than the English words they replace, and short
symbols can be harder for a model to track. That is what the benchmark decides.

---

## 4. Multi-level dictionaries

Revision 1 had only pull-local dictionaries. That gave a player's abilities new
codes every pull — wasteful, and confusing for a model reading a run.

| Scope | Contents | Stable across |
|---|---|---|
| **dungeon / spec** | NPC species, NPC abilities, per-spec player ability vocabulary | the whole export |
| **run** | the five players, their abilities, their trinkets | the run |
| **pull** | enemy instances `E1..En` | the pull |

**Only enemy instances are genuinely pull-local.** The same species in two pulls
is two different sets of creatures; sharing instance codes between them would
assert continuity that does not exist. Everything else is stable at a wider scope
and is defined once.

Every dictionary entry retains NPC game ID, report actor ID, source instance
where known, and species, so nothing is lost by aliasing.

**Sub-experiment: player aliases.** `P1-P5` (group order) vs `T/H/D1/D2/D3`
(role order). Role aliases carry meaning a model already has priors about, which
may help comprehension more than it costs in tokens — the same hypothesis that
makes hierarchical ability codes attractive, and possibly wrong for the same
reason. Both measured, on tokens *and* comprehension.

**Sub-experiment: ability codes.** `P2:a0=Serenity` vs `P2.00` vs categorised
`P2 H0`. Do not assume hierarchy is cheaper.

**Sub-experiment: item effects.** Player abilities, a separate `G0` namespace,
or ordinary abilities carrying `source=item`? The semantic distinction probably
matters later even where the token cost is identical.

---

## 5. Legend placement and self-contained chunks

This is **not primarily a compression question — it is a retrieval requirement.**
If Shifu is to answer "find historical situations similar to this moment", it
must be able to hand a model *one pull from one run*, decodable on its own. A
format whose chunk 40 cannot be read without chunks 1-39 cannot serve that at
all, however well it compresses.

Variants measured:

1. legend once, at the top;
2. legend repeated every N events;
3. block-local legend (per pull);
4. chunk-local mini-legend (only the symbols that chunk uses).

Variant 4 is expected to win for retrieval and to cost most in total tokens for a
whole-run read. Both facts can be true, which is why both are measured.

**This settles one sub-experiment on correctness rather than metrics.** Repeat
opcodes whose meaning crosses a chunk boundary are **excluded**, not merely
predicted to lose: they break decodability of a retrieved fragment, and that is a
requirement rather than a number to weigh.

---

## 6. Candidates

### Track A

| | Name | Example | Purpose |
|---|---|---|---|
| A | verbose JSON | `{"timestamp":18492,"source":"Street Sneak",...}` | baseline; every ratio is against this |
| B | compact JSON | `{"dt":842,"s":"E2","e":"d","a":"A1","t":"P1","v":38127}` | keeps JSON parseability, drops verbosity |
| C | line symbolic | `+842 E2 D A1>P1 38127` | one line per event, explicit fields |
| D | domain-aware | `@MELEE>P1` header, then `+842 E2 38127` | default suppression under a header |
| E | integer rows | `842,2,1,1,38127` | storage lower bound **only**, not an LLM candidate |
| **G** | **frequency-aware** | shortest codes to the most frequent abilities | amendment 12 |

### Track B

| | Name | Purpose |
|---|---|---|
| F | state / action / outcome | semantic compression; teaching and comparison |

**Candidate G in detail.** Code length assigned by **measured LLM token cost**,
not characters and not bits. The three assumptions the review names — one char =
one token; two digits beat a short word; hierarchical coding is cheaper — are all
plausible, all unverified, and all settleable with a tokenizer in an afternoon.

G's code table is **corpus-derived, so it is versioned and travels with the
export.** Frequency ranks shift as the corpus grows, and a decoder holding an
older table would resolve every symbol to a *valid but wrong* ability — plausible
nonsense rather than an error. The table is content-hashed, the hash sits in the
export header, and a decoder that cannot match it refuses. Same rule as version
rejection, same reason.

---

## 7. Default-value suppression

Revision 1 said "do not suppress merely because something is usually true". That
was aimed at a real danger — suppressing something *unrecoverable* — but attached
to the wrong variable.

**Reversibility is a property of encoding every exception, not of the default
being common.** `@MELEE default_target=T` with 20% exceptions is exactly as
lossless as one with 2%, provided every exception carries its target. Probability
determines savings, not correctness.

The gate, in order:

1. **Is reconstruction unambiguous?** Hard pass/fail. Every exception explicitly
   encoded, decoder rule documented. Nothing else is considered if this fails.
2. **Net token savings**, including exception overhead. A 20% exception rate at
   3 extra tokens each may still beat spelling the target every time — or may
   not. Measure.
3. **Comprehension.** A high exception rate can be lossless and still hurt: the
   model must track "default or not" on every line. This is where a technically
   fine encoding can still lose.
4. **Decoder simplicity.**

Fields worth testing: autoattack target · ability event family · self-targeted
defensives · self-heals · periodic-effect source and target · item-use source ·
standard player role.

**Measure the base rates first**, per dungeon, species, pull and key bracket:
`P(enemy melee target = tank)`, and `P(field = value | actor, ability, event
type)` generally. The base rate feeds step 2; it is not itself the gate.

---

## 8. Time encoding

Absolute first timestamp, deltas after. Expected to be a clear win; measured
anyway. Pull-local sequences may start at `t0`.

For machine storage, integer delta encoding. For model-facing output, readability
wins over density — Level 0 and Level 1 are different problems (§9).

---

## 9. Representation levels

| Level | What | Consumer | Optimize for |
|---|---|---|---|
| 0 | Parquet, numeric IDs, dictionary encoding | the analytical engine | scan speed, size |
| 1 | compact exact events (**Track A**) | forensic review, representative pulls | exactness, then tokens |
| 2 | state → action → outcome (**Track B**) | teaching, comparison, similarity | meaning per token |
| 3 | analytical summaries with evidence depth | most Shifu questions | legibility |

Descent must work with provenance intact:

```
summary → representative states → compact timeline → normalized rows → raw payload
```

That chain is why nothing may be thrown away, and it is the actual product
requirement underneath all of this.

---

## 10. Fixtures and metrics

### Fixture set

**20-50 real pulls**, never synthetic — synthetic data cannot have the redundancy
structure the hypothesis is about. Covering, at least twice each: ordinary trash ·
large pull · boss · duplicate enemy species · heavy autoattack pressure ·
player-targeted mechanic · party AoE · interrupt · dispel · buff · debuff ·
periodic damage · healing · absorb · death · resource-intensive play · proc-heavy
spec · tank · healer · DPS · three key levels · a quiet period · a dense period.

Pseudonymized by the existing `sanitize.py`; each carries run, pull, time window
and the streams collected.

**Constraint:** a fixture from a `mechanics_research` run contains no Healing.
Comparing formats across fixtures with different coverage measures coverage, not
format. Fixtures are grouped by stream set and compared only within a group.

### Size metrics (Track A)

bytes · bytes/event · characters/event · lines/event · **tokens** · legend
overhead · tokens including legend · tokens at 10 / 100 / 1,000 events · ratio vs
Candidate A · reversibility · decode complexity · human readability.

The 10/100/1,000 breakdown exists because legend overhead is fixed and per-event
cost is not: a format that is terrible at ten events can win at a thousand. **The
crossover point is the interesting number**, and it is what decides which format
a retrieved chunk should use versus a whole-run export.

**On tokenization.** If the target model's tokenizer is not available locally,
that is stated in the output, the proxy is named, and the numbers are labelled as
proxy counts. **Token counts are never estimated.** A measurement whose
instrument is unnamed is not a measurement.

### Track B metrics

Tokens are still counted, but they are not the objective. Measured instead:
information relevant to a teaching answer per token; whether two states can be
compared from the representation alone; whether a pattern across many states is
recognisable; whether a decision can be explained from it.

These are partly qualitative. They are recorded as judgements **with the fixture
and the answer attached**, so they can be disputed, rather than as scores.

---

## 11. Comprehension benchmark

Questions whose answers are determinable from the timeline and verifiable from
normalized rows:

- Which enemy hit the tank immediately before ability X?
- How long between the first and second cast of Y?
- Which player had defensive Z active when the damage landed?
- Which target was attacked when E2 stopped attacking the tank?
- What happened immediately after the healer used Apotheosis?
- Which player received the dispel?
- Did the interrupt land before the cast completed?
- Which action sequence occurred inside the party-damage window?

```json
{"question_id":"q0007","fixture":"pull-mr-014","track":"A",
 "question":"Which enemy hit the tank immediately before Shadow Bolt?",
 "expected":{"actor":"E2","game_id":218455,"instance":2},
 "derivable_from":["Casts@Enemies","DamageTaken"],
 "source_events":[8814423,8814431]}
```

`expected` is computed from the database, never written by hand, so the benchmark
cannot drift from the corpus.

**No API integration is built for this.** Fixtures and the expected-answer format
come first; the operator pastes candidate exports into ChatGPT and records
answers. A scored harness can be added later without changing the fixtures.

**The failure this exists to catch:** a model that answers correctly at 10 events
and drifts at 500, having lost a dictionary or an implicit default. Test at all
three scales. A format that only works small is only useful small.

---

## 12. Reversibility

Every Track A candidate needs an encoder and a decoder, and the round trip is a
test:

```
normalized events → encode → decode → normalized events
```

equal on every field the format claims to carry. Where a format is deliberately
lossy, the loss is enumerated in its specification and the round trip asserts
exactly that difference and no other.

**A format whose decoder is not written is not a candidate.** This is the rule
that stops a plausible-looking notation being adopted on the strength of a
hand-written example.

Track B is not reversible by construction — it is a summary — so it asserts
something different: every state, action and outcome traces to its source events,
and the trace is checked.

---

## 13. Standardization gate

`SCL/1` may be declared only when all six hold:

1. candidates exist, with encoders **and** decoders;
2. size and token cost measured with a named tokenizer;
3. reversibility measured, loss enumerated;
4. comprehension fixtures exist and have run at 10, 100 and 1,000 events;
5. **both tracks evaluated separately**, with no assumption of one winner;
6. **the operator has reviewed the trade-offs and chosen.**

Until then: `experimental-<n>`, and every decoder rejects an unrecognised version
rather than guessing. A guessing decoder turns a format mismatch into silently
wrong data — the same failure `run_stream_coverage` was designed to prevent.

---

## 14. Deliverables

```
src/wcl_mplus/scl/
  fixtures.py        select and freeze pulls from the corpus
  dictionaries.py    dungeon / run / pull scopes
  candidates/        encoder + decoder per candidate (A-E, G, F)
  measure.py         size and token metrics
  comprehension.py   question fixtures and expected answers
tests/test_scl.py    round trip per Track A candidate; version rejection;
                     chunk decodes standalone; code-table hash mismatch refused
data/exports/scl/
  SCL_BENCHMARK.md   two tables, one per track
  scl_benchmark.json the numbers
```

The output is **two tables and a recommendation**, not a standard.
