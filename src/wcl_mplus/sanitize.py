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
from typing import Any

#: Fixed, non-secret salt. Changing it renames every pseudonym and breaks
#: cross-run roster matching, so treat it as part of the data format.
IDENTITY_SALT = "wcl-mplus-v1"

#: Keys whose values are free-text identifiers of people or uploads.
_TEXT_IDENTITY_KEYS = {"title", "guild", "server", "serverName", "serverSlug", "realm"}


def identity_hash(value: str, *, length: int = 12) -> str:
    """Stable pseudonym for a personal identifier."""
    digest = hashlib.sha256(f"{IDENTITY_SALT}:{value.strip().lower()}".encode())
    return digest.hexdigest()[:length]


def pseudonym(value: str, prefix: str = "player") -> str:
    return f"{prefix}-{identity_hash(value)}"


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
