"""De-identification for fixtures and exports.

The research questions need class, spec and role -- not names (brief section
51). Player names and realms are replaced with stable pseudonyms before
anything is written to a fixture or an export, while NPC and ability names are
kept because they are the subject of the research.

Stability matters: the same player must hash to the same pseudonym across
reports, otherwise duplicate-run detection by roster becomes impossible. A
fixed project salt gives that. It is deliberately **not** a secret and not a
security control -- these names are public on Warcraft Logs. It is a measure
to keep personal names out of files that get shared with an analyst.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

#: Fixed, non-secret salt. Changing it renames every pseudonym and breaks
#: cross-run roster matching, so treat it as part of the data format.
IDENTITY_SALT = "wcl-mplus-v1"

#: Identity scheme version. Bump this **and** the salt together if the
#: pseudonym construction ever changes, so a corpus can say which scheme its
#: `actors.name` values were written under instead of leaving two
#: incompatible generations of pseudonym indistinguishable. Databases written
#: under different schemes must not be merged without re-deriving names from
#: the raw cache; see ARCHITECTURE notes on identity stability.
IDENTITY_SCHEME_VERSION = 1

#: What a stored pseudonym looks like. Live WoW character names are letters
#: only, so no real name can collide with this shape -- which is what makes it
#: safe to accept an already-pseudonymized name as input.
_PSEUDONYM_RE = re.compile(r"^(?:player|pet|uploader)-[0-9a-f]{8,64}$")

#: Keys whose values are free-text identifiers of people or uploads.
_TEXT_IDENTITY_KEYS = {"title", "guild", "server", "serverName", "serverSlug", "realm"}


def salt_fingerprint(*, length: int = 12) -> str:
    """Fingerprint of the salt in force, for comparing two corpora.

    The salt is not secret -- the names it protects are public on Warcraft Logs
    -- but a fingerprint compares exactly and keeps anything credential-shaped
    out of the database file.
    """
    return hashlib.sha256(IDENTITY_SALT.encode()).hexdigest()[:length]


def identity_hash(value: str, *, length: int = 12) -> str:
    """Stable pseudonym for a personal identifier."""
    digest = hashlib.sha256(f"{IDENTITY_SALT}:{value.strip().lower()}".encode())
    return digest.hexdigest()[:length]


def pseudonym(value: str, prefix: str = "player") -> str:
    return f"{prefix}-{identity_hash(value)}"


def is_pseudonym(value: str) -> bool:
    """True if `value` is already a stored pseudonym rather than a real name."""
    return bool(_PSEUDONYM_RE.match(value.strip()))


def player_name_candidates(value: str) -> list[str]:
    """Stored `actors.name` values a user-supplied focus player could be under.

    The corpus never holds a clear character name: `normalize_actors` writes
    `pseudonym(name, "player")`. So a name typed on the command line has to be
    put through the same function before it can match anything, and the clear
    name never needs to reach the database or a diagnostic.

    Two spellings are accepted because both are things a person actually has
    to hand:

    * a character name, optionally with a realm suffix (``Inomrah-Draenor``).
      WCL stores the character name alone in `masterData`, so the bare name is
      tried as well as the full string;
    * a pseudonym copied out of a validation report, which is passed through.

    Order is deterministic and duplicates are dropped, so the same input always
    resolves to the same actor.
    """
    cleaned = value.strip()
    if not cleaned:
        return []
    if is_pseudonym(cleaned):
        return [cleaned.lower()]
    forms = [cleaned]
    bare = cleaned.split("-", 1)[0].strip()
    if bare and bare != cleaned:
        forms.append(bare)
    out: list[str] = []
    for form in forms:
        candidate = pseudonym(form, "player")
        if candidate not in out:
            out.append(candidate)
    return out


def sanitize_actor(actor: dict[str, Any]) -> dict[str, Any]:
    """Pseudonymize a masterData actor if it is a player or pet.

    NPC actors are returned unchanged: their names are research data.
    """
    out = dict(actor)
    actor_type = str(actor.get("type") or "")
    if actor_type in ("Player", "PlayerPet", "Pet"):
        name = actor.get("name")
        if isinstance(name, str) and name:
            out["name"] = pseudonym(name, "player" if actor_type == "Player" else "pet")
            out["name_pseudonymized"] = True
    for key in ("server", "serverName", "serverSlug"):
        if isinstance(out.get(key), str) and out[key]:
            out[key] = f"realm-{identity_hash(out[key], length=8)}"
    return out


def sanitize_payload(value: Any, *, depth: int = 0) -> Any:
    """Recursively de-identify an arbitrary API response.

    Applied to everything written to `tests/fixtures/`, so committed fixtures
    contain no personal names.
    """
    if depth > 40:  # pragma: no cover - pathological nesting guard
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "actors" and isinstance(item, list):
                out[key] = [
                    sanitize_actor(a)
                    if isinstance(a, dict)
                    else sanitize_payload(a, depth=depth + 1)
                    for a in item
                ]
            elif key == "owner" and isinstance(item, dict):
                owner = dict(item)
                if isinstance(owner.get("name"), str):
                    owner["name"] = pseudonym(owner["name"], "uploader")
                out[key] = owner
            elif key in _TEXT_IDENTITY_KEYS and isinstance(item, str) and item:
                out[key] = f"redacted-{identity_hash(item, length=8)}"
            else:
                out[key] = sanitize_payload(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [sanitize_payload(v, depth=depth + 1) for v in value]
    return value
