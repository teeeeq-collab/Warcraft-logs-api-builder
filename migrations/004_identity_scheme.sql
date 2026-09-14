-- ---------------------------------------------------------------------------
-- Identity scheme stability (brief section 4).
--
-- Player names never enter this database in the clear: `normalize_actors`
-- writes `pseudonym(name)`, a salted hash. Every cross-run question about a
-- person -- did this player appear in both uploads, is this the same roster,
-- collect only this player's streams -- is answered by comparing those
-- pseudonyms. So the pseudonym function is part of the data format, not an
-- implementation detail: change the salt or the construction and every stored
-- name silently becomes a different person.
--
-- Nothing in the corpus recorded which scheme produced its names. Two
-- databases written under different schemes would merge without complaint and
-- the roster matching would just quietly stop finding anything.
--
-- `corpus_identity` closes that. It holds exactly one row, written the first
-- time a job runs against the database, stating the scheme the stored names
-- were produced under. A later job under a different scheme fails instead of
-- appending incompatible names.
--
-- The salt is not a secret -- these names are public on Warcraft Logs -- but a
-- fingerprint is stored rather than the salt itself so that comparison is
-- exact and the file stays free of anything that looks like a credential.
-- ---------------------------------------------------------------------------

CREATE TABLE corpus_identity (
    scheme_version    INTEGER PRIMARY KEY,
    salt_fingerprint  TEXT NOT NULL,
    first_written_at  REAL NOT NULL,
    software_version  TEXT NOT NULL,
    notes             TEXT
);

-- Which scheme each job wrote under. Redundant with corpus_identity while a
-- database holds one scheme, and the evidence needed to untangle one that does
-- not -- a corpus assembled by hand from two partitions, say.
ALTER TABLE collection_jobs ADD COLUMN identity_scheme_version INTEGER;

-- Focus-player resolution looks an actor up by its stored pseudonym. Without
-- this the lookup scans `actors`, which is the largest dimension table in the
-- database, once per run.
CREATE INDEX idx_actors_name ON actors (name);
