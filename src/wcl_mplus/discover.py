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
    compared dungeon names against zone names and matched nothing at all.

    Matching runs in two passes, because dungeons are reused across seasons
    and a name alone is not enough. Ruby Life Pools, for instance, is an
    encounter in Dragonflight Season 1 (zone 32), Dragonflight Season 4
    (zone 37) **and** Midnight Season 2 (zone 55):

    1. Match every dungeon that resolves to exactly one encounter, and infer
       the season zone from where those agree (or take it from the config's
       `season.wcl_mplus_zone_id` when set).
    2. Resolve the remaining ambiguous names *within that season zone*.

    That uses evidence from the rest of the season rather than guesswork. A
    name still ambiguous after pass 2 is reported, never resolved: choosing
    arbitrarily would tie the corpus to the wrong season's mechanics.
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

    def _describe(
        entry: Any, zone: dict[str, Any], encounter: dict[str, Any] | None, match_kind: str
    ) -> dict[str, Any]:
        if encounter is not None:
            encounter_ids = [int(encounter["id"])]
            encounter_names = [str(encounter.get("name"))]
        else:
            encounter_ids = [
                int(e["id"])
                for e in (zone.get("encounters") or [])
                if isinstance(e, dict) and e.get("id")
            ]
            encounter_names = [
                str(e.get("name")) for e in (zone.get("encounters") or []) if isinstance(e, dict)
            ]
        return {
            "key": entry.key,
            "display_name": entry.display_name,
            "match_kind": match_kind,
            "wcl_zone_id": int(zone["id"]),
            "wcl_zone_name": zone.get("name"),
            "encounter_ids": encounter_ids,
            "encounter_names": encounter_names,
            "expansion": zone.get("expansion"),
            "partitions": zone.get("partitions"),
            "difficulties": zone.get("difficulties"),
            "verified": True,
        }

    # -- pass 1: unambiguous matches ---------------------------------------
    matched: dict[str, Any] = {}
    pending: dict[str, dict[tuple[int, int], tuple[dict[str, Any], dict[str, Any]]]] = {}
    unmatched: list[str] = []
    zone_fallback: dict[str, dict[int, dict[str, Any]]] = {}

    for entry in registry.dungeons:
        needles = [entry.display_name, *entry.aliases]

        encounter_hits: dict[tuple[int, int], tuple[dict[str, Any], dict[str, Any]]] = {}
        for needle in needles:
            for zone, encounter in encounters_by_name.get(needle.strip().lower(), []):
                if zone.get("id") is not None and encounter.get("id") is not None:
                    encounter_hits[(int(zone["id"]), int(encounter["id"]))] = (zone, encounter)

        if len(encounter_hits) == 1:
            zone, encounter = next(iter(encounter_hits.values()))
            matched[entry.key] = _describe(entry, zone, encounter, "encounter")
            continue
        if len(encounter_hits) > 1:
            pending[entry.key] = encounter_hits
            continue

        zone_hits: dict[int, dict[str, Any]] = {}
        for needle in needles:
            for zone in zones_by_name.get(needle.strip().lower(), []):
                if zone.get("id") is not None:
                    zone_hits[int(zone["id"])] = zone
        if not zone_hits:
            unmatched.append(entry.display_name)
        elif len(zone_hits) == 1:
            matched[entry.key] = _describe(entry, next(iter(zone_hits.values())), None, "zone")
        else:
            zone_fallback[entry.key] = zone_hits

    # -- infer the season zone --------------------------------------------
    configured_zone = getattr(registry, "wcl_mplus_zone_id", None)
    season_zone_id: int | None
    season_zone_source: str | None
    if configured_zone is not None:
        season_zone_id = int(configured_zone)
        season_zone_source = "config"
    else:
        agreed = {item["wcl_zone_id"] for item in matched.values()}
        season_zone_id = agreed.pop() if len(agreed) == 1 else None
        season_zone_source = "inferred" if season_zone_id is not None else None

    # -- pass 2: resolve the ambiguous ones inside the season zone --------
    ambiguous: dict[str, list[int]] = {}
    entries_by_key = {entry.key: entry for entry in registry.dungeons}

    for key, hits in pending.items():
        in_season = {
            zone_encounter
            for zone_encounter in hits
            if season_zone_id is not None and zone_encounter[0] == season_zone_id
        }
        if len(in_season) == 1:
            zone, encounter = hits[next(iter(in_season))]
            item = _describe(entries_by_key[key], zone, encounter, "encounter")
            item["resolved_via_season_zone"] = True
            matched[key] = item
        else:
            ambiguous[key] = sorted({zone_id for zone_id, _ in hits})

    for key, zone_hits in zone_fallback.items():
        if season_zone_id is not None and season_zone_id in zone_hits:
            item = _describe(entries_by_key[key], zone_hits[season_zone_id], None, "zone")
            item["resolved_via_season_zone"] = True
            matched[key] = item
        else:
            ambiguous[key] = sorted(zone_hits)

    final_zones = {item["wcl_zone_id"] for item in matched.values()}
    return {
        "matched": matched,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "zones_seen": len(zones),
        "season_zone_id": season_zone_id
        if season_zone_id is not None
        else (final_zones.pop() if len(final_zones) == 1 else None),
        "season_zone_source": season_zone_source,
        "season_zone_ids_seen": sorted(final_zones) if len(final_zones) > 1 else [],
        "resolved_via_season_zone": sorted(
            k for k, v in matched.items() if v.get("resolved_via_season_zone")
        ),
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
