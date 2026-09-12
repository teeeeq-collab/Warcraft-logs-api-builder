"""Query loading and schema-safe query building.

Queries live in `queries/*.graphql` so they can be inspected and edited
without touching Python. Templates contain `__PLACEHOLDER__` tokens which are
replaced at runtime with the subset of desired fields that introspection
confirmed exists.

That indirection is the point: this project must never send a query naming a
field the live schema does not have (brief section 6). A missing field becomes
a recorded absence in `API_NOTES.md` instead of a GraphQL validation error
halfway through a collection run.

The field wish-lists below come from the research brief. They are *hopes*, not
facts, until `wclmplus recon` checks them.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from .redaction import RedactedError
from .settings import project_root

logger = logging.getLogger(__name__)


class QueryError(RedactedError):
    """A query file is missing, or a template was left unfilled."""


# ---------------------------------------------------------------------------
# Field wish-lists (unverified until recon runs)
# ---------------------------------------------------------------------------

#: Report-level scalars.
WANTED_REPORT_FIELDS = [
    "code",
    "title",
    "startTime",
    "endTime",
    "revision",
    "gameVersion",
    "zone",
    "visibility",
    "archiveStatus",
    "segments",
    "owner",
    "region",
]

#: Fight-level fields. The Mythic+ block (keystone*, rating, countReached,
#: countRequired, averageItemLevel, friendlyPlayers, dungeonPulls, gameZone,
#: npcCountMap) is what makes a fight a usable Mythic+ run record.
WANTED_FIGHT_FIELDS = [
    "id",
    "name",
    "startTime",
    "endTime",
    "encounterID",
    "difficulty",
    "kill",
    "keystoneLevel",
    "keystoneAffixes",
    "keystoneBonus",
    "keystoneTime",
    "rating",
    "completeRaid",
    "countReached",
    "countRequired",
    "averageItemLevel",
    "friendlyPlayers",
    "gameZone",
    "npcCountMap",
    "lastPhase",
    "size",
    "fightPercentage",
    "bossPercentage",
]

#: Dungeon-pull fields. Pull boundaries come from WCL, not from us.
WANTED_PULL_FIELDS = [
    "id",
    "name",
    "startTime",
    "endTime",
    "encounterID",
    "kill",
    "x",
    "y",
    "boundingBox",
    "maps",
]

#: Pull-NPC fields. `minimumInstanceID` / `maximumInstanceID` /
#: `*InstanceGroupID` are what keep two copies of the same NPC species apart.
#: Losing them would silently merge two cast timelines into one (section 14).
WANTED_PULL_NPC_FIELDS = [
    "id",
    "gameID",
    "minimumInstanceID",
    "maximumInstanceID",
    "minimumInstanceGroupID",
    "maximumInstanceGroupID",
]

#: Nested object fields requested as sub-selections when the parent exists.
#: A scalar-looking field that is actually an object needs a selection set, so
#: these are appended to the generated selection only if the parent is present.
NESTED_FIELD_SELECTIONS: dict[str, str] = {
    "zone": "zone { id name }",
    "gameZone": "gameZone { id name }",
    "owner": "owner { id name }",
    "region": "region { id name slug }",
    "friendlyPlayers": "friendlyPlayers",
    "maps": "maps { id }",
    "boundingBox": "boundingBox { minX minY maxX maxY }",
}

#: Event categories the mechanics profile wants. Checked against the live
#: `EventDataType` enum by recon; unknown values are dropped with a warning
#: rather than sent.
WANTED_EVENT_DATA_TYPES = [
    "Casts",
    "Debuffs",
    "Buffs",
    "Interrupts",
    "Dispels",
    "Deaths",
    "Summons",
    "DamageTaken",
    "DamageDone",
    "Healing",
    "Resources",
    "Threat",
    "CombatantInfo",
    "All",
]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def queries_dir() -> Path:
    return project_root() / "queries"


@lru_cache(maxsize=64)
def load_query(name: str) -> str:
    """Read a query file by stem name (without `.graphql`)."""
    path = queries_dir() / f"{name}.graphql"
    if not path.is_file():
        available = sorted(p.stem for p in queries_dir().glob("*.graphql"))
        raise QueryError(f"No query file {path.name!r}. Available: {', '.join(available)}")
    return path.read_text(encoding="utf-8")


def build_selection(fields: list[str]) -> str:
    """Render a GraphQL selection set from verified field names.

    Fields known to be object-typed are expanded to a sub-selection, because
    GraphQL rejects an object field selected without one.
    """
    if not fields:
        raise QueryError(
            "Refusing to build an empty selection set. Introspection found none of the "
            "desired fields, which means the schema changed shape -- inspect the live "
            "schema with `wclmplus schema-check` before collecting."
        )
    parts = [NESTED_FIELD_SELECTIONS.get(name, name) for name in fields]
    return "\n        ".join(parts)


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def render(template_name: str, substitutions: dict[str, list[str]]) -> str:
    """Fill `__PLACEHOLDER__` tokens in a query template with field lists.

    Substitution skips `#` comment lines. The templates document their own
    placeholders in comments, and rewriting those would both mangle the
    documentation and make the "unfilled placeholder" check meaningless.
    """
    lines = load_query(template_name).splitlines()
    rendered: list[str] = []
    filled: set[str] = set()

    for line in lines:
        if _is_comment(line):
            rendered.append(line)
            continue
        for placeholder, fields in substitutions.items():
            token = f"__{placeholder}__"
            if token in line:
                # Preserve the template's indentation for the whole block.
                indent = line[: len(line) - len(line.lstrip())]
                selection = build_selection(fields).replace("\n        ", "\n" + indent)
                line = line.replace(token, selection)
                filled.add(placeholder)
        rendered.append(line)

    missing = set(substitutions) - filled
    if missing:
        raise QueryError(
            f"Template {template_name!r} has no placeholder(s) for: {sorted(missing)}."
        )

    text = "\n".join(rendered)
    leftover = [
        line.strip()
        for line in text.splitlines()
        if not _is_comment(line) and line.strip().startswith("__") and line.strip().endswith("__")
    ]
    if leftover:
        raise QueryError(f"Template {template_name!r} left placeholders unfilled: {leftover}")
    return text
