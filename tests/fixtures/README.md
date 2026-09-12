# Fixtures

This directory holds **real, sanitized Warcraft Logs API responses**, used by
integration tests.

It is currently empty (apart from this file) because no live API call has been
made — see blocker **B1** in `PROJECT_STATE.md`.

## How it gets populated

```bash
wclmplus recon --report <PUBLIC_MYTHIC_PLUS_REPORT_CODE>
```

Recon writes one JSON file per probe: the schema type list, field verification,
report metadata, fights, dungeon pulls, master data, world zones, and one
sample per event category.

## Sanitization

Before anything is written here, `src/wcl_mplus/sanitize.py` replaces:

- player and pet names with stable pseudonyms (`player-<hash>`)
- the uploader name with `uploader-<hash>`
- report titles and realm names with `redacted-<hash>` / `realm-<hash>`

NPC names, ability names and all IDs, timestamps and instance IDs are **kept** —
they are the research data.

Pseudonyms are stable across reports (fixed non-secret salt), so roster-based
duplicate-run detection still works without storing names.

## What this directory is not

`tests/wcl_simulator.py` is a **synthetic** stand-in used so the offline test
suite can exercise control flow without a network. It is deliberately kept out
of this directory: synthetic data must never be mistaken for evidence about the
real API.
