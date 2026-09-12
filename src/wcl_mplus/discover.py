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
    """Match configured dungeon names against live zone names.

    Matching is by name, case-insensitively, across display names and aliases.
    Ambiguity is reported rather than resolved arbitrarily: two zones with the
    same name (a re-release, say) must be disambiguated by a human.
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    for zone in zones:
        name = str(zone.get("name") or "").strip().lower()
        if name:
            by_name.setdefault(name, []).append(zone)

    matched: dict[str, dict[str, Any]] = {}
    ambiguous: dict[str, list[int]] = {}
    unmatched: list[str] = []

    for entry in registry.dungeons:
        candidates: list[dict[str, Any]] = []
        for needle in [entry.display_name, *entry.aliases]:
            candidates.extend(by_name.get(needle.strip().lower(), []))
        # De-duplicate by zone id, since aliases can hit the same zone.
        unique: dict[int, dict[str, Any]] = {
            int(z["id"]): z for z in candidates if z.get("id") is not None
        }
        if not unique:
            unmatched.append(entry.display_name)
            continue
        if len(unique) > 1:
            ambiguous[entry.key] = sorted(unique)
            continue
        zone = next(iter(unique.values()))
        matched[entry.key] = {
            "key": entry.key,
            "display_name": entry.display_name,
            "wcl_zone_id": int(zone["id"]),
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

    return {
        "matched": matched,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "zones_seen": len(zones),
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
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "season": {
            "id": registry.season_id,
            # The season is only "verified" once every configured dungeon
            # resolved to exactly one live zone.
            "verified": bool(all_verified),
        },
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
