"""Track A candidates: exact or explicitly lossy event timelines.

Every candidate has an encoder **and** a decoder, and the round trip is a test.
A format whose decoder is not written is not a candidate -- that rule is what
stops a plausible-looking notation being adopted on the strength of one
hand-written example.

Each encoded document carries its version and its dictionary hash. A decoder
that cannot match either refuses rather than guessing: a format mismatch must
fail loudly instead of resolving every symbol to a valid but wrong ability,
which produces plausible nonsense and no error at all.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from . import SCL_VERSION
from .dictionaries import Dictionaries
from .model import TimelineEvent


class DecodeError(Exception):
    """A document could not be decoded, and guessing was refused."""


@dataclass
class Encoded:
    """One encoded timeline, with everything a decoder needs to check itself."""

    candidate: str
    version: str
    text: str
    legend_text: str
    dictionary_hash: str
    event_count: int

    @property
    def full_text(self) -> str:
        """Legend and stream together, as a model would actually receive it."""
        return f"{self.legend_text}\n{self.text}" if self.legend_text else self.text


def dictionary_hash(dictionaries: Dictionaries) -> str:
    """Content hash of the symbol tables.

    Candidate G's code table is corpus-derived, so frequency ranks shift as the
    corpus grows. A decoder holding an older table would resolve every symbol to
    a valid but wrong ability. Hashing the table turns that into a refusal.
    """
    payload = json.dumps(dictionaries.legend(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _check(encoded: Encoded, dictionaries: Dictionaries) -> None:
    if encoded.version != SCL_VERSION:
        raise DecodeError(
            f"Document is {encoded.version!r}, decoder is {SCL_VERSION!r}. "
            "Refusing to decode: a version mismatch that decodes anyway produces "
            "plausible nonsense rather than an error."
        )
    current = dictionary_hash(dictionaries)
    if encoded.dictionary_hash != current:
        raise DecodeError(
            f"Dictionary hash {encoded.dictionary_hash} does not match {current}. "
            "The symbol table changed since this was encoded; every symbol would "
            "resolve to a valid but wrong entry."
        )


# ---------------------------------------------------------------------------
# A -- verbose JSON, the baseline every ratio is measured against
# ---------------------------------------------------------------------------


def encode_a(events: list[TimelineEvent], dictionaries: Dictionaries) -> Encoded:
    rows = [
        {
            "timestamp": e.rel_ms,
            "event": e.type,
            "source": dictionaries.names.get(dictionaries.symbol_for_source(e), "unknown"),
            "sourceInstance": e.source_instance,
            "ability": dictionaries.names.get(
                dictionaries.abilities.get(e.ability_game_id or -1, ""), None
            ),
            "target": dictionaries.names.get(dictionaries.symbol_for_target(e), None),
            "targetInstance": e.target_instance,
            "amount": e.amount,
            # Identity is carried explicitly: the baseline must be reversible
            # too, or every ratio is measured against something lossy.
            "sourceID": e.source_id,
            "targetID": e.target_id,
            "abilityGameID": e.ability_game_id,
        }
        for e in events
    ]
    return Encoded(
        candidate="A",
        version=SCL_VERSION,
        text=json.dumps(rows, indent=2),
        legend_text="",
        dictionary_hash=dictionary_hash(dictionaries),
        event_count=len(events),
    )


def decode_a(encoded: Encoded, dictionaries: Dictionaries) -> list[TimelineEvent]:
    _check(encoded, dictionaries)
    return [
        TimelineEvent(
            rel_ms=int(r["timestamp"]),
            type=str(r["event"]),
            source_id=r["sourceID"],
            source_instance=r["sourceInstance"],
            target_id=r["targetID"],
            target_instance=r["targetInstance"],
            ability_game_id=r["abilityGameID"],
            amount=r["amount"],
        )
        for r in json.loads(encoded.text)
    ]


# ---------------------------------------------------------------------------
# B -- compact JSON: symbols and time deltas, still parseable as JSON
# ---------------------------------------------------------------------------


def encode_b(events: list[TimelineEvent], dictionaries: Dictionaries) -> Encoded:
    rows = []
    previous = 0
    for index, e in enumerate(events):
        delta = e.rel_ms if index == 0 else e.rel_ms - previous
        previous = e.rel_ms
        row: dict[str, Any] = {
            "dt": delta,
            "s": dictionaries.symbol_for_source(e),
            "e": dictionaries.types.get(e.type, e.type),
        }
        if e.ability_game_id is not None:
            row["a"] = dictionaries.abilities[e.ability_game_id]
        if e.target_id is not None:
            row["t"] = dictionaries.symbol_for_target(e)
        if e.amount is not None:
            row["v"] = e.amount
        rows.append(row)
    return Encoded(
        candidate="B",
        version=SCL_VERSION,
        text="\n".join(json.dumps(r, separators=(",", ":")) for r in rows),
        legend_text=json.dumps(dictionaries.legend(), separators=(",", ":")),
        dictionary_hash=dictionary_hash(dictionaries),
        event_count=len(events),
    )


def decode_b(encoded: Encoded, dictionaries: Dictionaries) -> list[TimelineEvent]:
    _check(encoded, dictionaries)
    out: list[TimelineEvent] = []
    clock = 0
    for line in encoded.text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        clock = row["dt"] if not out else clock + row["dt"]
        source_id, source_instance = dictionaries.resolve_actor(row["s"])
        target_id, target_instance = (
            dictionaries.resolve_actor(row["t"]) if "t" in row else (None, None)
        )
        out.append(
            TimelineEvent(
                rel_ms=clock,
                type=dictionaries.resolve_type(row["e"]),
                source_id=source_id,
                source_instance=source_instance,
                target_id=target_id,
                target_instance=target_instance,
                ability_game_id=dictionaries.resolve_ability(row.get("a", "-")),
                amount=row.get("v"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# C -- line symbolic: one line per event, every field explicit
# ---------------------------------------------------------------------------


def encode_c(events: list[TimelineEvent], dictionaries: Dictionaries) -> Encoded:
    lines = []
    previous = 0
    for index, e in enumerate(events):
        delta = e.rel_ms if index == 0 else e.rel_ms - previous
        previous = e.rel_ms
        ability = dictionaries.abilities.get(e.ability_game_id or -1, "-")
        target = dictionaries.symbol_for_target(e) if e.target_id is not None else "-"
        amount = "-" if e.amount is None else str(e.amount)
        lines.append(
            f"+{delta} {dictionaries.symbol_for_source(e)} "
            f"{dictionaries.types.get(e.type, e.type)} {ability}>{target} {amount}"
        )
    return Encoded(
        candidate="C",
        version=SCL_VERSION,
        text="\n".join(lines),
        legend_text=dictionaries.legend_text(),
        dictionary_hash=dictionary_hash(dictionaries),
        event_count=len(events),
    )


def decode_c(encoded: Encoded, dictionaries: Dictionaries) -> list[TimelineEvent]:
    _check(encoded, dictionaries)
    out: list[TimelineEvent] = []
    clock = 0
    for line in encoded.text.splitlines():
        if not line.strip():
            continue
        delta_text, source_symbol, type_code, pair, amount_text = line.split(" ")
        delta = int(delta_text.lstrip("+"))
        clock = delta if not out else clock + delta
        ability_symbol, _, target_symbol = pair.partition(">")
        source_id, source_instance = dictionaries.resolve_actor(source_symbol)
        target_id, target_instance = (
            (None, None) if target_symbol == "-" else dictionaries.resolve_actor(target_symbol)
        )
        out.append(
            TimelineEvent(
                rel_ms=clock,
                type=dictionaries.resolve_type(type_code),
                source_id=source_id,
                source_instance=source_instance,
                target_id=target_id,
                target_instance=target_instance,
                ability_game_id=dictionaries.resolve_ability(ability_symbol),
                amount=None if amount_text == "-" else int(amount_text),
            )
        )
    return out


# ---------------------------------------------------------------------------
# D -- domain-aware: a declared default target, exceptions always explicit
# ---------------------------------------------------------------------------


def _default_target(events: list[TimelineEvent]) -> tuple[int | None, int | None] | None:
    """The most common target, if there is one to suppress."""
    counts: dict[tuple[int | None, int | None], int] = {}
    for e in events:
        if e.target_id is None:
            continue
        counts[e.target_key] = counts.get(e.target_key, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: (counts[k], str(k)))


def encode_d(events: list[TimelineEvent], dictionaries: Dictionaries) -> Encoded:
    """Suppress the default target; every exception carries its own.

    Reversibility here is a property of encoding every exception, not of the
    default being common. A 20% exception rate is exactly as lossless as a 2%
    one; the exception rate decides the *saving*, which is what the benchmark
    measures, and the comprehension test decides whether the saving is worth it.
    """
    default = _default_target(events)
    default_symbol = (
        dictionaries._actor_symbol(*default) if default is not None else None  # noqa: SLF001
    )
    lines = []
    previous = 0
    for index, e in enumerate(events):
        delta = e.rel_ms if index == 0 else e.rel_ms - previous
        previous = e.rel_ms
        ability = dictionaries.abilities.get(e.ability_game_id or -1, "-")
        amount = "-" if e.amount is None else str(e.amount)
        parts = [
            f"+{delta}",
            dictionaries.symbol_for_source(e),
            dictionaries.types.get(e.type, e.type),
            ability,
        ]
        if e.target_id is None:
            parts.append(">-")  # explicitly no target, never implied
        elif default is not None and e.target_key == default:
            pass  # the declared default: suppressed, recoverable by the header
        else:
            parts.append(">" + dictionaries.symbol_for_target(e))
        parts.append(amount)
        lines.append(" ".join(parts))

    header = f"@DEFAULT_TARGET={default_symbol}" if default_symbol else "@DEFAULT_TARGET=none"
    return Encoded(
        candidate="D",
        version=SCL_VERSION,
        text="\n".join([header, *lines]),
        legend_text=dictionaries.legend_text(),
        dictionary_hash=dictionary_hash(dictionaries),
        event_count=len(events),
    )


def decode_d(encoded: Encoded, dictionaries: Dictionaries) -> list[TimelineEvent]:
    _check(encoded, dictionaries)
    lines = encoded.text.splitlines()
    if not lines or not lines[0].startswith("@DEFAULT_TARGET="):
        raise DecodeError("Candidate D requires a @DEFAULT_TARGET header.")
    default_symbol = lines[0].split("=", 1)[1]
    default = (
        (None, None) if default_symbol == "none" else dictionaries.resolve_actor(default_symbol)
    )

    out: list[TimelineEvent] = []
    clock = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(" ")
        delta = int(parts[0].lstrip("+"))
        clock = delta if not out else clock + delta
        source_id, source_instance = dictionaries.resolve_actor(parts[1])
        type_code, ability_symbol = parts[2], parts[3]
        rest = parts[4:]
        if rest and rest[0].startswith(">"):
            target_symbol = rest[0][1:]
            amount_text = rest[1]
            target = (
                (None, None) if target_symbol == "-" else dictionaries.resolve_actor(target_symbol)
            )
        else:
            amount_text = rest[0]
            target = default
        out.append(
            TimelineEvent(
                rel_ms=clock,
                type=dictionaries.resolve_type(type_code),
                source_id=source_id,
                source_instance=source_instance,
                target_id=target[0],
                target_instance=target[1],
                ability_game_id=dictionaries.resolve_ability(ability_symbol),
                amount=None if amount_text == "-" else int(amount_text),
            )
        )
    return out


# ---------------------------------------------------------------------------
# E -- integer rows: a storage lower bound, NOT an LLM candidate
# ---------------------------------------------------------------------------


def encode_e(events: list[TimelineEvent], dictionaries: Dictionaries) -> Encoded:
    """Numeric rows. Kept only as a floor for the size comparison.

    Parquet does this better and a model should never be asked to read it, so
    it is excluded from the comprehension benchmark by design rather than by
    oversight.
    """
    type_index = {name: i for i, name in enumerate(sorted(dictionaries.types))}
    actor_index: dict[tuple[int | None, int | None], int] = {}
    for e in events:
        for key in (e.source_key, e.target_key):
            if key[0] is not None and key not in actor_index:
                actor_index[key] = len(actor_index)
    ability_index = {a: i for i, a in enumerate(sorted(dictionaries.abilities))}

    lines = []
    previous = 0
    for index, e in enumerate(events):
        delta = e.rel_ms if index == 0 else e.rel_ms - previous
        previous = e.rel_ms
        lines.append(
            ",".join(
                str(v)
                for v in (
                    delta,
                    actor_index.get(e.source_key, -1),
                    type_index.get(e.type, -1),
                    ability_index.get(e.ability_game_id, -1)
                    if e.ability_game_id is not None
                    else -1,
                    actor_index.get(e.target_key, -1) if e.target_id is not None else -1,
                    -1 if e.amount is None else e.amount,
                )
            )
        )
    legend = {
        "actors": {str(i): list(k) for k, i in actor_index.items()},
        "types": {str(i): n for n, i in type_index.items()},
        "abilities": {str(i): a for a, i in ability_index.items()},
    }
    return Encoded(
        candidate="E",
        version=SCL_VERSION,
        text="\n".join(lines),
        legend_text=json.dumps(legend, separators=(",", ":")),
        dictionary_hash=dictionary_hash(dictionaries),
        event_count=len(events),
    )


def decode_e(encoded: Encoded, dictionaries: Dictionaries) -> list[TimelineEvent]:
    _check(encoded, dictionaries)
    legend = json.loads(encoded.legend_text)
    actors = {int(i): tuple(k) for i, k in legend["actors"].items()}
    types = {int(i): n for i, n in legend["types"].items()}
    abilities = {int(i): a for i, a in legend["abilities"].items()}

    out: list[TimelineEvent] = []
    clock = 0
    for line in encoded.text.splitlines():
        if not line.strip():
            continue
        delta, source, type_id, ability, target, amount = (int(v) for v in line.split(","))
        clock = delta if not out else clock + delta
        source_key = actors.get(source, (None, None))
        target_key = actors.get(target, (None, None)) if target >= 0 else (None, None)
        out.append(
            TimelineEvent(
                rel_ms=clock,
                type=types.get(type_id, "?"),
                source_id=source_key[0],
                source_instance=source_key[1],
                target_id=target_key[0],
                target_instance=target_key[1],
                ability_game_id=abilities.get(ability) if ability >= 0 else None,
                amount=None if amount < 0 else amount,
            )
        )
    return out


# ---------------------------------------------------------------------------
# G -- frequency-aware shorthand
# ---------------------------------------------------------------------------


def encode_g(events: list[TimelineEvent], dictionaries: Dictionaries) -> Encoded:
    """Candidate C's shape over a frequency-ordered ability table.

    The table must be built with `frequency_ordered=True`, which puts the
    shortest codes on the commonest abilities. Whether that is actually cheaper
    is a question about the tokenizer, not about character counts -- which is
    why it is a candidate and not an optimization.
    """
    encoded = encode_c(events, dictionaries)
    return Encoded(
        candidate="G",
        version=encoded.version,
        text=encoded.text,
        legend_text=encoded.legend_text,
        dictionary_hash=encoded.dictionary_hash,
        event_count=encoded.event_count,
    )


def decode_g(encoded: Encoded, dictionaries: Dictionaries) -> list[TimelineEvent]:
    return decode_c(encoded, dictionaries)


CANDIDATES = {
    "A": (encode_a, decode_a),
    "B": (encode_b, decode_b),
    "C": (encode_c, decode_c),
    "D": (encode_d, decode_d),
    "E": (encode_e, decode_e),
    "G": (encode_g, decode_g),
}

#: Candidates a model is meant to read. E is a storage floor only.
LLM_CANDIDATES = ("A", "B", "C", "D", "G")
