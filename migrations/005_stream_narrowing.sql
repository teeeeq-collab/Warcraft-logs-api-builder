-- ---------------------------------------------------------------------------
-- event_pages must identify a stream the same way everything else does.
--
-- `run_stream_coverage` keys a stream on
--     (run_id, data_type, hostility, source_id, target_id)
-- because a stream narrowed to one actor answers a different question from the
-- same stream unnarrowed. `event_pages` did not carry the narrowing at all, so
-- to the pagination layer those two streams were one stream. Three failures
-- follow, and none of them are loud:
--
--   1. RESUME. `_resume_point` finds the last page for
--      (run_id, data_type, hostility) and continues from its cursor. A focus
--      stream would resume from the party-wide stream's cursor -- skipping
--      events, or seeing a finished stream and collecting nothing at all.
--
--   2. COVERAGE. `_record_coverage` counts pages and events with the same key,
--      so both coverage rows would report the SUM of both streams. The manifest
--      built to stop silent miscounting would itself be miscounting.
--
--   3. PROVENANCE. `events.page_id` is the only link from a row back to the
--      request that fetched it. Without the narrowing on the page, a
--      focus-narrowed subset cannot be told from the party-wide superset it sits
--      inside -- which makes "buffs the focus player applied" and "buffs active
--      on the focus player" unrecoverable after the fact.
--
-- No shipped profile triggers this today: `reference_player` is the only profile
-- with focus streams and its two lists do not intersect. The planned redesign
-- (adding Healing+targetID as a focus stream while Healing@Friendlies stays
-- party-wide) intersects them immediately, so this lands first.
--
-- Existing rows get NULL, which is what they are: unnarrowed.
-- ---------------------------------------------------------------------------

ALTER TABLE event_pages ADD COLUMN source_id INTEGER;
ALTER TABLE event_pages ADD COLUMN target_id INTEGER;

-- Resume and coverage both look up the newest page of one stream. With the
-- narrowing in the key this index serves both without a scan.
CREATE INDEX idx_event_pages_stream
    ON event_pages (
        run_id, data_type, IFNULL(hostility, ''),
        IFNULL(source_id, -1), IFNULL(target_id, -1), page_index
    );
