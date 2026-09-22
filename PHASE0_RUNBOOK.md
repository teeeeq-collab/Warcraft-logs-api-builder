# Phase 0 runbook

Everything in this file runs on **your** machine. The build environment has no
Warcraft Logs credentials and no route to the API — by design, since your Client
Secret never leaves your computer — so these are the steps I cannot run for you.

Run them in order. Each says what it proves and what to send back.

---

## 0. Update

```powershell
powershell -ExecutionPolicy Bypass -File "$HOME\wcl\update-windows.ps1"
```

Then confirm the version:

```powershell
.venv\Scripts\wclmplus.exe version
```

Expect `schema_version: 5`. Two migrations apply on the next command that opens
the database (004 identity scheme, 005 stream narrowing). Both are additive —
no existing row changes, and no re-collection is needed.

---

## 1. Deduplicate the corpus

**This has never been run.** Until it does, every `N` is an upper bound: two
uploads of one real run count twice.

```powershell
.venv\Scripts\wclmplus.exe dedupe
```

Nothing is deleted. Duplicates are grouped and one member of each group is
marked canonical; the rest stay as evidence about upload behaviour.

---

## 2. Regenerate validation

```powershell
.venv\Scripts\wclmplus.exe validate
```

Writes `data\exports\validation\`. Three things in it are new:

- **Overlapping pull intervals** — the two warnings, now classified as
  `touching`, `nested` or `partial`, with a count of how many events actually
  fall inside each contested window. See §5 below for how to read it.
- **Dedupe coverage** — should now say `current` rather than `never_run`.
- **Worked seconds per run** — real collection time, not the calendar span that
  previously reported 5.5 hours for a run re-collected the next day.

**Send me `validation.json`.**

---

## 3. Find your packs

```powershell
.venv\Scripts\wclmplus.exe packs --dungeon "Murder Row"
```

The query moved out of the CLI into the repository; the output is unchanged.

---

## 4. The focus-filter benchmark

This decides whether the `reference_player` profile is worth collecting at all,
and it is the one measurement blocking that redesign. It is cheap — roughly
24 requests, a few points.

Pick **one** report that you personally appear in, put its code in a file, then:

```powershell
.venv\Scripts\wclmplus.exe focus-benchmark --report-list one-report.txt --focus-player "YourCharacterName"
```

The name is matched through the same pseudonym function the corpus uses, so type
your real character name. A realm suffix is fine.

It writes `data\exports\benchmark\FOCUS_BENCHMARK.md` and `focus_benchmark.json`.
**Send me the JSON.** It answers:

| Question | Why it matters |
|---|---|
| Does `sourceID` reduce **points**, or only rows? | Rate limiting is per page. If the server filters after paging, narrowing saves disk and not quota — and the profile's whole premise changes. |
| Does the API accept `sourceID` and `targetID` together? | Unverified. Nothing has ever sent both. |
| What does `Buffs` + `sourceID` return? | Buffs the player *applied* — not buffs *on* them. The config currently uses one word for both. |
| What does `Healing` + `targetID` return? | Healing *received*, which is how "this player was being kept alive" separates from "this player was fine". |
| Do `DamageTaken`+target and `DamageDone`+target overlap? | If they agree, a focus profile needs only the cheaper one. |

**Do not start a large `reference_player` collection until this has run.** As
configured, that profile collects `DamageDone` and `Healing` party-wide while
calling itself a focus profile — `DamageDone` alone is 89,703 events per run.

---

## 5. Reading the overlapping-pull section

| Kind | What it means | Does it matter |
|---|---|---|
| `touching` | The next pull starts on the exact millisecond the previous one ends. Pull intervals are closed at both ends, so one tick belongs to two pulls. | **No.** A boundary convention. The assigner resolves it deterministically to the later pull. |
| `nested` | One pull lies entirely inside another. | Only if events fall in the window. |
| `partial` | Genuine overlap — the tank engaged the next pack while the last was alive. | Only if events fall in the window. The report counts them. |

The `events_in_contested_windows` figure settles it. Zero means nothing is
affected regardless of how the intervals look.

---

## 6. Optional: the compression benchmark

Runs entirely on your corpus, no API calls:

```powershell
.venv\Scripts\wclmplus.exe scl-benchmark --dungeon "Murder Row"
```

Six encodings measured on real pulls, each proved reversible by its own decoder.

**One caveat that is in the report itself:** without a tokenizer installed the
figures are labelled `symbols`, not tokens, and are comparable only to each
other. To get real counts:

```powershell
.venv\Scripts\pip.exe install tiktoken
```

Even then they are a proxy — tiktoken is an OpenAI tokenizer. The report says so.

---

## 7. Still blocked, and staying blocked

`config\hotfix_epochs.yml` is empty. It needs real Blizzard patch dates, which I
will not invent. Until it is filled every run sits in one undifferentiated
epoch, so a mechanic changed mid-season reads as one noisy distribution instead
of two clean ones.

If you can find the dates for the current season, the format is in the file's
comments and it is a few minutes of typing.
