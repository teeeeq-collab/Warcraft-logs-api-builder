# Tasks

`TODO` / `DOING` / `DONE` / `BLOCKED`. Owner `coordinator` means work done in
this repository; `user` means a step only the credential holder can perform.

---

## Phase 0 — API reconnaissance

| ID | Description | Owner | Status | Depends on | Acceptance criteria |
| --- | --- | --- | --- | --- | --- |
| P0-1 | Repository scaffold, packaging, git hygiene | coordinator | DONE | — | `.env` git-ignored; `data/` ignored; `pip install -e .` works; `wclmplus --help` runs |
| P0-2 | Canonical project documents | coordinator | DONE | — | All six files exist and describe real state, not aspiration |
| P0-3 | Secret redaction layer | coordinator | DONE | — | Secret cannot appear in logs, exceptions, reprs or cache; proven by tests including a planted-leak test |
| P0-4 | OAuth client-credentials auth | coordinator | DONE | P0-3 | Token fetched, cached, refreshed early; 400/401/403/5xx/non-JSON/network each give an actionable error; token never printed |
| P0-5 | Rate-limit awareness | coordinator | DONE | P0-4 | Budget parsed defensively; reserve enforced; `Retry-After` honoured; unknown shape degrades with a warning |
| P0-6 | GraphQL client with retry classification | coordinator | DONE | P0-4, P0-5 | GraphQL validation errors never retried; 5xx/timeout/429 retried with backoff; partial data rejected |
| P0-7 | Raw cache | coordinator | DONE | P0-3 | Versioned identity incl. page cursor; atomic writes; truncated files tolerated; write containing a credential refused |
| P0-8 | Event paginator | coordinator | DONE | — | Correct under both cursor semantics; no loss or duplication; identical events at one timestamp preserved; stalls raise; resume exact |
| P0-9 | Schema introspection and field verification | coordinator | DONE | P0-6 | Every wanted field checked; queries contain only confirmed fields; renamed types get candidate suggestions |
| P0-10 | Query templates and schema-safe builder | coordinator | DONE | P0-9 | Queries live in `queries/*.graphql`; placeholders filled from verified fields; empty selection refused |
| P0-11 | Report discovery abstraction | coordinator | DONE | P0-6 | `ManualReportSource` works with codes, URLs, CSV, comments; provenance recorded; API sources refuse to run unverified |
| P0-12 | Config layer | coordinator | DONE | — | Dungeons, sampling and epochs parse; no hardcoded IDs; epoch overlaps rejected; config hash stable |
| P0-13 | Dungeon ID discovery | coordinator | DONE | P0-9, P0-12 | Names resolved from live zones; overlay written; ambiguity reported not guessed; authored fields preserved on merge |
| P0-14 | Fixture sanitizer | coordinator | DONE | — | Player and uploader names pseudonymized stably; NPC and ability names kept |
| P0-15 | Recon engine | coordinator | DONE | P0-9…P0-13 | Runs every probe; writes JSON + Markdown even on failure; measures pagination semantics and query cost; records limitations |
| P0-16 | CLI | coordinator | DONE | P0-15 | 10 commands; 5 need no credentials; every command has help; no secrets in output; clear exit codes |
| P0-17 | Offline test suite | coordinator | DONE | all above | 216 tests pass with no credentials and no network; live tests opt-in |
| P0-18 | **Live schema verification** | user | **BLOCKED** | B1 | `wclmplus recon --report <CODE>` completes; `recon_findings.json` written |
| P0-19 | Fold findings into API_NOTES.md | coordinator | BLOCKED | P0-18 | Every hypothesis marked VERIFIED or CORRECTED with evidence |
| P0-20 | Gate A decision | coordinator | BLOCKED | P0-19 | All six Gate A questions answered from evidence |

---

## Phase 1 — Tiny Murder Row pilot (not started)

| ID | Description | Owner | Status | Depends on | Acceptance criteria |
| --- | --- | --- | --- | --- | --- |
| P1-1 | SQLite schema and migrations | coordinator | TODO | P0-20 | Tables for jobs, provenance, reports, runs, players, actors, abilities, pulls, pull NPCs, event pages, events; indexes justified by a real query |
| P1-2 | Report ingestion and run extraction | coordinator | TODO | P1-1 | Keystone metadata stored; non-Mythic+ fights excluded; archived/partial states explicit |
| P1-3 | Pull extraction and event assignment | coordinator | TODO | P1-2 | WCL pull boundaries authoritative; unassigned events retained and counted; overlaps diagnosed |
| P1-4 | NPC instance identity | coordinator | TODO | P1-3 | A pull with two copies of one species yields two separate timelines; proven on a real fixture |
| P1-5 | Event collection with resume | coordinator | TODO | P1-1, P0-8 | Interrupt mid-report and resume; result identical to an uninterrupted run |
| P1-6 | Idempotent re-ingest | coordinator | TODO | P1-5 | Running the same job twice changes no row counts |
| P1-7 | Duplicate-run detection | coordinator | TODO | P1-2 | Multi-field fingerprint; uncertain cases grouped and flagged, never deleted |
| P1-8 | Validation report | coordinator | TODO | P1-1…P1-7 | JSON + Markdown with counts, assignment rates, unknown actors/abilities, pagination warnings, limitations |
| P1-9 | Gate B and Gate C decisions | coordinator | TODO | P1-8 | Both question sets answered from evidence on 5–10 runs |

Phases 2–5 are described in the research brief and are not broken down until
Gate A passes: their design depends on the real event shape.
