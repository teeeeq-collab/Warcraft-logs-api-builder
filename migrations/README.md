# Migrations

Empty by design. Database schema version is **0**: no tables exist yet.

Storage is Phase 1 work, deferred until `wclmplus recon` establishes the real
event field names. See `DATA_DICTIONARY.md` for the proposed design and
blocker **B1** in `PROJECT_STATE.md` for why it is not implemented.

Each migration will be a numbered SQL file (`001_initial.sql`, ...) applied in
order and recorded in a `schema_migrations` table, so a database can always
state which schema version produced it.
