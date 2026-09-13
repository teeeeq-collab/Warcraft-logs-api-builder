-- ---------------------------------------------------------------------------
-- Cross-log pack identity.
--
-- The central research question is "what does THIS pack do", which means
-- recognising the same pack across hundreds of separate logs. Two handles
-- already existed: `pulls.composition_signature` (exact, counts included) and
-- `pull_npcs.npc_game_id` (find every pull containing one NPC). Both are
-- indexed and both work.
--
-- What was missing is the handle that survives how the pull was made. An exact
-- signature treats "236085x3" and "236085x4" as different packs, so the same
-- trash group splits across as many signatures as there are ways a tank can
-- grab it -- exactly the case the corpus exists to study. `species_signature`
-- drops the counts and keeps the set of NPC types, so one pack stays one pack
-- whether three or five of something came along.
--
-- Both are kept. The exact signature answers "how was it pulled", the species
-- signature answers "which pack is it". Collapsing to one would lose a
-- question, and the counts are the oversized-pull signal.
--
-- `dungeon_key` is denormalised from dungeon_runs onto pulls. It is derivable
-- by join, but every cross-log pack query filters by dungeon first, and at
-- hundreds of millions of events that join is the difference between an index
-- seek and a scan.
-- ---------------------------------------------------------------------------

ALTER TABLE pulls ADD COLUMN species_signature TEXT;
ALTER TABLE pulls ADD COLUMN dungeon_key TEXT;

-- The cross-log pack lookup: one dungeon, one pack, every occurrence.
CREATE INDEX idx_pulls_species    ON pulls (dungeon_key, species_signature);
CREATE INDEX idx_pulls_dungeon    ON pulls (dungeon_key, is_boss, pull_index);

-- "Every pull anywhere that contained this miniboss", without touching pulls.
CREATE INDEX idx_pull_npcs_lookup ON pull_npcs (npc_game_id, pull_id);

-- No new event indexes: migration 001 already carries (ability_game_id, type)
-- for the cross-log ability lookup and (pull_id, rel_ms) for walking a pull's
-- timeline. Both were verified present rather than assumed, after this
-- migration first tried to create them again and failed loudly -- which is the
-- migration runner doing its job.
