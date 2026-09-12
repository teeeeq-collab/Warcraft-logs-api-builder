"""A synthetic stand-in for the Warcraft Logs v2 API.

IMPORTANT: this is **not** real API data and must never be mistaken for a
fixture. Its shapes are modelled on the API as described in the research
brief, which is exactly the thing the project refuses to treat as verified.

Its job is narrow and legitimate: exercise the collector's control flow
(introspection, query generation, pagination, error handling) with no network.
Whether the live schema really looks like this is what `wclmplus recon`
answers, and `tests/fixtures/` is where real sanitized responses land.

Where a test needs to prove behaviour under a *different* schema shape, the
simulator is constructed with fields removed -- that is how "the live schema
wins" is tested.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

REPORT_CODE = "SimReport123456a"
FIGHT_ID = 7
KEYSTONE_LEVEL = 10

#: gameID shared by two copies of one NPC in pull 2 -- the instance-identity case.
DUPLICATE_NPC_GAME_ID = 210148

REPORT_FIELDS = {
    "code": "String!",
    "title": "String",
    "startTime": "Float!",
    "endTime": "Float!",
    "revision": "Int!",
    "gameVersion": "Int",
    "zone": "Zone",
    "visibility": "String!",
    "archiveStatus": "ReportArchiveStatus",
    "segments": "Int!",
    "owner": "User",
    "region": "Region",
    "fights": "[ReportFight]",
    "events": "ReportEventPaginator",
    "masterData": "ReportMasterData",
}

FIGHT_FIELDS = {
    "id": "Int!",
    "name": "String!",
    "startTime": "Float!",
    "endTime": "Float!",
    "encounterID": "Int!",
    "difficulty": "Int",
    "kill": "Boolean",
    "keystoneLevel": "Int",
    "keystoneAffixes": "[Int]",
    "keystoneBonus": "Int",
    "keystoneTime": "Int",
    "rating": "Int",
    "completeRaid": "Boolean!",
    "countReached": "Int",
    "countRequired": "Int",
    "averageItemLevel": "Float",
    "friendlyPlayers": "[Int]",
    "gameZone": "GameZone",
    "npcCountMap": "JSON",
    "lastPhase": "Int",
    "size": "Int",
    "fightPercentage": "Float",
    "bossPercentage": "Float",
}

PULL_FIELDS = {
    "id": "Int!",
    "name": "String!",
    "startTime": "Float!",
    "endTime": "Float!",
    "encounterID": "Int!",
    "kill": "Boolean",
    "x": "Int!",
    "y": "Int!",
    "boundingBox": "ReportMapBoundingBox!",
    "maps": "[ReportMap]",
}

PULL_NPC_FIELDS = {
    "id": "Int",
    "gameID": "Int",
    "minimumInstanceID": "Int",
    "maximumInstanceID": "Int",
    "minimumInstanceGroupID": "Int",
    "maximumInstanceGroupID": "Int",
}

EVENT_DATA_TYPES = [
    "All",
    "Buffs",
    "Casts",
    "CombatantInfo",
    "DamageDone",
    "DamageTaken",
    "Deaths",
    "Debuffs",
    "Dispels",
    "Healing",
    "Interrupts",
    "Resources",
    "Summons",
    "Threat",
]


def _type_ref(rendered: str) -> dict[str, Any]:
    """Build an introspection type reference from an SDL-ish string."""
    if rendered.endswith("!"):
        return {"kind": "NON_NULL", "name": None, "ofType": _type_ref(rendered[:-1])}
    if rendered.startswith("[") and rendered.endswith("]"):
        return {"kind": "LIST", "name": None, "ofType": _type_ref(rendered[1:-1])}
    kind = "SCALAR" if rendered in {"Int", "Float", "String", "Boolean", "JSON"} else "OBJECT"
    return {"kind": kind, "name": rendered, "ofType": None}


def _fields(mapping: dict[str, str], args: dict[str, list[str]] | None = None) -> list[dict]:
    args = args or {}
    return [
        {
            "name": name,
            "description": None,
            "isDeprecated": False,
            "args": [
                {"name": arg, "defaultValue": None, "type": _type_ref("Int")}
                for arg in args.get(name, [])
            ],
            "type": _type_ref(rendered),
        }
        for name, rendered in mapping.items()
    ]


class WclSimulator:
    """Serves plausible responses for the queries the collector sends.

    `drop_fields` removes fields from the advertised schema so tests can prove
    the collector degrades gracefully instead of sending invalid queries.
    """

    def __init__(
        self,
        *,
        drop_fields: dict[str, set[str]] | None = None,
        event_page_limit: int = 25,
        inclusive_cursor: bool = True,
        total_cast_events: int = 60,
        missing_types: set[str] | None = None,
        event_data_types: list[str] | None = None,
        unscoped_reports_allowed: bool = True,
    ) -> None:
        self.drop_fields = drop_fields or {}
        self.event_page_limit = event_page_limit
        self.inclusive_cursor = inclusive_cursor
        self.missing_types = missing_types or set()
        self.event_data_types = (
            event_data_types if event_data_types is not None else list(EVENT_DATA_TYPES)
        )
        #: Whether ReportData.reports answers without a guild/user scope.
        #: The live behaviour is unverified, so both outcomes are testable.
        self.unscoped_reports_allowed = unscoped_reports_allowed
        self.queries_seen: list[tuple[str, dict[str, Any]]] = []
        self.points_spent = 100.0
        self.cast_events = self._build_cast_events(total_cast_events)

    # -- data ------------------------------------------------------------

    def _build_cast_events(self, count: int) -> list[dict[str, Any]]:
        """Two copies of one NPC casting, so instance identity is observable."""
        events: list[dict[str, Any]] = []
        base = 10_000
        for index in range(count):
            instance = 1 if index % 2 == 0 else 2
            events.append(
                {
                    "timestamp": base + index * 500,
                    "type": "begincast" if index % 3 == 0 else "cast",
                    "sourceID": 42,
                    "sourceInstance": instance,
                    "targetID": 1,
                    "abilityGameID": 372107,
                    "fight": FIGHT_ID,
                }
            )
        # Three events on one identical millisecond, to exercise the boundary path.
        events.append(
            {
                "timestamp": base + 2_000,
                "type": "cast",
                "sourceID": 43,
                "sourceInstance": 1,
                "abilityGameID": 372108,
                "fight": FIGHT_ID,
            }
        )
        events.append(
            {
                "timestamp": base + 2_000,
                "type": "cast",
                "sourceID": 44,
                "sourceInstance": 1,
                "abilityGameID": 372108,
                "fight": FIGHT_ID,
            }
        )
        return sorted(events, key=lambda e: e["timestamp"])

    def _visible(self, type_name: str, mapping: dict[str, str]) -> dict[str, str]:
        dropped = self.drop_fields.get(type_name, set())
        return {k: v for k, v in mapping.items() if k not in dropped}

    # -- introspection ---------------------------------------------------

    def _type_list(self) -> dict[str, Any]:
        names = [
            ("Query", "OBJECT"),
            ("RateLimitData", "OBJECT"),
            ("ReportData", "OBJECT"),
            ("Report", "OBJECT"),
            ("ReportFight", "OBJECT"),
            ("ReportDungeonPull", "OBJECT"),
            ("ReportDungeonPullNPC", "OBJECT"),
            ("ReportMasterData", "OBJECT"),
            ("ReportActor", "OBJECT"),
            ("ReportAbility", "OBJECT"),
            ("ReportEventPaginator", "OBJECT"),
            ("EventDataType", "ENUM"),
            ("HostilityType", "ENUM"),
            ("WorldData", "OBJECT"),
            ("Zone", "OBJECT"),
            ("Encounter", "OBJECT"),
            ("CharacterData", "OBJECT"),
            ("Character", "OBJECT"),
            ("GuildData", "OBJECT"),
            ("Guild", "OBJECT"),
            ("ReportArchiveStatus", "OBJECT"),
            ("User", "OBJECT"),
            ("Region", "OBJECT"),
            ("GameZone", "OBJECT"),
            ("ReportMap", "OBJECT"),
            ("ReportMapBoundingBox", "OBJECT"),
            ("Expansion", "OBJECT"),
            ("Difficulty", "OBJECT"),
            ("Partition", "OBJECT"),
        ]
        return {
            "__schema": {
                "queryType": {"name": "Query"},
                "types": [
                    {"name": name, "kind": kind, "description": None}
                    for name, kind in names
                    if name not in self.missing_types
                ],
            }
        }

    def _type_detail(self, name: str) -> dict[str, Any]:
        if name in self.missing_types:
            return {"__type": None}
        table: dict[str, dict[str, Any]] = {
            "Report": {
                "fields": _fields(
                    self._visible("Report", REPORT_FIELDS),
                    {
                        "events": [
                            "startTime",
                            "endTime",
                            "dataType",
                            "hostilityType",
                            "limit",
                            "fightIDs",
                            "includeResources",
                            "translate",
                        ],
                        "fights": ["fightIDs", "encounterID", "killType", "translate"],
                    },
                )
            },
            "ReportFight": {"fields": _fields(self._visible("ReportFight", FIGHT_FIELDS))},
            "ReportDungeonPull": {
                "fields": _fields(
                    self._visible("ReportDungeonPull", PULL_FIELDS)
                    | {"enemyNPCs": "[ReportDungeonPullNPC]"}
                )
            },
            "ReportDungeonPullNPC": {
                "fields": _fields(self._visible("ReportDungeonPullNPC", PULL_NPC_FIELDS))
            },
            "ReportData": {
                "fields": _fields(
                    {"report": "Report", "reports": "ReportPagination"},
                    {
                        "report": ["code"],
                        "reports": [
                            "guildID",
                            "userID",
                            "zoneID",
                            "startTime",
                            "endTime",
                            "limit",
                            "page",
                        ],
                    },
                )
            },
            "RateLimitData": {
                "fields": _fields(
                    {
                        "limitPerHour": "Int!",
                        "pointsSpentThisHour": "Float!",
                        "pointsResetIn": "Int!",
                    }
                )
            },
            "ReportMasterData": {
                "fields": _fields(
                    {
                        "logVersion": "Int!",
                        "gameVersion": "Int",
                        "lang": "String",
                        "actors": "[ReportActor]",
                        "abilities": "[ReportAbility]",
                    }
                )
            },
            "ReportActor": {
                "fields": _fields(
                    {
                        "id": "Int",
                        "gameID": "Int",
                        "name": "String",
                        "type": "String",
                        "subType": "String",
                        "icon": "String",
                        "petOwner": "Int",
                    }
                )
            },
            "ReportAbility": {
                "fields": _fields(
                    {"gameID": "Float", "name": "String", "icon": "String", "type": "String"}
                )
            },
            "ReportEventPaginator": {
                "fields": _fields({"data": "JSON!", "nextPageTimestamp": "Float"})
            },
            "EventDataType": {
                "enumValues": [
                    {"name": v, "description": None, "isDeprecated": False}
                    for v in self.event_data_types
                ]
            },
            "HostilityType": {
                "enumValues": [
                    {"name": v, "description": None, "isDeprecated": False}
                    for v in ("Enemies", "Friendlies")
                ]
            },
            "WorldData": {
                "fields": _fields(
                    {"expansions": "[Expansion]", "zones": "[Zone]", "encounter": "Encounter"},
                    {"zones": ["expansion_id"]},
                )
            },
            "Zone": {
                "fields": _fields(
                    {
                        "id": "Int!",
                        "name": "String!",
                        "frozen": "Boolean!",
                        "encounters": "[Encounter]",
                        "difficulties": "[Difficulty]",
                        "partitions": "[Partition]",
                        "expansion": "Expansion!",
                    }
                )
            },
            "Encounter": {
                "fields": _fields(
                    {
                        "id": "Int!",
                        "name": "String!",
                        "characterRankings": "JSON",
                        "fightRankings": "JSON",
                    },
                    {"fightRankings": ["difficulty", "page", "metric"]},
                )
            },
            "Character": {
                "fields": _fields(
                    {"id": "Int!", "name": "String!", "recentReports": "ReportPagination"},
                    {"recentReports": ["limit", "page"]},
                )
            },
            "CharacterData": {
                "fields": _fields(
                    {"character": "Character"},
                    {"character": ["name", "serverSlug", "serverRegion"]},
                )
            },
            "GuildData": {
                "fields": _fields({"guild": "Guild"}, {"guild": ["id", "name", "serverSlug"]})
            },
            "Guild": {
                "fields": _fields(
                    {"id": "Int!", "name": "String!", "attendance": "GuildAttendancePagination"}
                )
            },
            "Query": {
                "fields": _fields(
                    {
                        "reportData": "ReportData",
                        "worldData": "WorldData",
                        "characterData": "CharacterData",
                        "guildData": "GuildData",
                        "rateLimitData": "RateLimitData",
                    }
                )
            },
            # Composite types reachable from the field wish-lists. Each needs a
            # sub-selection -- which the live API taught us the hard way.
            "ReportArchiveStatus": {
                "fields": _fields(
                    {"isArchived": "Boolean!", "isAccessible": "Boolean!", "archiveDate": "Int"}
                )
            },
            "User": {"fields": _fields({"id": "Int!", "name": "String!"})},
            "Region": {
                "fields": _fields(
                    {"id": "Int!", "compactName": "String!", "name": "String!", "slug": "String!"}
                )
            },
            "GameZone": {"fields": _fields({"id": "Int!", "name": "String"})},
            "ReportMap": {"fields": _fields({"id": "Int!"})},
            "ReportMapBoundingBox": {
                "fields": _fields({"minX": "Int!", "minY": "Int!", "maxX": "Int!", "maxY": "Int!"})
            },
            "Expansion": {"fields": _fields({"id": "Int!", "name": "String!"})},
            "Difficulty": {"fields": _fields({"id": "Int!", "name": "String!"})},
            "Partition": {
                "fields": _fields(
                    {
                        "id": "Int!",
                        "name": "String!",
                        "compactName": "String!",
                        "default": "Boolean!",
                    }
                )
            },
        }
        block = table.get(name)
        if block is None:
            return {"__type": None}
        return {
            "__type": {
                "name": name,
                "kind": "ENUM" if "enumValues" in block else "OBJECT",
                "description": None,
                "fields": block.get("fields"),
                "enumValues": block.get("enumValues"),
            }
        }

    # -- domain responses ------------------------------------------------

    def _report_metadata(self) -> dict[str, Any]:
        return {
            "reportData": {
                "report": {
                    "code": REPORT_CODE,
                    "title": "Tuesday keys",
                    "startTime": 1_768_000_000_000,
                    "endTime": 1_768_002_000_000,
                    "revision": 26,
                    "gameVersion": 1,
                    "zone": {"id": 44, "name": "Mythic+ Season 2"},
                    "visibility": "public",
                    "archiveStatus": {"isArchived": False, "isAccessible": True},
                    "segments": 5,
                    "owner": {"id": 99, "name": "SomeUploader"},
                    "region": {"id": 1, "name": "Europe", "slug": "EU"},
                }
            }
        }

    def _fights(self) -> dict[str, Any]:
        return {
            "reportData": {
                "report": {
                    "code": REPORT_CODE,
                    "startTime": 1_768_000_000_000,
                    "endTime": 1_768_002_000_000,
                    "fights": [
                        {
                            "id": FIGHT_ID,
                            "name": "Murder Row",
                            "startTime": 10_000.0,
                            "endTime": 1_600_000.0,
                            "encounterID": 0,
                            "difficulty": 10,
                            "kill": True,
                            "keystoneLevel": KEYSTONE_LEVEL,
                            "keystoneAffixes": [10, 152, 148],
                            "keystoneBonus": 2,
                            "keystoneTime": 1_590_000,
                            "rating": 212,
                            "completeRaid": False,
                            "countReached": 280,
                            "countRequired": 280,
                            "averageItemLevel": 642.4,
                            "friendlyPlayers": [1, 2, 3, 4, 5],
                            "gameZone": {"id": 2662, "name": "Murder Row"},
                            "npcCountMap": {str(DUPLICATE_NPC_GAME_ID): 2},
                            "lastPhase": 0,
                            "size": 5,
                            "fightPercentage": 0.0,
                            "bossPercentage": 0.0,
                        },
                        {
                            "id": 1,
                            "name": "Trash",
                            "startTime": 0.0,
                            "endTime": 5_000.0,
                            "encounterID": 0,
                            "difficulty": None,
                            "kill": None,
                            "keystoneLevel": None,
                            "keystoneAffixes": None,
                            "keystoneBonus": None,
                            "keystoneTime": None,
                            "rating": None,
                            "completeRaid": False,
                            "countReached": None,
                            "countRequired": None,
                            "averageItemLevel": None,
                            "friendlyPlayers": [1, 2],
                            "gameZone": None,
                            "npcCountMap": None,
                            "lastPhase": 0,
                            "size": 5,
                            "fightPercentage": None,
                            "bossPercentage": None,
                        },
                    ],
                }
            }
        }

    def _dungeon_pulls(self) -> dict[str, Any]:
        return {
            "reportData": {
                "report": {
                    "code": REPORT_CODE,
                    "fights": [
                        {
                            "id": FIGHT_ID,
                            "dungeonPulls": [
                                {
                                    "id": 1,
                                    "name": "First pack",
                                    "startTime": 10_000.0,
                                    "endTime": 42_000.0,
                                    "encounterID": 0,
                                    "kill": True,
                                    "x": 1500,
                                    "y": 2400,
                                    "boundingBox": {
                                        "minX": 1400,
                                        "minY": 2300,
                                        "maxX": 1600,
                                        "maxY": 2500,
                                    },
                                    "maps": [{"id": 2113}],
                                    "enemyNPCs": [
                                        {
                                            "id": 42,
                                            "gameID": DUPLICATE_NPC_GAME_ID,
                                            "minimumInstanceID": 1,
                                            "maximumInstanceID": 1,
                                            "minimumInstanceGroupID": 1,
                                            "maximumInstanceGroupID": 1,
                                        },
                                        {
                                            "id": 43,
                                            "gameID": 210149,
                                            "minimumInstanceID": 1,
                                            "maximumInstanceID": 2,
                                            "minimumInstanceGroupID": 1,
                                            "maximumInstanceGroupID": 1,
                                        },
                                    ],
                                },
                                {
                                    "id": 2,
                                    "name": "Second pack",
                                    "startTime": 60_000.0,
                                    "endTime": 95_000.0,
                                    "encounterID": 0,
                                    "kill": True,
                                    "x": 1800,
                                    "y": 2600,
                                    "boundingBox": {
                                        "minX": 1700,
                                        "minY": 2500,
                                        "maxX": 1900,
                                        "maxY": 2700,
                                    },
                                    "maps": [{"id": 2113}],
                                    "enemyNPCs": [
                                        # Same species twice: instance IDs 1 and 2.
                                        {
                                            "id": 42,
                                            "gameID": DUPLICATE_NPC_GAME_ID,
                                            "minimumInstanceID": 1,
                                            "maximumInstanceID": 2,
                                            "minimumInstanceGroupID": 1,
                                            "maximumInstanceGroupID": 2,
                                        },
                                    ],
                                },
                            ],
                        }
                    ],
                }
            }
        }

    def _master_data(self) -> dict[str, Any]:
        return {
            "reportData": {
                "report": {
                    "code": REPORT_CODE,
                    "masterData": {
                        "logVersion": 26,
                        "gameVersion": 1,
                        "lang": "en",
                        "actors": [
                            {
                                "id": 1,
                                "gameID": 0,
                                "name": "Tankadin",
                                "type": "Player",
                                "subType": "Protection",
                                "icon": "Paladin-Protection",
                                "petOwner": None,
                            },
                            {
                                "id": 2,
                                "gameID": 0,
                                "name": "Brewhealz",
                                "type": "Player",
                                "subType": "Mistweaver",
                                "icon": "Monk-Mistweaver",
                                "petOwner": None,
                            },
                            {
                                "id": 42,
                                "gameID": DUPLICATE_NPC_GAME_ID,
                                "name": "Shivan Punisher",
                                "type": "NPC",
                                "subType": "NPC",
                                "icon": "NPC",
                                "petOwner": None,
                            },
                            {
                                "id": 43,
                                "gameID": 210149,
                                "name": "Thunderhead",
                                "type": "NPC",
                                "subType": "NPC",
                                "icon": "NPC",
                                "petOwner": None,
                            },
                        ],
                        "abilities": [
                            {
                                "gameID": 372107.0,
                                "name": "Whirlwind",
                                "icon": "ability_whirlwind",
                                "type": "1",
                            },
                            {
                                "gameID": 372108.0,
                                "name": "Rolling Thunder",
                                "icon": "spell_nature_thunderclap",
                                "type": "8",
                            },
                        ],
                    },
                }
            }
        }

    def _events(self, variables: dict[str, Any]) -> dict[str, Any]:
        data_type = variables.get("dataType") or "All"
        start = float(variables.get("startTime") or 0)
        limit = int(variables.get("limit") or self.event_page_limit)
        limit = min(limit, self.event_page_limit)

        if data_type in ("Casts", "All"):
            pool = self.cast_events
        elif data_type == "Deaths":
            pool = [
                {
                    "timestamp": 90_000,
                    "type": "death",
                    "targetID": 3,
                    "abilityGameID": 372108,
                    "fight": FIGHT_ID,
                }
            ]
        elif data_type == "Debuffs":
            pool = [
                {
                    "timestamp": 20_000,
                    "type": "applydebuff",
                    "sourceID": 43,
                    "sourceInstance": 1,
                    "targetID": 2,
                    "abilityGameID": 372108,
                    "fight": FIGHT_ID,
                },
                {
                    "timestamp": 20_000,
                    "type": "applydebuff",
                    "sourceID": 43,
                    "sourceInstance": 1,
                    "targetID": 3,
                    "abilityGameID": 372108,
                    "fight": FIGHT_ID,
                },
                {
                    "timestamp": 24_500,
                    "type": "removedebuff",
                    "sourceID": 43,
                    "sourceInstance": 1,
                    "targetID": 2,
                    "abilityGameID": 372108,
                    "fight": FIGHT_ID,
                },
            ]
        elif data_type == "Dispels":
            pool = [
                {
                    "timestamp": 24_500,
                    "type": "dispel",
                    "sourceID": 2,
                    "targetID": 2,
                    "abilityGameID": 115450,
                    "extraAbilityGameID": 372108,
                    "fight": FIGHT_ID,
                }
            ]
        elif data_type == "Interrupts":
            pool = [
                {
                    "timestamp": 30_000,
                    "type": "interrupt",
                    "sourceID": 4,
                    "targetID": 42,
                    "targetInstance": 2,
                    "abilityGameID": 147362,
                    "extraAbilityGameID": 372107,
                    "fight": FIGHT_ID,
                }
            ]
        elif data_type == "DamageTaken":
            pool = [
                {
                    "timestamp": 31_000,
                    "type": "damage",
                    "sourceID": 42,
                    "sourceInstance": 1,
                    "targetID": 1,
                    "abilityGameID": 372107,
                    "amount": 184_231,
                    "absorbed": 12_000,
                    "hitType": 1,
                    "fight": FIGHT_ID,
                }
            ]
        else:
            pool = []

        remaining = [e for e in pool if e["timestamp"] >= start]
        page = remaining[:limit]
        if len(remaining) <= limit:
            next_ts = None
        else:
            last = page[-1]["timestamp"]
            next_ts = last if self.inclusive_cursor else last + 1
        return {
            "reportData": {
                "report": {
                    "events": {"data": [dict(e) for e in page], "nextPageTimestamp": next_ts}
                }
            }
        }

    def _world_zones(self) -> dict[str, Any]:
        """Zones as the live API shapes them.

        Two details are modelled from the real response, both of which broke
        an earlier version of this project:

        * **Expansions come back newest first.** Reporting code that took the
          last six entries printed the six OLDEST expansions and hid the
          current one entirely.
        * **A Mythic+ season is one zone whose encounters are the dungeons**,
          not one zone per dungeon. Matching dungeon names against zone names
          therefore matches nothing at all.
        """
        return {
            "worldData": {
                "expansions": [
                    {"id": 7, "name": "Midnight"},
                    {"id": 6, "name": "The War Within"},
                    {"id": 5, "name": "Dragonflight"},
                    {"id": 4, "name": "Shadowlands"},
                    {"id": 3, "name": "Battle for Azeroth"},
                    {"id": 2, "name": "Legion"},
                    {"id": 1, "name": "Warlords of Draenor"},
                    {"id": 0, "name": "Mists of Pandaria"},
                ],
                "zones": [
                    {
                        "id": 44,
                        "name": "Mythic+ Season 2",
                        "frozen": False,
                        "expansion": {"id": 7, "name": "Midnight"},
                        "encounters": [
                            {"id": 12801, "name": "Murder Row"},
                            {"id": 12802, "name": "Ruby Life Pools"},
                            {"id": 12803, "name": "The Blinding Vale"},
                            {"id": 12804, "name": "Den of Nalorakk"},
                            {"id": 12805, "name": "Fifth Dungeon"},
                            {"id": 12806, "name": "Sixth Dungeon"},
                            {"id": 12807, "name": "Seventh Dungeon"},
                            {"id": 12808, "name": "Eighth Dungeon"},
                        ],
                        "difficulties": [{"id": 10, "name": "Mythic+", "sizes": [5]}],
                        "partitions": [
                            {"id": 2, "name": "Season 2", "compactName": "S2", "default": True}
                        ],
                    },
                    {
                        "id": 43,
                        "name": "Mythic+ Season 1",
                        "frozen": True,
                        "expansion": {"id": 7, "name": "Midnight"},
                        "encounters": [{"id": 12701, "name": "Some Old Dungeon"}],
                        "difficulties": [{"id": 10, "name": "Mythic+", "sizes": [5]}],
                        "partitions": [
                            {"id": 1, "name": "Season 1", "compactName": "S1", "default": False}
                        ],
                    },
                    {
                        "id": 42,
                        "name": "A Raid Tier",
                        "frozen": False,
                        "expansion": {"id": 7, "name": "Midnight"},
                        "encounters": [{"id": 12601, "name": "Some Raid Boss"}],
                        "difficulties": [{"id": 5, "name": "Mythic", "sizes": [20]}],
                        "partitions": [],
                    },
                ],
            }
        }

    # -- transport -------------------------------------------------------

    #: Composite field name -> its type, for the validation check below.
    COMPOSITE_FIELDS = {
        "archiveStatus": "ReportArchiveStatus",
        "zone": "Zone",
        "gameZone": "GameZone",
        "owner": "User",
        "region": "Region",
        "maps": "ReportMap",
        "boundingBox": "ReportMapBoundingBox",
    }

    def _validate_selections(self, query: str) -> str | None:
        """Mimic the server's "must have a sub selection" validation.

        The live API rejected a bare `archiveStatus` with
        `Field "archiveStatus" of type "ReportArchiveStatus" must have a sub
        selection.`, which aborted recon partway through. Enforcing the same
        rule here keeps that class of bug caught offline.
        """
        # Strip `#` comments first: a real server parses the document and
        # never sees them, and the project's queries document their own
        # arguments in comments that mention field names in prose.
        body = "\n".join(line for line in query.splitlines() if not line.lstrip().startswith("#"))
        for field, type_name in self.COMPOSITE_FIELDS.items():
            if re.search(rf"\b{field}\b(?!\s*(?:\{{|\(|:))", body):
                return f'Field "{field}" of type "{type_name}" must have a sub selection.'
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        query = body.get("query") or ""
        variables = body.get("variables") or {}
        self.queries_seen.append((query, variables))
        self.points_spent += 1.0

        # Introspection queries name types, not fields, so skip the check.
        if "__schema" not in query and "__type(" not in query:
            problem = self._validate_selections(query)
            if problem:
                return httpx.Response(200, json={"errors": [{"message": problem}]})

        if "__schema" in query:
            return httpx.Response(200, json={"data": self._type_list()})
        if "__type(" in query:
            return httpx.Response(200, json={"data": self._type_detail(variables.get("name", ""))})
        if "rateLimitData" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rateLimitData": {
                            "limitPerHour": 3600,
                            "pointsSpentThisHour": self.points_spent,
                            "pointsResetIn": 1800,
                        }
                    }
                },
            )
        if "worldData" in query:
            return httpx.Response(200, json={"data": self._world_zones()})
        if "ReportsProbe" in query:
            if not self.unscoped_reports_allowed and not variables.get("zoneID"):
                return httpx.Response(
                    200,
                    json={"errors": [{"message": "You must specify a guildID or userID."}]},
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "reportData": {
                            "reports": {
                                "total": 812,
                                "per_page": 3,
                                "current_page": 1,
                                "has_more_pages": True,
                                "data": [
                                    {
                                        "code": f"Probe{index:012d}",
                                        "startTime": 1_768_000_000_000 + index,
                                        "endTime": 1_768_002_000_000 + index,
                                        "zone": {"id": 44, "name": "Mythic+ Season 2"},
                                    }
                                    for index in range(3)
                                ],
                            }
                        }
                    }
                },
            )
        if "masterData" in query:
            return httpx.Response(200, json={"data": self._master_data()})
        if "events(" in query:
            return httpx.Response(200, json={"data": self._events(variables)})
        if "dungeonPulls" in query:
            return httpx.Response(200, json={"data": self._dungeon_pulls()})
        if "fights {" in query or "fights(" in query:
            return httpx.Response(200, json={"data": self._fights()})
        if "report(code:" in query:
            return httpx.Response(200, json={"data": self._report_metadata()})
        return httpx.Response(200, json={"errors": [{"message": "simulator: unhandled query"}]})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))
