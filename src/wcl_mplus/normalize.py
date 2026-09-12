"""Raw API responses to database rows.

Built against the event shapes **observed** in five live recon runs, not
against guesses. The rules below are research decisions, not conveniences:

* **Three timestamp bases per event.** The API returns milliseconds relative to
  report start. Pull-relative time is the research unit, run-relative is needed
  for run-level comparison, and absolute is needed for hotfix epochs and date
  filtering. Deriving one later is cheap; recovering a discarded one is not.

* **`sourceInstance` is never defaulted.** A missing value probably means the
  NPC had only one copy, but that is an inference about the pull, not a fact
  about the event. It stays NULL and is resolved during analysis against the
  pull's instance range.

* **Unpromoted fields go to `extra` verbatim.** Forcing every event into a
  fixed schema would discard fields whose value is not yet understood, and the
  whole point is to answer questions not yet asked.

* **Player names are pseudonymized here**, at the boundary, so no downstream
  table or export can leak one. NPC and ability names are kept: they are the
  subject of the research.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from .db import json_or_none
from .sanitize import identity_hash, pseudonym
from .version import NORMALIZER_VERSION

logger = logging.getLogger(__name__)

#: Event fields promoted to their own column. Everything else lands in `extra`.
#: Keys are the API's names; values are the column names.
PROMOTED_EVENT_FIELDS: dict[str, str] = {
    "timestamp": "rel_ms",
    "type": "type",
    "sourceID": "source_id",
    "sourceInstance": "source_instance",
    "targetID": "target_id",
    "targetInstance": "target_instance",
    "abilityGameID": "ability_game_id",
    "extraAbilityGameID": "extra_ability_game_id",
    "amount": "amount",
    "absorbed": "absorbed",
    "blocked": "blocked",
    "mitigated": "mitigated",
    "unmitigatedAmount": "unmitigated_amount",
    "overkill": "overkill",
    "hitType": "hit_type",
    "tick": "is_tick",
    "isAoE": "is_aoe",
    "isBuff": "is_buff",
    "stack": "stack",
    "hitPoints": "hit_points",
    "maxHitPoints": "max_hit_points",
    "buffs": "buffs",
    "killerID": "killer_id",
    "killerInstance": "killer_instance",
    "killingAbilityGameID": "killing_ability_game_id",
    "x": "x",
    "y": "y",
    "mapID": "map_id",
}

#: Fields deliberately left in `extra` rather than promoted. Listed so the
#: choice is visible: these are per-event snapshots of the *actor's* state, not
#: properties of the event, and none of the stated research questions indexes
#: on them.
KNOWN_EXTRA_FIELDS = frozenset(
    {
        "armor",
        "attackPower",
        "spellPower",
        "versatility",
        "avoidance",
        "absorb",
        "itemLevel",
        "classResources",
        "resourceActor",
        "facing",
        "fight",
        "fake",
    }
)

#: `type` values on masterData actors that are players rather than NPCs.
PLAYER_ACTOR_TYPES = frozenset({"Player"})
PET_ACTOR_TYPES = frozenset({"Pet", "PlayerPet"})


def _as_int(value: Any) -> int | None:
    """Coerce to int, or None. Floats are truncated deliberately.

    Several numeric fields come back as floats (`rating`, ability `gameID`)
    even though they are conceptually integers.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_bool_int(value: Any) -> int | None:
    """SQLite has no boolean; store 0/1 and keep None as None."""
    if value is None:
        return None
    return 1 if value else 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def normalize_report(
    report: dict[str, Any], *, retrieved_at: float, collection_status: str = "metadata-only"
) -> dict[str, Any]:
    """One `reports` row from a report-metadata response."""
    archive = report.get("archiveStatus") or {}
    zone = report.get("zone") or {}
    region = report.get("region") or {}
    owner = report.get("owner") or {}
    title = report.get("title")

    return {
        "report_code": report.get("code"),
        # Titles routinely contain names and guild tags.
        "title_hash": identity_hash(title) if isinstance(title, str) and title else None,
        "owner_hash": (
            pseudonym(str(owner.get("name")), "uploader") if owner.get("name") else None
        ),
        "start_time_ms": _as_int(report.get("startTime")),
        "end_time_ms": _as_int(report.get("endTime")),
        "zone_id": _as_int(zone.get("id")),
        "zone_name": zone.get("name"),
        "region_id": _as_int(region.get("id")),
        "region_slug": region.get("slug"),
        "revision": _as_int(report.get("revision")),
        "segments": _as_int(report.get("segments")),
        "visibility": report.get("visibility"),
        "is_archived": _as_bool_int(archive.get("isArchived")),
        "is_accessible": _as_bool_int(archive.get("isAccessible")),
        "archive_date": _as_int(archive.get("archiveDate")),
        "log_version": None,
        "game_version": None,
        "retrieved_at": retrieved_at,
        "collection_status": collection_status,
    }


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def run_id_for(report_code: str, fight_id: int) -> str:
    return f"{report_code}:{fight_id}"


def is_mythic_plus(fight: dict[str, Any]) -> bool:
    """A fight counts as Mythic+ when it carries a keystone level.

    Held on the one report inspected (13 of 15 fights), but it is one report.
    Recorded as an assumption in API_NOTES.md rather than a certainty.
    """
    return fight.get("keystoneLevel") is not None


def normalize_run(
    fight: dict[str, Any],
    *,
    report_code: str,
    report_start_ms: int,
    hotfix_epoch: str,
    key_bracket: str | None,
    dungeon_key: str | None,
    wcl_zone_id: int | None,
    collection_status: str = "metadata-only",
    job_id: str | None = None,
) -> dict[str, Any]:
    """One `dungeon_runs` row from a fight."""
    rel_start = _as_int(fight.get("startTime")) or 0
    rel_end = _as_int(fight.get("endTime")) or 0
    game_zone = fight.get("gameZone") or {}
    bonus = _as_int(fight.get("keystoneBonus"))

    return {
        "run_id": run_id_for(report_code, _as_int(fight.get("id")) or 0),
        "report_code": report_code,
        "fight_id": _as_int(fight.get("id")),
        "dungeon_key": dungeon_key,
        "encounter_id": _as_int(fight.get("encounterID")),
        "game_zone_id": _as_int(game_zone.get("id")),
        "game_zone_name": game_zone.get("name"),
        "wcl_zone_id": wcl_zone_id,
        "rel_start_ms": rel_start,
        "rel_end_ms": rel_end,
        "abs_start_ms": report_start_ms + rel_start,
        "abs_end_ms": report_start_ms + rel_end,
        "duration_ms": max(0, rel_end - rel_start),
        "keystone_level": _as_int(fight.get("keystoneLevel")),
        "keystone_affixes": json_or_none(fight.get("keystoneAffixes")),
        "keystone_bonus": bonus,
        "keystone_time_ms": _as_int(fight.get("keystoneTime")),
        "rating": fight.get("rating"),
        "count_reached": _as_int(fight.get("countReached")),
        "count_required": _as_int(fight.get("countRequired")),
        "average_item_level": fight.get("averageItemLevel"),
        "kill": _as_bool_int(fight.get("kill")),
        # keystoneBonus counts upgrade levels: >0 means the key was timed.
        # None stays None -- "not timed" and "unknown" are different facts.
        "timed": None if bonus is None else (1 if bonus > 0 else 0),
        "size": _as_int(fight.get("size")),
        "npc_count_map": json_or_none(fight.get("npcCountMap")),
        "hotfix_epoch": hotfix_epoch,
        "key_bracket": key_bracket,
        "duplicate_group_id": None,
        "is_canonical": None,
        "collection_status": collection_status,
        "job_id": job_id,
    }


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


def normalize_actors(master: dict[str, Any], *, report_code: str) -> list[dict[str, Any]]:
    """`actors` rows. Player and pet names are pseudonymized here."""
    rows: list[dict[str, Any]] = []
    for actor in master.get("actors") or []:
        if not isinstance(actor, dict):
            continue
        actor_type = str(actor.get("type") or "")
        is_player = actor_type in PLAYER_ACTOR_TYPES
        name = actor.get("name")
        if isinstance(name, str) and name:
            if is_player:
                name = pseudonym(name, "player")
            elif actor_type in PET_ACTOR_TYPES:
                name = pseudonym(name, "pet")
        rows.append(
            {
                "report_code": report_code,
                "actor_id": _as_int(actor.get("id")),
                "game_id": _as_int(actor.get("gameID")),
                "name": name,
                "type": actor_type or None,
                "sub_type": actor.get("subType"),
                "icon": actor.get("icon"),
                "pet_owner": _as_int(actor.get("petOwner")),
                "is_player": 1 if is_player else 0,
            }
        )
    return rows


def normalize_abilities(master: dict[str, Any], *, seen_at: float) -> list[dict[str, Any]]:
    """`abilities` rows. `gameID` comes back as a float and is truncated."""
    rows: list[dict[str, Any]] = []
    for ability in master.get("abilities") or []:
        if not isinstance(ability, dict):
            continue
        game_id = _as_int(ability.get("gameID"))
        if game_id is None:
            continue
        rows.append(
            {
                "game_id": game_id,
                "name": ability.get("name"),
                "icon": ability.get("icon"),
                "type": ability.get("type"),
                "first_seen": seen_at,
                "last_seen": seen_at,
            }
        )
    return rows


def class_and_spec(actor: dict[str, Any]) -> tuple[str | None, str | None]:
    """Derive class and spec from a masterData actor.

    `icon` is formatted "Class-Spec" (e.g. "Paladin-Protection") and `subType`
    carries the spec on its own. Class comes from the icon prefix because no
    dedicated class field exists on the actor.
    """
    spec = actor.get("subType") or None
    icon = actor.get("icon")
    class_name = None
    if isinstance(icon, str) and "-" in icon:
        class_name = icon.split("-", 1)[0] or None
    elif isinstance(icon, str) and icon:
        class_name = icon
    return class_name, spec


def normalize_run_players(
    master: dict[str, Any],
    *,
    run_id: str,
    friendly_player_ids: list[int],
    role_for: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`run_players` and `players` rows for one run's roster.

    Only actors listed in the fight's `friendlyPlayers` are included: a report
    holds every player across every fight in it, and a run's roster is the five
    who were actually there.
    """
    wanted = set(friendly_player_ids)
    run_rows: list[dict[str, Any]] = []
    player_rows: list[dict[str, Any]] = []
    now = __import__("time").time()

    for actor in master.get("actors") or []:
        if not isinstance(actor, dict):
            continue
        actor_id = _as_int(actor.get("id"))
        if actor_id is None or actor_id not in wanted:
            continue
        if str(actor.get("type") or "") not in PLAYER_ACTOR_TYPES:
            continue
        raw_name = actor.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            continue
        player_id = identity_hash(raw_name)
        class_name, spec = class_and_spec(actor)
        run_rows.append(
            {
                "run_id": run_id,
                "actor_id": actor_id,
                "player_id": player_id,
                "class": class_name,
                "spec": spec,
                "role": role_for(class_name, spec) if callable(role_for) else None,
                "item_level": None,
            }
        )
        player_rows.append(
            {"player_id": player_id, "first_seen": now, "last_seen": now, "run_count": 0}
        )
    return run_rows, player_rows


# ---------------------------------------------------------------------------
# Pulls
# ---------------------------------------------------------------------------


def pull_id_for(run_id: str, wcl_pull_id: int) -> str:
    return f"{run_id}:{wcl_pull_id}"


def composition_signature(npcs: list[dict[str, Any]]) -> tuple[str, int, int]:
    """Canonical composition string plus species and total counts.

    Rendered as `gameID x count | gameID x count`, sorted by game ID so the
    same composition always produces the same string. Counts come from the
    instance-ID range, which is an inference about multiplicity -- see
    `instance_count`.
    """
    counts: Counter[int] = Counter()
    for npc in npcs:
        game_id = _as_int(npc.get("gameID"))
        if game_id is None:
            continue
        counts[game_id] += instance_count(npc)[0] or 1
    if not counts:
        return "", 0, 0
    parts = [f"{game_id}x{count}" for game_id, count in sorted(counts.items())]
    return " | ".join(parts), len(counts), sum(counts.values())


def instance_count(npc: dict[str, Any]) -> tuple[int | None, str]:
    """How many copies of this NPC the pull held, and how sure we are.

    The API gives an instance-ID *range*, not a count. A range of 1..18 means
    eighteen copies were seen -- almost certainly. Both values are stored, and
    the count carries its confidence, because a derived multiplicity must never
    be mistaken for a reported one.
    """
    low = _as_int(npc.get("minimumInstanceID"))
    high = _as_int(npc.get("maximumInstanceID"))
    if low is None and high is None:
        # WCL omits instance IDs for a species with a single copy.
        return 1, "inferred"
    if low is None or high is None:
        return None, "unknown"
    if high < low:
        return None, "unknown"
    return high - low + 1, "inferred"


def normalize_pulls(
    pulls: list[dict[str, Any]],
    *,
    run_id: str,
    report_start_ms: int,
    run_rel_start_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`pulls` and `pull_npcs` rows for one run.

    Pulls are ordered by start time and linked prev/next, so route context --
    which pack came before which -- survives into analysis.
    """
    ordered = sorted(
        (p for p in pulls if isinstance(p, dict)),
        key=lambda p: (_as_int(p.get("startTime")) or 0, _as_int(p.get("id")) or 0),
    )
    pull_rows: list[dict[str, Any]] = []
    npc_rows: list[dict[str, Any]] = []

    for index, pull in enumerate(ordered):
        wcl_pull_id = _as_int(pull.get("id"))
        if wcl_pull_id is None:
            continue
        this_id = pull_id_for(run_id, wcl_pull_id)
        rel_start = _as_int(pull.get("startTime")) or 0
        rel_end = _as_int(pull.get("endTime")) or 0
        npcs = [n for n in (pull.get("enemyNPCs") or []) if isinstance(n, dict)]
        signature, species, total = composition_signature(npcs)
        encounter_id = _as_int(pull.get("encounterID"))

        pull_rows.append(
            {
                "pull_id": this_id,
                "run_id": run_id,
                "wcl_pull_id": wcl_pull_id,
                "pull_index": index,
                "name": pull.get("name"),
                "encounter_id": encounter_id,
                # encounterID 0 is trash; a real encounter ID marks a boss.
                "is_boss": 1 if (encounter_id or 0) > 0 else 0,
                "rel_start_ms": rel_start,
                "rel_end_ms": rel_end,
                "run_rel_start_ms": rel_start - run_rel_start_ms,
                "run_rel_end_ms": rel_end - run_rel_start_ms,
                "abs_start_ms": report_start_ms + rel_start,
                "abs_end_ms": report_start_ms + rel_end,
                "duration_ms": max(0, rel_end - rel_start),
                "kill": _as_bool_int(pull.get("kill")),
                "x": _as_int(pull.get("x")),
                "y": _as_int(pull.get("y")),
                "map_ids": json_or_none(
                    [_as_int(m.get("id")) for m in (pull.get("maps") or []) if isinstance(m, dict)]
                ),
                "bounding_box": json_or_none(pull.get("boundingBox")),
                "composition_signature": signature or None,
                "npc_species_count": species,
                "npc_total_count": total,
                "prev_pull_id": None,
                "next_pull_id": None,
            }
        )

        for npc in npcs:
            game_id = _as_int(npc.get("gameID"))
            if game_id is None:
                continue
            count, confidence = instance_count(npc)
            npc_rows.append(
                {
                    "pull_id": this_id,
                    "npc_game_id": game_id,
                    "actor_id": _as_int(npc.get("id")),
                    "min_instance_id": _as_int(npc.get("minimumInstanceID")),
                    "max_instance_id": _as_int(npc.get("maximumInstanceID")),
                    "min_instance_group_id": _as_int(npc.get("minimumInstanceGroupID")),
                    "max_instance_group_id": _as_int(npc.get("maximumInstanceGroupID")),
                    "instance_count": count,
                    "instance_count_confidence": confidence,
                }
            )

    for previous, current in zip(pull_rows, pull_rows[1:], strict=False):
        previous["next_pull_id"] = current["pull_id"]
        current["prev_pull_id"] = previous["pull_id"]

    return pull_rows, npc_rows


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def normalize_event(
    event: dict[str, Any],
    *,
    page_id: int,
    seq_in_page: int,
    run_id: str,
    report_code: str,
    report_start_ms: int,
    run_rel_start_ms: int,
    data_type: str,
    hostility: str | None,
    pull_id: str | None = None,
    pull_rel_start_ms: int | None = None,
) -> dict[str, Any]:
    """One `events` row.

    `source_instance` and `target_instance` pass through untouched, including
    when absent. A missing instance is a fact about the API's output, and
    turning it into `1` here would silently assert that the NPC had one copy.
    """
    rel_ms = _as_int(event.get("timestamp"))
    if rel_ms is None:
        rel_ms = 0

    row: dict[str, Any] = {
        "page_id": page_id,
        "seq_in_page": seq_in_page,
        "run_id": run_id,
        "report_code": report_code,
        "pull_id": pull_id,
        "data_type": data_type,
        "hostility": hostility,
        "rel_ms": rel_ms,
        "abs_ms": report_start_ms + rel_ms,
        "run_rel_ms": rel_ms - run_rel_start_ms,
        "pull_rel_ms": (None if pull_rel_start_ms is None else rel_ms - pull_rel_start_ms),
        "normalizer_version": NORMALIZER_VERSION,
    }

    extra: dict[str, Any] = {}
    for key, value in event.items():
        column = PROMOTED_EVENT_FIELDS.get(key)
        if column is None:
            extra[key] = value
            continue
        if column in ("is_tick", "is_aoe", "is_buff"):
            row[column] = _as_bool_int(value)
        elif column in ("type", "buffs"):
            row[column] = value if value is None else str(value)
        elif column == "rel_ms":
            continue  # already set
        else:
            row[column] = _as_int(value)

    row.setdefault("type", str(event.get("type") or "unknown"))
    for column in set(PROMOTED_EVENT_FIELDS.values()):
        row.setdefault(column, None)
    row["rel_ms"] = rel_ms
    row["extra"] = json_or_none(extra)
    return row


def unexpected_event_fields(events: list[dict[str, Any]]) -> set[str]:
    """Field names seen that are neither promoted nor knowingly left in `extra`.

    Surfaced as a diagnostic: a new field appearing in the API is a finding,
    and noticing it beats discovering it a season later.
    """
    seen: set[str] = set()
    for event in events:
        if isinstance(event, dict):
            seen.update(event)
    return seen - set(PROMOTED_EVENT_FIELDS) - KNOWN_EXTRA_FIELDS
