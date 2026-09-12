"""Dungeon and zone discovery from the live API.

Dungeon identity is never hardcoded (brief sections 33, 54). This module
queries the zone registry, matches it against the dungeon names configured in
`config/dungeons.yml`, and writes the confirmed IDs into a machine-generated
overlay file. The authored config keeps its comments; the overlay carries the
facts.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .configs import DungeonRegistry
from .querybuild import load_query

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import GraphQLClient

logger = logging.getLogger(__name__)

OVERLAY_HEADER = """\
# ---------------------------------------------------------------------------
# GENERATED FILE -- do not edit by hand.
#
# Written by `wclmplus discover-dungeons --write` from the live Warcraft Logs
# zone registry. It is merged over config/dungeons.yml at load time, which is
# how zone and encounter IDs enter this project without ever being hardcoded.
#
# Only identity fields are carried here (wcl_zone_id, encounter_ids,
# verified). Display names, aliases, roles and validation targets stay in the
# authored config.
#
# Re-run discovery after a season change. Delete this file to return to the
# unverified state.
# ---------------------------------------------------------------------------
"""


def fetch_zones(client: GraphQLClient, *, expansion_id: int | None = None) -> dict[str, Any]:
    """Fetch the zone/encounter registry."""
    data = client.execute(
        load_query("world_zones"),
        {"expansionID": expansion_id},
        kind=f"world_zones_exp_{expansion_id}",
    )
    return data.get("worldData") or {}


def match_zones(registry: DungeonRegistry, zones: list[dict[str, Any]]) -> dict[str, Any]:
    """Match configured dungeon names against the live zone registry.

    Warcraft Logs models a Mythic+ season as **one zone whose encounters are
    the individual dungeons** -- not one zone per dungeon. An earlier version
    of this function compared dungeon names against zone names and matched
    nothing at all, which is how that was discovered.

    So encounters are searched first, and zone names only as a fallback (some
    zones are a single dungeon or raid). Each match records which way it was
    found, so the distinction stays visible in the findings.

    Ambiguity is reported rather than resolved: two encounters sharing a name
    across different seasons must be disambiguated by a human, because picking
    one silently would tie the whole corpus to the wrong season.
    """
    encounters_by_name: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    zones_by_name: dict[str, list[dict[str, Any]]] = {}

    for zone in zones:
        zone_name = str(zone.get("name") or "").strip().lower()
        if zone_name:
            zones_by_name.setdefault(zone_name, []).append(zone)
        for encounter in zone.get("encounters") or []:
            if not isinstance(encounter, dict):
                continue
            name = str(encounter.get("name") or "").strip().lower()
            if name:
                encounters_by_name.setdefault(name, []).append((zone, encounter))

    matched: dict[str, Any] = {}
    ambiguous: dict[str, list[int]] = {}
    unmatched: list[str] = []

    for entry in registry.dungeons:
        needles = [entry.display_name, *entry.aliases]

        # -- preferred: the dungeon is an encounter inside a season zone ----
        encounter_hits: dict[tuple[int, int], tuple[dict[str, Any], dict[str, Any]]] = {}
        for needle in needles:
            for zone, encounter in encounters_by_name.get(needle.strip().lower(), []):
                if zone.get("id") is not None and encounter.get("id") is not None:
                    encounter_hits[(int(zone["id"]), int(encounter["id"]))] = (zone, encounter)

        if len(encounter_hits) == 1:
            zone, encounter = next(iter(encounter_hits.values()))
            matched[entry.key] = {
                "key": entry.key,
                "display_name": entry.display_name,
                "match_kind": "encounter",
                "wcl_zone_id": int(zone["id"]),
                "wcl_zone_name": zone.get("name"),
                "encounter_ids": [int(encounter["id"])],
                "encounter_names": [str(encounter.get("name"))],
                "expansion": zone.get("expansion"),
                "partitions": zone.get("partitions"),
                "difficulties": zone.get("difficulties"),
                "verified": True,
            }
            continue
        if len(encounter_hits) > 1:
            ambiguous[entry.key] = sorted({zid for zid, _ in encounter_hits})
            continue

        # -- fallback: the dungeon is its own zone --------------------------
        zone_hits: dict[int, dict[str, Any]] = {}
        for needle in needles:
            for zone in zones_by_name.get(needle.strip().lower(), []):
                if zone.get("id") is not None:
                    zone_hits[int(zone["id"])] = zone

        if not zone_hits:
            unmatched.append(entry.display_name)
            continue
        if len(zone_hits) > 1:
            ambiguous[entry.key] = sorted(zone_hits)
            continue

        zone = next(iter(zone_hits.values()))
        matched[entry.key] = {
            "key": entry.key,
            "display_name": entry.display_name,
            "match_kind": "zone",
            "wcl_zone_id": int(zone["id"]),
            "wcl_zone_name": zone.get("name"),
            "encounter_ids": [
                int(e["id"])
                for e in (zone.get("encounters") or [])
                if isinstance(e, dict) and e.get("id")
            ],
            "encounter_names": [
                str(e.get("name")) for e in (zone.get("encounters") or []) if isinstance(e, dict)
            ],
            "expansion": zone.get("expansion"),
            "partitions": zone.get("partitions"),
            "difficulties": zone.get("difficulties"),
            "verified": True,
        }

    # The season zone is only unambiguous when every match agrees on one zone.
    season_zone_ids = {item["wcl_zone_id"] for item in matched.values()}
    return {
        "matched": matched,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "zones_seen": len(zones),
        "season_zone_id": season_zone_ids.pop() if len(season_zone_ids) == 1 else None,
        "season_zone_ids_seen": sorted(season_zone_ids) if len(season_zone_ids) > 1 else [],
    }


def build_overlay(result: dict[str, Any], registry: DungeonRegistry) -> dict[str, Any]:
    """Build the overlay document from a match result."""
    entries = [
        {
            "key": item["key"],
            "wcl_zone_id": item["wcl_zone_id"],
            "encounter_ids": item["encounter_ids"],
            "verified": True,
        }
        for item in result["matched"].values()
    ]
    all_verified = len(entries) == len(registry.dungeons) and not result["unmatched"]
    season: dict[str, Any] = {
        "id": registry.season_id,
        # The season is only "verified" once every configured dungeon resolved
        # to exactly one live encounter or zone.
        "verified": bool(all_verified),
    }
    if result.get("season_zone_id") is not None:
        season["wcl_mplus_zone_id"] = result["season_zone_id"]
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "season": season,
        "dungeons": entries,
    }


def write_overlay(path: Path, overlay: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(overlay, sort_keys=False, default_flow_style=False)
    path.write_text(OVERLAY_HEADER + body, encoding="utf-8")
    logger.info("Wrote dungeon discovery overlay to %s", path)
    return path


def discover_dungeons(
    client: GraphQLClient,
    registry: DungeonRegistry,
    *,
    expansion_id: int | None = None,
    write: bool = False,
    overlay_path: Path | None = None,
) -> dict[str, Any]:
    """Discover zone/encounter IDs and optionally persist them."""
    world = fetch_zones(client, expansion_id=expansion_id)
    zones = [z for z in (world.get("zones") or []) if isinstance(z, dict)]
    result = match_zones(registry, zones)
    result["expansions"] = [
        {"id": e.get("id"), "name": e.get("name")}
        for e in (world.get("expansions") or [])
        if isinstance(e, dict)
    ]
    if write and result["matched"]:
        target = overlay_path or registry.path.with_name("dungeons.discovered.yml")
        result["overlay_written"] = str(write_overlay(target, build_overlay(result, registry)))
    elif write:
        result["overlay_written"] = None
    return result
