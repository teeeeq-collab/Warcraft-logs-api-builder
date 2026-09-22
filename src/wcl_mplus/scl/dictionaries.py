"""Multi-level symbol dictionaries.

Giving a player's abilities new codes in every pull is both wasteful and
confusing for a model reading a whole run, so scope is explicit:

| Scope         | Contents                                   | Stable across |
|---------------|--------------------------------------------|---------------|
| dungeon/spec  | NPC species, NPC abilities, spec vocabulary| the export    |
| run           | the five players, their abilities, items   | the run       |
| pull          | enemy instances `E1..En`                   | the pull      |

**Only enemy instances are genuinely pull-local.** The same species in two
pulls is two different sets of creatures, and sharing instance codes between
them would assert a continuity that does not exist.

Player aliasing has two candidate schemes and the benchmark decides between
them: group order (`P1`-`P5`) and role order (`T`, `H`, `D1`-`D3`). Role
aliases carry meaning a model already has priors about, which may help
comprehension more than it costs in tokens -- a hypothesis, not a conclusion.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .model import TimelineEvent

PLAYER_SCHEMES = ("group", "role")

#: Role order used by the `role` scheme. Anything unrecognised falls through to
#: the DPS positions in roster order, so an unknown role never collides.
_ROLE_ALIASES = {"tank": "T", "healer": "H"}


@dataclass
class Dictionaries:
    """Symbol tables for one encoding, at three scopes."""

    #: (actor_id) -> "P1".."P5" or "T"/"H"/"D1".."D3"
    players: dict[int, str] = field(default_factory=dict)
    #: (actor_id, instance) -> "E1".."En", pull-local
    enemies: dict[tuple[int | None, int | None], str] = field(default_factory=dict)
    #: ability game id -> "A0", "A1", ... (export-scoped)
    abilities: dict[int, str] = field(default_factory=dict)
    #: event type -> single letter
    types: dict[str, str] = field(default_factory=dict)
    #: display names, kept beside the codes rather than inside the stream
    names: dict[str, str] = field(default_factory=dict)
    player_scheme: str = "group"

    def symbol_for_source(self, event: TimelineEvent) -> str:
        return self._actor_symbol(event.source_id, event.source_instance)

    def symbol_for_target(self, event: TimelineEvent) -> str:
        return self._actor_symbol(event.target_id, event.target_instance)

    def _actor_symbol(self, actor_id: int | None, instance: int | None) -> str:
        if actor_id is None:
            return "?"
        if actor_id in self.players:
            return self.players[actor_id]
        return self.enemies.get((actor_id, instance), f"X{actor_id}")

    def resolve_actor(self, symbol: str) -> tuple[int | None, int | None]:
        """Symbol back to (actor_id, instance). The decoder's half of the map."""
        if symbol == "?":
            return (None, None)
        for actor_id, code in self.players.items():
            if code == symbol:
                return (actor_id, None)
        for key, code in self.enemies.items():
            if code == symbol:
                return key
        if symbol.startswith("X"):
            try:
                return (int(symbol[1:]), None)
            except ValueError:
                return (None, None)
        return (None, None)

    def resolve_ability(self, symbol: str) -> int | None:
        if symbol in ("-", ""):
            return None
        for game_id, code in self.abilities.items():
            if code == symbol:
                return game_id
        return None

    def resolve_type(self, symbol: str) -> str:
        for name, code in self.types.items():
            if code == symbol:
                return name
        return symbol

    def legend(self) -> dict[str, Any]:
        """The legend a decoder needs, and a reader can check by eye."""
        return {
            "player_scheme": self.player_scheme,
            "players": {code: self.names.get(code, code) for code in self.players.values()},
            "enemies": {code: self.names.get(code, code) for code in self.enemies.values()},
            "abilities": {code: self.names.get(code, code) for code in self.abilities.values()},
            "types": {code: name for name, code in self.types.items()},
        }

    def legend_text(self) -> str:
        """Compact legend for line-oriented formats."""
        parts = [f"#v scheme={self.player_scheme}"]
        if self.players:
            parts.append("#P " + " ".join(sorted(self.players.values())))
        if self.enemies:
            parts.append(
                "#E " + " ".join(f"{c}={self.names.get(c, c)}" for c in self.enemies.values())
            )
        if self.abilities:
            parts.append(
                "#A " + " ".join(f"{c}={self.names.get(c, c)}" for c in self.abilities.values())
            )
        if self.types:
            parts.append("#T " + " ".join(f"{c}={n}" for n, c in self.types.items()))
        return "\n".join(parts)


def _type_code(name: str, taken: set[str]) -> str:
    """One letter per event type, first-come, falling back to two."""
    options = (
        name[:1].upper(),
        name[:1].lower(),
        *(f"{name[:1].upper()}{i}" for i in range(10)),
    )
    for candidate in options:
        if candidate not in taken:
            return candidate
    return name


def build(
    events: Iterable[TimelineEvent],
    *,
    player_roles: dict[int, str] | None = None,
    ability_names: dict[int, str] | None = None,
    actor_names: dict[int, str] | None = None,
    player_scheme: str = "group",
    frequency_ordered: bool = False,
) -> Dictionaries:
    """Build symbol tables over a timeline.

    `frequency_ordered` assigns the shortest ability codes to the most frequent
    abilities -- candidate G. The criterion there is measured token cost, not
    character count, so the ordering is produced here and the benchmark decides
    whether it was worth anything.
    """
    if player_scheme not in PLAYER_SCHEMES:
        raise ValueError(f"Unknown player scheme {player_scheme!r}; expected {PLAYER_SCHEMES}.")

    events = list(events)
    player_roles = player_roles or {}
    ability_names = ability_names or {}
    actor_names = actor_names or {}

    dictionaries = Dictionaries(player_scheme=player_scheme)

    # Players, in roster order. Deterministic: the same timeline always
    # produces the same symbols, or two exports of one run would disagree.
    player_ids = sorted(player_roles)
    if player_scheme == "role":
        dps = 0
        for actor_id in player_ids:
            role = str(player_roles.get(actor_id, "")).lower()
            if role in _ROLE_ALIASES and _ROLE_ALIASES[role] not in dictionaries.players.values():
                dictionaries.players[actor_id] = _ROLE_ALIASES[role]
            else:
                dps += 1
                dictionaries.players[actor_id] = f"D{dps}"
    else:
        for index, actor_id in enumerate(player_ids, start=1):
            dictionaries.players[actor_id] = f"P{index}"
    for actor_id, code in dictionaries.players.items():
        dictionaries.names[code] = actor_names.get(actor_id, f"actor-{actor_id}")

    # Enemy instances, pull-local, in first-appearance order.
    seen: list[tuple[int | None, int | None]] = []
    for event in events:
        for key in (event.source_key, event.target_key):
            holder = key[0]
            if holder is None or holder in dictionaries.players:
                continue
            if key not in seen:
                seen.append(key)
    for index, key in enumerate(seen, start=1):
        code = f"E{index}"
        dictionaries.enemies[key] = code
        base = actor_names.get(key[0] or -1, f"npc-{key[0]}")
        dictionaries.names[code] = base if key[1] is None else f"{base} #{key[1]}"

    # Abilities.
    counts: dict[int, int] = {}
    for event in events:
        ability = event.ability_game_id
        if ability is not None:
            counts[ability] = counts.get(ability, 0) + 1
    # Candidate G puts the most frequent abilities on the shortest codes. Ties
    # break by id so the table is deterministic -- a code table that varied
    # between two runs over one corpus would decode the same file differently.
    order = sorted(counts, key=lambda a: (-counts[a], a)) if frequency_ordered else sorted(counts)
    for index, game_id in enumerate(order):
        code = f"a{index}" if frequency_ordered else f"A{index}"
        dictionaries.abilities[game_id] = code
        dictionaries.names[code] = ability_names.get(game_id, f"ability-{game_id}")

    # Event types.
    taken: set[str] = set()
    for event in events:
        if event.type not in dictionaries.types:
            code = _type_code(event.type, taken)
            taken.add(code)
            dictionaries.types[event.type] = code

    return dictionaries
