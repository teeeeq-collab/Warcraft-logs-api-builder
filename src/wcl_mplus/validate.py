"""Validation reporting.

After every milestone the project produces machine-readable evidence and a
readable summary (brief section 48). Two rules shape what goes in:

* **What could not be done is reported next to what could.** A report that only
  lists successes cannot be used to judge whether the data is trustworthy.
* **Claims are demonstrated, not asserted.** The report reconstructs a real
  per-NPC-copy mechanic timeline out of the database, because "instance
  identity is preserved" means nothing until a timeline actually comes back
  with two separate copies in it.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from .collect import EVENTS_PAGE_LIMIT
from .db import Database
from .version import provenance


def _rows(db: Database, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(sql, params)]


def collect_validation(db: Database, *, dungeon_key: str | None = None) -> dict[str, Any]:
    """Gather the evidence for one validation report."""
    where = "WHERE dungeon_key = ?" if dungeon_key else ""
    params: tuple[Any, ...] = (dungeon_key,) if dungeon_key else ()

    counts = db.table_counts()
    runs = _rows(
        db,
        f"SELECT collection_status, COUNT(*) AS n FROM dungeon_runs {where} "
        "GROUP BY collection_status",
        params,
    )
    by_bracket = _rows(
        db,
        f"SELECT key_bracket, COUNT(*) AS runs, "
        "       ROUND(AVG(duration_ms) / 1000.0) AS mean_duration_s, "
        "       MIN(keystone_level) AS min_key, MAX(keystone_level) AS max_key "
        f"  FROM dungeon_runs {where} GROUP BY key_bracket ORDER BY key_bracket",
        params,
    )
    by_epoch = _rows(
        db,
        f"SELECT hotfix_epoch, COUNT(*) AS runs FROM dungeon_runs {where} GROUP BY hotfix_epoch",
        params,
    )

    total_events = db.scalar("SELECT COUNT(*) FROM events") or 0
    assigned = db.scalar("SELECT COUNT(*) FROM events WHERE pull_id IS NOT NULL") or 0

    # An event whose actor or ability is unknown means master data and the
    # event stream disagree -- worth knowing before trusting any name.
    unknown_actors = (
        db.scalar(
            "SELECT COUNT(*) FROM events e LEFT JOIN actors a "
            "  ON a.report_code = e.report_code AND a.actor_id = e.source_id "
            " WHERE e.source_id IS NOT NULL AND e.source_id >= 0 AND a.actor_id IS NULL"
        )
        or 0
    )
    unknown_abilities = (
        db.scalar(
            "SELECT COUNT(*) FROM events e LEFT JOIN abilities ab "
            "  ON ab.game_id = e.ability_game_id "
            " WHERE e.ability_game_id IS NOT NULL AND e.ability_game_id > 0 "
            "   AND ab.game_id IS NULL"
        )
        or 0
    )

    duplicate_groups = _rows(
        db,
        "SELECT duplicate_group_id, COUNT(*) AS members FROM dungeon_runs "
        " WHERE duplicate_group_id IS NOT NULL GROUP BY duplicate_group_id "
        "HAVING members > 1",
    )

    diagnostics = _rows(
        db,
        "SELECT kind, severity, COUNT(*) AS n FROM ingest_diagnostics "
        "GROUP BY kind, severity ORDER BY n DESC",
    )

    return {
        "generated_at": time.time(),
        "provenance": provenance(),
        "dungeon_filter": dungeon_key,
        "database": {
            "path": str(db.path),
            "size_bytes": db.size_bytes(),
            "table_counts": counts,
        },
        "runs": {
            "by_collection_status": runs,
            "by_key_bracket": by_bracket,
            "by_hotfix_epoch": by_epoch,
            "date_span": _date_span(db, where, params),
            "analysable": db.scalar(
                f"SELECT COUNT(*) FROM dungeon_runs {where or 'WHERE 1=1'} "
                "AND collection_status = 'complete' AND IFNULL(is_canonical, 1) = 1",
                params,
            ),
        },
        "events": {
            "total": total_events,
            "assigned_to_pulls": assigned,
            "unassigned": total_events - assigned,
            "assigned_pct": (round(100 * assigned / total_events, 2) if total_events else None),
            "by_data_type": _rows(
                db,
                "SELECT data_type, hostility, COUNT(*) AS n FROM events "
                "GROUP BY data_type, hostility ORDER BY n DESC",
            ),
            "unknown_source_actors": unknown_actors,
            "unknown_abilities": unknown_abilities,
        },
        "pages": {
            "total": db.scalar("SELECT COUNT(*) FROM event_pages") or 0,
            "failed": db.scalar("SELECT COUNT(*) FROM event_pages WHERE status != 'ok'") or 0,
            "incomplete_streams": db.scalar(
                "SELECT COUNT(*) FROM (SELECT run_id, data_type, MAX(page_index) AS last "
                "  FROM event_pages WHERE status='ok' GROUP BY run_id, data_type) s "
                " JOIN event_pages p ON p.run_id = s.run_id AND p.data_type = s.data_type "
                "   AND p.page_index = s.last "
                " WHERE p.next_cursor_ms IS NOT NULL"
            )
            or 0,
        },
        "cost": cost_profile(db),
        "dedupe": dedupe_coverage(db),
        "paired_abilities": paired_abilities(db),
        "stream_coverage": stream_coverage(db),
        "duplicates": {
            "groups": len(duplicate_groups),
            "runs_in_groups": sum(int(g["members"]) for g in duplicate_groups),
            "detail": duplicate_groups[:20],
        },
        "diagnostics": diagnostics,
        "npc_instance_evidence": npc_instance_evidence(db),
        "limitations": known_limitations(db),
    }


def stream_coverage(db: Database) -> dict[str, Any]:
    """Which event streams each run actually holds, observed rather than declared.

    A corpus grown over months will outlive the config that built it. Collect
    200 runs at full fidelity, switch to a reduced profile for the next 2,000,
    and every buff statistic pooled across the two is wrong for a reason nothing
    in the events themselves reveals -- the missing rows look exactly like rows
    that never happened.

    The profile name a job recorded is an assertion about intent. The set of
    streams a run actually carries is evidence, and it survives re-collection,
    config edits and interrupted jobs, none of which the label does. So this
    groups runs by the streams they hold and reports every distinct shape.
    """
    # Read from the manifest, not from event_pages. A stream that was requested
    # and genuinely returned nothing writes no pages, so inferring coverage from
    # pages reports it as never collected -- turning a real zero into a gap and
    # a gap into a zero, in whichever direction happens to mislead.
    rows = _rows(
        db,
        "SELECT run_id, data_type, IFNULL(hostility, '') AS hostility, "
        "       scope, source_id, target_id, status "
        "  FROM run_stream_coverage",
    )
    per_run: dict[str, set[str]] = {}
    incomplete: list[dict[str, Any]] = []
    for row in rows:
        label = row["data_type"] + (f"@{row['hostility']}" if row["hostility"] else "")
        if row["source_id"] is not None or row["target_id"] is not None:
            label += " (focus)"
        per_run.setdefault(row["run_id"], set()).add(label)
        if row["status"] != "ok":
            incomplete.append({"run_id": row["run_id"], "stream": label, "status": row["status"]})

    if not per_run:
        return {
            "shapes": [],
            "distinct_shapes": 0,
            "streams_seen": [],
            "incomplete_streams": [],
            "manifest_rows": 0,
        }

    widest = max(per_run.values(), key=len)
    shapes: dict[tuple[str, ...], list[str]] = {}
    for run_id, streams in per_run.items():
        shapes.setdefault(tuple(sorted(streams)), []).append(run_id)

    return {
        "distinct_shapes": len(shapes),
        "manifest_rows": len(rows),
        "incomplete_streams": incomplete[:50],
        "streams_seen": sorted({s for streams in per_run.values() for s in streams}),
        "shapes": sorted(
            (
                {
                    "streams": list(streams),
                    "runs": len(members),
                    "missing_vs_widest": sorted(widest - set(streams)),
                    "example_run": sorted(members)[0],
                }
                for streams, members in shapes.items()
            ),
            key=lambda s: int(s["runs"]),
            reverse=True,
        ),
    }


def paired_abilities(
    db: Database,
    *,
    window_ms: int = 50,
    min_pairs: int = 10,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Find ability pairs that fire from one NPC copy at the same instant.

    Two distinct ability IDs landing within milliseconds of each other, from the
    same copy, over and over, are very unlikely to be two independent mechanics.
    They are usually one game action that Warcraft Logs surfaces twice -- a cast
    and its linked effect, or an ability and the damage component that shares its
    animation.

    This matters because it is the "cast start plus completion counted twice"
    hazard in a different costume, and it is invisible in aggregate: every
    per-ability count looks plausible on its own, while any statistic that sums
    across abilities silently doubles. The detector reports the coincidence rate
    and stops there. Whether a pair is one action or two genuinely simultaneous
    ones is a question about the game, not about the data, and merging them here
    would destroy the evidence needed to answer it.
    """
    # COUNT(*) over this join counts *pairs*, and one ability firing twice inside
    # the window pairs with the other twice. Counting distinct event_ids on each
    # side instead keeps both rates in [0, 1] and, more usefully, separates "two
    # names for one action" (both sides near 1.0) from "B always accompanies A
    # but A often fires alone" (only one side near 1.0).
    rows = _rows(
        db,
        "WITH casts AS ("
        "  SELECT event_id, run_id, source_id, source_instance, ability_game_id, rel_ms"
        "    FROM events"
        "   WHERE type = 'cast' AND hostility = 'Enemies'"
        "     AND ability_game_id IS NOT NULL AND source_id IS NOT NULL"
        ") "
        "SELECT a.ability_game_id AS ability_a, b.ability_game_id AS ability_b, "
        "       COUNT(DISTINCT a.event_id) AS matched_a, "
        "       COUNT(DISTINCT b.event_id) AS matched_b "
        "  FROM casts a "
        "  JOIN casts b "
        "    ON a.run_id = b.run_id AND a.source_id = b.source_id "
        "   AND IFNULL(a.source_instance, -1) = IFNULL(b.source_instance, -1) "
        "   AND a.ability_game_id < b.ability_game_id "
        "   AND ABS(a.rel_ms - b.rel_ms) <= ? "
        " GROUP BY a.ability_game_id, b.ability_game_id "
        "HAVING COUNT(DISTINCT a.event_id) >= ? "
        " ORDER BY matched_a DESC LIMIT ?",
        (window_ms, min_pairs, limit),
    )

    out: list[dict[str, Any]] = []
    for row in rows:
        totals = {
            side: db.scalar(
                "SELECT COUNT(*) FROM events WHERE type = 'cast' AND hostility = 'Enemies' "
                "AND ability_game_id = ?",
                (row[f"ability_{side}"],),
            )
            or 0
            for side in ("a", "b")
        }
        rate_a = round(int(row["matched_a"]) / totals["a"], 3) if totals["a"] else None
        rate_b = round(int(row["matched_b"]) / totals["b"], 3) if totals["b"] else None
        out.append(
            {
                "ability_a_id": row["ability_a"],
                "ability_a_name": _ability_name(db, row["ability_a"]),
                "ability_a_casts": totals["a"],
                "ability_b_id": row["ability_b"],
                "ability_b_name": _ability_name(db, row["ability_b"]),
                "ability_b_casts": totals["b"],
                "matched_a": int(row["matched_a"]),
                "matched_b": int(row["matched_b"]),
                # Share of each side's casts accompanied by the other. Both near
                # 1.0 means one action under two IDs. One near 1.0 and the other
                # low means a subset relationship, which is a different fact.
                "rate_a": rate_a,
                "rate_b": rate_b,
                "inseparable": bool(
                    rate_a is not None and rate_b is not None and rate_a >= 0.95 and rate_b >= 0.95
                ),
                # Never seen apart does not mean one-for-one. A 4:1 ratio is a
                # pairing, not a duplicate -- probably one action whose ticking
                # component fires several times per marker -- and merging the two
                # would be as wrong as double-counting them.
                "cast_ratio": (
                    round(max(totals["a"], totals["b"]) / min(totals["a"], totals["b"]), 2)
                    if min(totals["a"], totals["b"])
                    else None
                ),
                "window_ms": window_ms,
            }
        )
    return out


def _pair_verdict(pair: dict[str, Any]) -> str:
    """What the numbers support saying, and no more."""
    if not pair["inseparable"]:
        return "partial overlap"
    ratio = pair.get("cast_ratio")
    if ratio is not None and ratio >= 1.5:
        return f"never apart, but {ratio}:1 -- one action with a repeating component"
    return "never apart, 1:1 -- probably one action under two IDs"


def _ability_name(db: Database, game_id: int | None) -> str | None:
    if game_id is None:
        return None
    return db.scalar("SELECT name FROM abilities WHERE game_id = ?", (game_id,))


#: A gap longer than this between consecutive pages is not collection, it is a
#: pause. Pages land about half a second apart, so a minute is generous enough
#: to keep any real hesitation and short enough to exclude a resumed session.
SESSION_GAP_S = 60.0


def _working_seconds_per_run(db: Database) -> list[dict[str, Any]]:
    """Seconds actually spent fetching each run, excluding pauses between sessions.

    This replaces MAX(fetched_at) - MIN(fetched_at), which measured calendar time
    and was wrong the moment a run was collected incrementally. Adding one stream
    to runs fetched the previous evening made every run report about eight hours,
    all within a hundred seconds of each other -- the gap between two sittings,
    reported as the cost of a run.

    Summing only the gaps small enough to be work gives a figure that means the
    same thing whether a corpus was collected in one sitting or twenty.
    """
    rows = _rows(
        db,
        "SELECT run_id, fetched_at, event_count FROM event_pages "
        " WHERE status = 'ok' ORDER BY run_id, fetched_at",
    )
    per_run: dict[str, dict[str, Any]] = {}
    previous_run: str | None = None
    previous_at = 0.0
    for row in rows:
        run_id = row["run_id"]
        entry = per_run.setdefault(
            run_id, {"run_id": run_id, "pages": 0, "events": 0, "span_s": 0.0, "gaps": 0}
        )
        entry["pages"] = int(entry["pages"]) + 1
        entry["events"] = int(entry["events"]) + int(row["event_count"] or 0)
        if run_id == previous_run:
            delta = float(row["fetched_at"]) - previous_at
            if 0 <= delta <= SESSION_GAP_S:
                entry["span_s"] = float(entry["span_s"]) + delta
            else:
                # A resumed session. Counted rather than silently folded in: a
                # run collected across many sittings has a less certain figure.
                entry["gaps"] = int(entry["gaps"]) + 1
        previous_run, previous_at = run_id, float(row["fetched_at"])

    out = [r for r in per_run.values() if int(r["pages"]) > 1]
    for entry in out:
        entry["span_s"] = round(float(entry["span_s"]), 1)
    out.sort(key=lambda r: float(r["span_s"]), reverse=True)
    return out


def _api_cost(db: Database) -> dict[str, Any]:
    """Hourly-budget spend per run, as observed across collection jobs."""
    rows = _rows(
        db,
        "SELECT detail FROM ingest_diagnostics WHERE kind = 'api_cost' ORDER BY created_at",
    )
    points = 0.0
    runs = 0
    unknown_jobs = 0
    for row in rows:
        try:
            payload = json.loads(row["detail"])
        except (ValueError, TypeError):
            continue
        if payload.get("points_spent") is None:
            unknown_jobs += 1
            continue
        points += float(payload["points_spent"])
        runs += int(payload.get("runs_completed") or 0)
    return {
        "jobs_measured": len(rows) - unknown_jobs,
        "jobs_unmeasured": unknown_jobs,
        "points_spent": round(points, 1) if runs else None,
        "runs_covered": runs,
        "points_per_run": round(points / runs, 2) if runs else None,
    }


def _date_span(db: Database, where: str, params: tuple[Any, ...]) -> dict[str, Any]:
    """How much calendar time this corpus covers.

    "66 runs unclassified" says nothing about whether epochs matter yet. Sixty-six
    runs from one week almost certainly share a mechanic version; the same sixty-six
    spread over four months almost certainly do not. The span is the difference, and
    it costs one query.
    """
    row = db.execute(
        f"SELECT MIN(abs_start_ms) AS first, MAX(abs_start_ms) AS last FROM dungeon_runs {where}",
        params,
    ).fetchone()
    if row is None or row["first"] is None:
        return {"first": None, "last": None, "days": None}
    days = (int(row["last"]) - int(row["first"])) / 86_400_000
    return {
        "first": _iso(int(row["first"])),
        "last": _iso(int(row["last"])),
        "days": round(days, 1),
    }


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def dedupe_coverage(db: Database) -> dict[str, Any]:
    """Whether duplicate detection has actually examined this corpus.

    `group_duplicates()` sets `is_canonical` on every run it considers --
    1 for a run in no group at all -- so a NULL there means no pass has ever
    looked at that run. Without this distinction a report saying "0 duplicate
    groups" reads as "none exist" when it may mean "never checked", and
    `analysable` silently counts both uploads of one real run as two.
    """
    total = db.scalar("SELECT COUNT(*) FROM dungeon_runs") or 0
    uncovered = db.scalar("SELECT COUNT(*) FROM dungeon_runs WHERE is_canonical IS NULL") or 0
    if total == 0:
        state = "empty"
    elif uncovered == total:
        state = "never_run"
    elif uncovered:
        state = "stale"
    else:
        state = "current"
    return {
        "state": state,
        "runs_total": total,
        "runs_not_examined": uncovered,
    }


def cost_profile(db: Database) -> dict[str, Any]:
    """What this corpus cost to fetch, measured rather than estimated.

    Wall clock is derived from `event_pages.fetched_at`, which is stamped when
    a page lands. Two caveats travel with every number here and are printed
    alongside them:

    * A run's span is ``MAX(fetched_at) - MIN(fetched_at)``, so it excludes the
      first page's own round trip and understates the true cost by one page.
    * The span includes the metadata queries and database writes interleaved
      with paging, so it is end-to-end cost, not pure API latency.

    Both biases are small and in known directions, which is enough to answer
    the only question this section exists for: what does one more run cost, and
    which event stream is buying the least per second spent.
    """
    by_stream = _rows(
        db,
        "SELECT data_type, hostility, COUNT(*) AS pages, "
        "       SUM(event_count) AS events, COUNT(DISTINCT run_id) AS runs "
        "FROM event_pages WHERE status = 'ok' "
        "GROUP BY data_type, hostility ORDER BY pages DESC",
    )
    for row in by_stream:
        runs = int(row["runs"] or 0)
        row["pages_per_run"] = round(int(row["pages"]) / runs, 1) if runs else None

    by_run = _working_seconds_per_run(db)

    total_pages = db.scalar("SELECT COUNT(*) FROM event_pages WHERE status = 'ok'") or 0
    spans = [float(r["span_s"]) for r in by_run if r["span_s"] is not None]
    paged_runs = len(spans)
    total_span = sum(spans)
    measured_pages = sum(int(r["pages"]) - 1 for r in by_run)

    return {
        "by_stream": by_stream,
        "by_run": by_run[:20],
        "total_pages": total_pages,
        "runs_with_pagination": paged_runs,
        "mean_seconds_per_run": round(total_span / paged_runs, 1) if paged_runs else None,
        "mean_seconds_per_page": (
            round(total_span / measured_pages, 2) if measured_pages > 0 else None
        ),
        "mean_pages_per_run": (round(total_pages / paged_runs, 1) if paged_runs else None),
        "page_limit_used": EVENTS_PAGE_LIMIT,
        "api_points": _api_cost(db),
        "runs_collected_across_sessions": sum(1 for r in by_run if int(r.get("gaps") or 0)),
        "measurement_caveats": [
            "Per-run span excludes the first page's round trip (understates by one page).",
            f"Gaps over {SESSION_GAP_S:.0f}s between pages are treated as pauses between "
            "sessions and excluded, so this is time worked, not time elapsed.",
            "Span includes metadata queries and database writes, not API latency alone.",
            "Collection is strictly serial; these numbers carry no concurrency.",
        ],
    }


def npc_instance_evidence(db: Database, limit: int = 5) -> list[dict[str, Any]]:
    """Reconstruct per-copy cast timelines for NPCs that appeared more than once.

    This is the demonstration the whole corpus rests on. If two copies of a
    species do not come back as separate rows here, every recast statistic
    derived from this database is invalid, and the report must say so.
    """
    rows = _rows(
        db,
        "SELECT a.name AS npc, e.source_id, e.source_instance, e.pull_id, "
        "       ab.name AS ability, e.ability_game_id, COUNT(*) AS casts, "
        "       MIN(e.pull_rel_ms) AS first_cast_ms, MAX(e.pull_rel_ms) AS last_cast_ms "
        "  FROM events e "
        "  LEFT JOIN actors a ON a.report_code = e.report_code AND a.actor_id = e.source_id "
        "  LEFT JOIN abilities ab ON ab.game_id = e.ability_game_id "
        " WHERE e.type = 'cast' AND e.hostility = 'Enemies' "
        "   AND e.source_instance IS NOT NULL "
        # An event outside every pull has no pull-relative clock, so it can
        # demonstrate separation but never recast timing. Excluded here and
        # counted in known_limitations() instead of being shown with null times.
        "   AND e.pull_id IS NOT NULL AND e.pull_rel_ms IS NOT NULL "
        " GROUP BY e.pull_id, e.source_id, e.source_instance, e.ability_game_id "
        " ORDER BY e.pull_id, e.source_id, e.source_instance",
    )
    grouped: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["pull_id"], row["source_id"], row["ability_game_id"]), []).append(
            row
        )

    evidence = [
        {
            "pull_id": key[0],
            "npc": entries[0]["npc"],
            "ability": entries[0]["ability"],
            "distinct_copies": len(entries),
            "per_copy": [
                {
                    "instance": e["source_instance"],
                    "casts": e["casts"],
                    "first_cast_ms_into_pull": e["first_cast_ms"],
                    "last_cast_ms_into_pull": e["last_cast_ms"],
                }
                for e in entries
            ],
        }
        for key, entries in grouped.items()
        if len(entries) > 1
    ]
    for example in evidence:
        example["median_interval_ms"] = _median_interval_ms(example["per_copy"])

    # Order by what each example proves, not by insertion order. A copy that
    # casts twice shows a recast interval; ten copies casting once each show
    # only that the copies are distinct. Both matter, the first matters more,
    # so surface the richest examples rather than whichever pull sorted first.
    evidence.sort(
        key=lambda ev: (
            max(int(c["casts"]) for c in ev["per_copy"]),
            ev["distinct_copies"],
        ),
        reverse=True,
    )

    # One example per NPC. Ranking alone let a single melee-heavy species take
    # four of five slots -- twice over, because two of its ability IDs fire
    # together -- which demonstrates the same thing four times and hides every
    # other species in the corpus. Breadth is the point of a sample.
    seen: set[Any] = set()
    diverse = []
    for example in evidence:
        if example["npc"] in seen:
            continue
        seen.add(example["npc"])
        diverse.append(example)
    return diverse[:limit]


def _median_interval_ms(per_copy: list[dict[str, Any]]) -> float | None:
    """Median of each copy's mean gap between its own casts.

    Reported, never filtered on. An interval near one second is almost
    certainly melee filler rather than a mechanic, but that is the reader's
    call to make against the game, and a threshold here would quietly decide it.
    """
    intervals = [
        (c["last_cast_ms_into_pull"] - c["first_cast_ms_into_pull"]) / (c["casts"] - 1)
        for c in per_copy
        if c["casts"] > 1
        and c["first_cast_ms_into_pull"] is not None
        and c["last_cast_ms_into_pull"] is not None
    ]
    return round(statistics.median(intervals)) if intervals else None


def known_limitations(db: Database) -> list[str]:
    """Everything the report should not be read as claiming."""
    limitations: list[str] = []

    dedupe = dedupe_coverage(db)
    if dedupe["state"] == "never_run":
        limitations.append(
            f"Duplicate detection has never been run over these {dedupe['runs_total']} run(s). "
            "A reported count of 0 duplicate groups means 'not looked', not 'none found', and "
            "the analysable count may hold two uploads of the same real run as two "
            "observations. Run `wclmplus dedupe`."
        )
    elif dedupe["state"] == "stale":
        limitations.append(
            f"{dedupe['runs_not_examined']} run(s) were collected after the last duplicate "
            "detection pass and have not been examined. Re-run `wclmplus dedupe` before "
            "treating the analysable count as a count of distinct real runs."
        )

    coverage = stream_coverage(db)
    if coverage["manifest_rows"] == 0 and (db.scalar("SELECT COUNT(*) FROM dungeon_runs") or 0):
        limitations.append(
            "No stream-coverage manifest exists for these runs: they were collected before the "
            "manifest was added. A stream holding zero events cannot be distinguished from a "
            "stream that was never requested. Re-collect to populate it."
        )
    if coverage["incomplete_streams"]:
        limitations.append(
            f"{len(coverage['incomplete_streams'])} stream(s) did not paginate to exhaustion. "
            "A count of zero events in those streams means the collection stopped, not that "
            "nothing happened; exclude them or re-collect before using them."
        )
    if coverage["distinct_shapes"] > 1:
        thin = [s for s in coverage["shapes"] if s["missing_vs_widest"]]
        detail = "; ".join(
            f"{s['runs']} run(s) lack {', '.join(s['missing_vs_widest'])}" for s in thin[:3]
        )
        limitations.append(
            f"This corpus was collected under {coverage['distinct_shapes']} different event "
            f"profiles ({detail}). Runs holding fewer streams are not runs where those events "
            "did not occur -- they were never requested. Any statistic over a stream some runs "
            "lack must be computed only over the runs that carry it, or the absent rows will "
            "read as zeros."
        )

    inseparable = [p for p in paired_abilities(db) if p["inseparable"]]
    if inseparable:
        names = ", ".join(f"{p['ability_a_name']} + {p['ability_b_name']}" for p in inseparable[:3])
        limitations.append(
            f"{len(inseparable)} enemy ability pair(s) are effectively never observed apart "
            f"({names}). Each pair is probably one game action reported under two ability IDs, "
            "so any statistic that sums casts across abilities will double-count it. Both are "
            "retained unmerged; per-ability counts are unaffected."
        )

    unassigned = db.scalar("SELECT COUNT(*) FROM events WHERE pull_id IS NULL") or 0
    if unassigned:
        limitations.append(
            f"{unassigned} event(s) fall outside every Warcraft Logs pull and carry no "
            "pull-relative time. They are retained, and any per-pull statistic excludes them."
        )

    null_instances = (
        db.scalar(
            "SELECT COUNT(*) FROM events WHERE hostility = 'Enemies' "
            "AND type IN ('cast','begincast') AND source_instance IS NULL"
        )
        or 0
    )
    if null_instances:
        limitations.append(
            f"{null_instances} enemy cast event(s) carry no sourceInstance. This probably "
            "means the NPC had a single copy, but that is an inference: the field is stored "
            "NULL and must be resolved against the pull's instance range, never defaulted."
        )

    span = _date_span(db, "", ())
    unclassified = (
        db.scalar("SELECT COUNT(*) FROM dungeon_runs WHERE hotfix_epoch = 'unclassified'") or 0
    )
    if unclassified:
        limitations.append(
            f"{unclassified} run(s) have hotfix epoch 'unclassified' because no epochs are "
            "declared in config/hotfix_epochs.yml. Absolute run dates are retained, so epochs "
            "can be applied retroactively, but results must not be pooled across a mechanic "
            "change until they are."
            + (
                f" This corpus spans {span['days']} days ({span['first']} to {span['last']}), "
                "which is how much calendar time is being pooled."
                # 0.0 days is falsy and is the most reassuring answer there is:
                # a corpus collected inside one day cannot straddle a hotfix.
                if span.get("days") is not None
                else ""
            )
        )

    incomplete = (
        db.scalar("SELECT COUNT(*) FROM dungeon_runs WHERE collection_status != 'complete'") or 0
    )
    if incomplete:
        limitations.append(
            f"{incomplete} run(s) are not 'complete' and must be excluded from any "
            "event-frequency denominator."
        )

    if not (db.scalar("SELECT COUNT(*) FROM run_players") or 0):
        limitations.append(
            "No roster rows were stored, so duplicate detection cannot use roster overlap -- "
            "its strongest signal."
        )

    limitations.append(
        "Player identity is a hash of the character name alone: Warcraft Logs master data "
        "exposes no realm for players, so two same-named characters on different realms "
        "collide."
    )
    return limitations


def write_reports(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    """Write the JSON evidence and the readable summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "validation.json"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    md_path = output_dir / "VALIDATION_REPORT.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_markdown(report: dict[str, Any]) -> str:
    prov = report["provenance"]
    runs = report["runs"]
    events = report["events"]
    pages = report["pages"]

    lines = [
        "# Validation report",
        "",
        "- Generated: "
        + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(report["generated_at"])),
        f"- Software {prov.get('software_version')} "
        f"(commit {prov.get('git_commit') or 'unknown'}"
        f"{', dirty tree' if prov.get('git_dirty') else ''})",
        f"- Schema version {prov.get('schema_version')}, "
        f"normalizer {prov.get('normalizer_version')}, queries {prov.get('query_version')}",
    ]
    if report.get("dungeon_filter"):
        lines.append(f"- Filtered to dungeon: `{report['dungeon_filter']}`")

    lines += [
        "",
        "## Corpus",
        "",
        f"- Database: `{report['database']['path']}` "
        f"({report['database']['size_bytes'] / 1_048_576:.1f} MiB)",
        f"- **Runs analysable** (complete and canonical): **{runs['analysable']}**",
        "",
        "| Collection status | Runs |",
        "| --- | --- |",
    ]
    lines += [f"| {r['collection_status']} | {r['n']} |" for r in runs["by_collection_status"]]

    if runs["by_key_bracket"]:
        lines += [
            "",
            "| Key bracket | Runs | Key levels | Mean duration (s) |",
            "| --- | ---: | --- | ---: |",
        ]
        lines += [
            f"| {r['key_bracket'] or 'outside brackets'} | {r['runs']} | "
            f"{r['min_key']}-{r['max_key']} | {r['mean_duration_s']} |"
            for r in runs["by_key_bracket"]
        ]

    lines += [
        "",
        "## Events",
        "",
        f"- Total: **{events['total']:,}**",
        f"- Assigned to a pull: {events['assigned_to_pulls']:,} ({events['assigned_pct']}%)",
        f"- Outside every pull: {events['unassigned']:,} (retained)",
        f"- Events whose source actor is not in master data: {events['unknown_source_actors']}",
        f"- Events whose ability is not in master data: {events['unknown_abilities']}",
        "",
        "| Event stream | Events |",
        "| --- | --- |",
    ]
    lines += [
        f"| {r['data_type']}{'@' + r['hostility'] if r['hostility'] else ''} | {r['n']:,} |"
        for r in events["by_data_type"]
    ]

    lines += [
        "",
        "## Pagination",
        "",
        f"- Pages fetched: {pages['total']:,}",
        f"- Failed pages: {pages['failed']}",
        f"- Streams left incomplete: {pages['incomplete_streams']}"
        + ("  ← resume needed" if pages["incomplete_streams"] else ""),
    ]

    cost = report["cost"]
    lines += [
        "",
        "## Cost and throughput",
        "",
        f"- Pages fetched per run (mean): {cost['mean_pages_per_run']}",
        f"- Seconds per run (mean): {cost['mean_seconds_per_run']}",
        f"- Seconds per page (mean): {cost['mean_seconds_per_page']}",
        f"- Events requested per page: {cost['page_limit_used']:,}",
        f"- API points per run: {cost['api_points']['points_per_run']}"
        + (
            f"  (over {cost['api_points']['runs_covered']} run(s); "
            f"{cost['api_points']['jobs_unmeasured']} job(s) unmeasured)"
            if cost["api_points"]["points_per_run"] is not None
            else "  ← not yet measured"
        ),
        "",
        "| Stream | Pages | Pages/run | Events |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines += [
        "| {}{} | {:,} | {} | {:,} |".format(
            r["data_type"],
            "@" + r["hostility"] if r["hostility"] else "",
            int(r["pages"]),
            r["pages_per_run"],
            int(r["events"] or 0),
        )
        for r in cost["by_stream"]
    ]
    lines += ["", "Measurement caveats:"]
    lines += [f"- {c}" for c in cost["measurement_caveats"]]

    coverage = report["stream_coverage"]
    lines += [
        "",
        "## Event streams collected",
        "",
        f"- Distinct stream shapes in this corpus: {coverage['distinct_shapes']}"
        + ("  ← mixed fidelity" if coverage["distinct_shapes"] > 1 else ""),
        f"- Coverage manifest rows: {coverage['manifest_rows']:,}",
        f"- Streams not collected to exhaustion: {len(coverage['incomplete_streams'])}"
        + ("  ← a zero in these is not a real zero" if coverage["incomplete_streams"] else ""),
    ]
    if coverage["distinct_shapes"] > 1:
        lines += ["", "| Runs | Missing streams |", "| ---: | --- |"]
        lines += [
            f"| {s['runs']} | {', '.join(s['missing_vs_widest']) or 'none (widest)'} |"
            for s in coverage["shapes"]
        ]
    else:
        lines += [f"- Streams: {', '.join(coverage['streams_seen'])}"]

    pairs = report["paired_abilities"]
    if pairs:
        lines += [
            "",
            "## Abilities that fire together",
            "",
            "Distinct ability IDs cast by the same NPC copy within "
            f"{pairs[0]['window_ms']} ms. A rate near 1.00 means the two are never seen "
            "apart, which usually means one game action reported twice. Nothing is merged.",
            "",
            "| Ability A | Ability B | Rate A | Rate B | Ratio | Reading |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
        lines += [
            f"| {p['ability_a_name']} ({p['ability_a_casts']:,}) "
            f"| {p['ability_b_name']} ({p['ability_b_casts']:,}) "
            f"| {p['rate_a']} | {p['rate_b']} | {p['cast_ratio']} "
            f"| {_pair_verdict(p)} |"
            for p in pairs
        ]

    dupes = report["duplicates"]
    lines += [
        "",
        "## Duplicate runs",
        "",
        f"- Probable duplicate groups: {dupes['groups']}"
        + (
            "  ← duplicate detection has never been run; this is 'not looked', not 'none'"
            if report["dedupe"]["state"] == "never_run"
            else f"  ← {report['dedupe']['runs_not_examined']} run(s) not yet examined"
            if report["dedupe"]["state"] == "stale"
            else ""
        ),
        f"- Runs inside a group: {dupes['runs_in_groups']} (none deleted; one per group "
        "is marked canonical)",
    ]

    evidence = report["npc_instance_evidence"]
    lines += ["", "## NPC instance identity — demonstrated", ""]
    if evidence:
        lines.append(
            "Per-copy cast timelines reconstructed from the database. Separate rows for "
            "the same NPC and ability in one pull mean two copies were kept apart; merging "
            "them would invent recasts that never happened."
        )
        lines.append("")
        lines.append("| Pull | NPC | Ability | Copy | Casts | First cast (ms into pull) |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in evidence:
            for copy in item["per_copy"]:
                lines.append(
                    f"| `{item['pull_id']}` | {item['npc']} | {item['ability']} | "
                    f"{copy['instance']} | {copy['casts']} | {copy['first_cast_ms_into_pull']} |"
                )
    else:
        lines.append(
            "**No pull in this corpus contained two copies of one NPC casting.** The "
            "instance-separation claim is therefore untested here. Collect a run with a "
            "duplicate-species pull before relying on per-instance recast statistics."
        )

    if report["diagnostics"]:
        lines += [
            "",
            "## Ingest diagnostics",
            "",
            "| Kind | Severity | Count |",
            "| --- | --- | --- |",
        ]
        lines += [f"| {d['kind']} | {d['severity']} | {d['n']} |" for d in report["diagnostics"]]

    lines += ["", "## Known limitations", ""]
    lines += [f"{i}. {text}" for i, text in enumerate(report["limitations"], start=1)] or [
        "None recorded."
    ]
    lines += ["", "---", "", "Machine-readable evidence: `validation.json` in this directory.", ""]
    return "\n".join(lines)
