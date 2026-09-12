# Warcraft Logs Mythic+ Research Dataset Collector

A research instrument for building a high-confidence empirical model of what
tanks and healers actually experience in **Midnight Season 2 Mythic+** runs —
pull compositions, mechanic timings, cast frequencies, debuff and dispel
timing, tank and party damage, mechanic overlaps, deaths, and how all of it
changes with pull duration and key level.

It is not a dungeon guide and not a web app. It is a collector plus a dataset
detailed enough that new questions can be answered later **without
re-downloading anything**.

> **Current status: Phase 0 complete — Gate A passed.**
>
> Four live runs against the real API. The fourth completed all 16 checks with
> no failures. Confirmed: every field the research design needs exists;
> **pagination is lossless** (316 pages, 7,920 events in, 7,920 out); **NPC
> instance identity is real** (one pull held 18 copies of a single NPC, and
> enemy events carry the instance marker); **Midnight Season 2 is zone 55**
> with all eight dungeons identified; and **report discovery works
> season-wide**, so sampling is not confined to leaderboard logs.
>
> The data also supports more than the brief assumed: damage events carry
> `maxHitPoints` (damage as a share of player health), `buffs` (which
> defensives were up at the moment of the hit) and `unmitigatedAmount`
> (what the mob swung for, versus what the tank took).
>
> One item open: enemy *cast* instance identity, which one short run settles.
> Then Phase 1 — the database and the Murder Row pilot.

---

## Setup

### 1. Create a Warcraft Logs API client

Go to <https://www.warcraftlogs.com/api/clients/> and create a client. You need
the **v2 Public API** with the OAuth *client credentials* flow. No redirect URL
is required.

Copy the **Client ID** and **Client Secret**.

### 2. Put the credentials in a local `.env`

```bash
cp .env.example .env
```

Open `.env` and paste your two values. `.env` is git-ignored.

> ### Never share your Client Secret
>
> Do **not** paste the Warcraft Logs Client Secret into ChatGPT, Claude,
> GitHub, an issue, a log file, a screenshot, or any project documentation.
> It belongs only in your local `.env`.
>
> If it is ever exposed, revoke and rotate it immediately at
> <https://www.warcraftlogs.com/api/clients/>.
>
> This project is built so that cannot happen by accident: the secret and any
> access token are registered with a redaction layer the moment they load, the
> raw cache refuses to write a file containing a credential, and
> `wclmplus cache-audit` lets you verify a dataset before you share it.

### 3. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Python 3.11 or newer.

### 4. Check it works

```bash
wclmplus auth-check
```

You should see `Authentication OK` and your hourly point budget. The access
token itself is never printed or written to disk.

### 5. Run the reconnaissance

```bash
wclmplus recon --report <PUBLIC_MYTHIC_PLUS_REPORT_CODE>
```

Find a public Mythic+ log on Warcraft Logs and pass either the report code or
the full URL. This is the step that turns assumptions into facts. It will:

- introspect the live GraphQL schema;
- verify every field this project wants, and record the ones that do not exist;
- read the rate-limit budget and measure what a query costs;
- fetch the run, its dungeon pulls, the enemy NPC list and the master data;
- sample every event category and record the real event shape;
- **measure the event pagination semantics instead of assuming them**;
- probe which report-discovery mechanisms the API actually supports;
- save sanitized fixtures with player names pseudonymized.

Then read:

```
data/exports/recon/RECON_REPORT.md      # human summary
data/exports/recon/recon_findings.json  # full evidence
```

### 6. Resolve the dungeon IDs

```bash
wclmplus discover-dungeons          # dry run
wclmplus discover-dungeons --write  # persist what it found
```

No Warcraft Logs zone or encounter ID is hardcoded anywhere in this project.
They are read from the live API and written to
`config/dungeons.discovered.yml`, which is merged over your hand-edited
`config/dungeons.yml` at load time.

---

## Why live verification happens on your machine

Phase 0 requires live API calls, and they **cannot be made from the
environment where this code is written**. Two independent blockers:

1. **No credentials there.** By design, yours never leave your computer.
2. **`warcraftlogs.com` is unreachable there.** The build environment's egress
   proxy denies every connection to it (`CONNECT tunnel failed, response 403`),
   while other hosts resolve normally — network policy, not a transient fault.

So the verification is a command *you* run, and it writes its own evidence
files. That turned out to be a good thing: the first run caught two real bugs
that no amount of offline testing would have found, because both were wrong
assumptions about the live schema.

Every schema claim in `API_NOTES.md` is tagged `VERIFIED`, `CORRECTED` or
`HYPOTHESIS`, with the evidence cited.

### Updating without git

If you downloaded this as a ZIP rather than cloning it, `git pull` won't work.
To update: download the ZIP again, extract it to a new folder, and copy your
existing **`.env`** into it before running setup. Your credentials stay put.

---

## Commands

Nothing here requires you to understand GraphQL.

### Works offline, no credentials

| Command | Purpose |
| --- | --- |
| `wclmplus config-check` | Validate the YAML config; show which dungeon IDs are still unverified |
| `wclmplus report-list-check FILE` | Check a report list for typos before collecting |
| `wclmplus cache-stats` | Raw-cache size and composition |
| `wclmplus cache-audit` | Scan the raw cache for credentials before sharing data |
| `wclmplus version` | Version and provenance stamps |

### Needs credentials

| Command | Purpose |
| --- | --- |
| `wclmplus auth-check` | Verify credentials, read the hourly point budget |
| `wclmplus schema-check` | Introspect the schema and verify every wanted field |
| `wclmplus inspect-report CODE` | Summarize one report and its Mythic+ runs |
| `wclmplus discover-dungeons [--write]` | Resolve dungeon names to live zone/encounter IDs |
| `wclmplus recon [--report CODE]` | Full Phase 0 reconnaissance |

Every command has `--help`.

---

## Report discovery

There is no documented API call meaning *"give me 500 random public +10 Murder
Row logs"*, so discovery is kept separate from ingestion and is pluggable.

`ManualReportSource` always works: a file with one report code or URL per line,
with `#` comments allowed.

```
# pilot sample
https://www.warcraftlogs.com/reports/aBcD1234EfGh5678
BBBBBBBBBBBBBBBB,10,timed,EU
```

API-backed sources (character recent reports, guild reports) are written but
**refuse to run until `wclmplus recon` confirms their schema shape**, rather
than sending a query that might not be valid. Every discovered report records
its provenance — source type, seed, timestamp, rank, page — because sampling
bias cannot be assessed after the fact without it.

---

## Configuration

| File | Contents |
| --- | --- |
| `config/dungeons.yml` | All eight Season 2 dungeons, aliases, validation targets. All IDs start `null` — they come from discovery. |
| `config/dungeons.discovered.yml` | Generated by `discover-dungeons --write`. Merged over the above. |
| `config/sampling.yml` | Key brackets, event profiles, sample profiles, politeness settings |
| `config/hotfix_epochs.yml` | Mechanic-version epochs, so old and current behaviour are never pooled silently |

Sampling and dungeon identity live in config, never in source.

### Event profiles

| Profile | Contents |
| --- | --- |
| `metadata` | No events. Report, run, pulls, NPC composition, roster, master data. |
| `mechanics` | Default. Casts, Debuffs, Buffs, Interrupts, Dispels, Deaths, Summons, DamageTaken. |
| `full` | Adds DamageDone, Healing, Resources. |
| `raw-all` | Everything. Tiny validation samples only until the cost is measured. |

---

## Design commitments

These are the rules the code enforces, not aspirations:

- **The live schema wins.** No field is assumed. Queries are built from
  introspected fields; absences are recorded in the recon report.
- **Raw responses are cached.** Every response is stored as sanitized
  `.json.gz`, so a normalization bug can be fixed and the database rebuilt
  without spending API quota again.
- **Pagination is proven, not guessed.** The paginator is correct whether the
  cursor is inclusive or exclusive, matches boundary events as a *multiset* so
  genuinely identical events are never dropped, verifies progress on every
  page, and checkpoints so an interrupted download resumes exactly.
- **NPC instance identity is preserved.** Two copies of the same NPC species in
  one pull keep separate timelines. Nothing merges them.
- **Nothing is aggregated early.** Millisecond timestamps, actor and target
  identities, instance IDs and raw event payloads are all retained.
- **A tooltip cooldown is not an NPC recast time.** Censoring is respected: a
  mob that dies before recasting is a censored observation, not evidence of a
  long cooldown.
- **Unknowns are recorded, not filled in.** If the API does not expose
  something, it becomes a documented limitation.
- **Player names are not needed.** Class, spec and role are kept; names are
  pseudonymized with a stable hash so cross-run duplicate detection still works.

---

## Tests

```bash
pip install -e ".[dev]"
pytest
```

247 tests, all offline: no credentials, no network. Live smoke tests are
opt-in via `pytest -m live`.

The suite covers secret redaction, OAuth failure modes, GraphQL error
classification, retry and 429 handling, cache identity and the
refuse-to-write-a-secret guard, config parsing, dungeon-ID discovery, report
parsing, and pagination — single page, multi-page, a full 10,000-event page,
many events sharing one timestamp, byte-identical events at one timestamp,
interruption and resume, repeated and backwards cursors, and both cursor
semantics.

`tests/wcl_simulator.py` is a clearly-labelled **synthetic** stand-in used to
exercise control flow offline. It is not API data. Real sanitized fixtures land
in `tests/fixtures/` when you run `wclmplus recon`.

---

## Project documents

| File | Purpose |
| --- | --- |
| `PROJECT_STATE.md` | Current milestone, decisions, blockers, next actions |
| `TASKS.md` | Task IDs, owners, status, acceptance criteria |
| `API_NOTES.md` | Verified API behaviour. **Hypotheses until recon runs.** |
| `DATA_DICTIONARY.md` | Tables, columns, units, null and timestamp semantics |
| `VALIDATION.md` | Mechanic test cases, expected vs observed evidence, limitations |
| `CHANGELOG.md` | Collector, schema and normalization changes |

---

## Handing the dataset to an analyst

When collection phases are complete, the deliverables are `data/db/*.sqlite`
and `data/exports/*.parquet`. Before sharing:

```bash
wclmplus cache-audit
```

Player names are pseudonymized by default, and credentials are structurally
prevented from entering the cache — but verify rather than assume.
