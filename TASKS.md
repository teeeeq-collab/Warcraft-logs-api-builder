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

## Phase 1 — Tiny Murder Row pilot

| ID | Description | Owner | Status | Acceptance criteria |
| --- | --- | --- | --- | --- |
| P1-1 | SQLite schema and migrations | coordinator | DONE | 14 tables against observed fields; migration runner applies and records versions; foreign keys enforced |
| P1-2 | Normalizer | coordinator | DONE | Real event shapes map to rows; four timestamp bases; players pseudonymized; unmodelled fields preserved in `extra` and reported |
| P1-3 | Report and run ingestion | coordinator | DONE | Keystone metadata stored; non-Mythic+ fights excluded; archived and missing reports recorded as explicit states |
| P1-4 | Pull extraction and event assignment | coordinator | DONE | WCL boundaries authoritative; events outside every pull retained and counted; overlapping intervals reported |
| P1-5 | NPC instance identity | coordinator | DONE | Two copies of one species produce separate timelines; proven by query and by the validation report |
| P1-6 | Event collection with resume | coordinator | DONE | Interrupt mid-stream, resume, land on identical data with no duplicated pages |
| P1-7 | Idempotent re-ingest | coordinator | DONE | Re-running a job changes no row counts; `--refresh` forces a rebuild |
| P1-8 | Duplicate-run detection | coordinator | DONE | Multi-field fingerprint; transitive grouping; nothing deleted; one canonical member per group |
| P1-9 | Validation report | coordinator | DONE | JSON + Markdown with counts, assignment rate, unknown actors/abilities, diagnostics, limitations, and a reconstructed per-copy timeline |
| P1-10 | CLI | coordinator | DONE | `collect` (with `--dry-run`, `--refresh`, `--dungeon`), `dedupe`, `validate`, `stats` |
| P1-11 | **Pilot collection on real reports** | user | **BLOCKED** | 5-10 Murder Row runs ingested; validation report produced from live data |
| P1-12 | Gate C decision | coordinator | BLOCKED | Normalization stable, resume reliable, known mechanics visible, dedupe plausible, volume manageable |

Phases 2-5 are described in the research brief and are not broken down until
Gate C passes: their shape depends on what the pilot measures.
