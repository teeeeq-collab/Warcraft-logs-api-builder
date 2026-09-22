"""The timeline an encoder encodes and a decoder must reproduce exactly.

Deliberately narrow. A format is only claimed to be reversible over the fields
it says it carries, so those fields are named here rather than left as
"whatever was in the row". Everything else stays in the database, where the
provenance chain can reach it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TimelineEvent:
    """One event, reduced to what a compact timeline claims to preserve."""

    rel_ms: int
    type: str
    source_id: int | None
    source_instance: int | None
    target_id: int | None
    target_instance: int | None
    ability_game_id: int | None
    amount: int | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TimelineEvent:
        return cls(
            rel_ms=int(row["rel_ms"]),
            type=str(row["type"]),
            source_id=_int_or_none(row.get("source_id")),
            source_instance=_int_or_none(row.get("source_instance")),
            target_id=_int_or_none(row.get("target_id")),
            target_instance=_int_or_none(row.get("target_instance")),
            ability_game_id=_int_or_none(row.get("ability_game_id")),
            amount=_int_or_none(row.get("amount")),
        )

    @property
    def source_key(self) -> tuple[int | None, int | None]:
        """Actor identity including its instance.

        Two copies of one NPC species are two creatures with separate
        timelines, and collapsing them is the single mistake this corpus was
        built to avoid. A NULL instance stays NULL -- it means the API did not
        say, not "copy 1".
        """
        return (self.source_id, self.source_instance)

    @property
    def target_key(self) -> tuple[int | None, int | None]:
        return (self.target_id, self.target_instance)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
