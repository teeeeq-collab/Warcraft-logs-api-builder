"""Warcraft Logs Mythic+ research dataset collector.

A research instrument for building an empirical model of what tanks and
healers actually experience in Midnight Season 2 Mythic+ runs.

Phase 0 (API reconnaissance) is the implemented scope. See PROJECT_STATE.md
for gate status and API_NOTES.md for what has and has not been verified
against the live API.

Nothing here asserts that a Warcraft Logs schema field exists: queries are
generated from introspected fields, so a renamed or removed field is recorded
as an absence rather than crashing a collection run.
"""

from .version import NORMALIZER_VERSION, QUERY_VERSION, SCHEMA_VERSION, SOFTWARE_VERSION

__version__ = SOFTWARE_VERSION

__all__ = [
    "NORMALIZER_VERSION",
    "QUERY_VERSION",
    "SCHEMA_VERSION",
    "SOFTWARE_VERSION",
    "__version__",
]
