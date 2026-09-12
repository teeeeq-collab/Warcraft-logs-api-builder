"""Live GraphQL schema introspection and field verification.

This module exists because of one project rule: **the live schema wins**
(brief section 6). Nothing in this repository asserts that a field exists.
Instead, the relevant types are introspected, the fields the research design
needs are checked against what is really there, and queries are then built
from the intersection.

The practical payoff is that a renamed or removed field shows up as a row in
`API_NOTES.md` saying "absent", rather than as a GraphQL validation error in
the middle of a long collection run.

Introspection is done in two targeted stages rather than one full-schema dump:
a cheap type listing, then per-type field queries for the handful of types that
matter. Full introspection on a schema this size is a large response for
information we would not read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import GraphQLClient

logger = logging.getLogger(__name__)

#: Stage 1: which types exist at all.
TYPE_LIST_QUERY = """
query TypeList {
  __schema {
    queryType { name }
    types { name kind description }
  }
}
"""

#: Stage 2: one type in detail. `ofType` is unrolled three levels, which
#: covers every wrapper combination in practice (e.g. [Type!]! ).
TYPE_DETAIL_QUERY = """
query TypeDetail($name: String!) {
  __type(name: $name) {
    name
    kind
    description
    enumValues(includeDeprecated: true) { name description isDeprecated }
    fields(includeDeprecated: true) {
      name
      description
      isDeprecated
      args { name defaultValue type { ...Ref } }
      type { ...Ref }
    }
  }
}

fragment Ref on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType { kind name }
    }
  }
}
"""


def unwrap_type_name(type_ref: dict[str, Any] | None) -> str | None:
    """Reduce a possibly-wrapped type reference to its named type.

    `[ReportDungeonPull!]!` becomes `ReportDungeonPull`.
    """
    current = type_ref
    depth = 0
    while isinstance(current, dict) and depth < 10:
        if current.get("name"):
            return str(current["name"])
        current = current.get("ofType")
        depth += 1
    return None


def render_type(type_ref: dict[str, Any] | None) -> str:
    """Render a type reference roughly as it appears in SDL."""
    if not isinstance(type_ref, dict):
        return "?"
    kind = type_ref.get("kind")
    if kind == "NON_NULL":
        return f"{render_type(type_ref.get('ofType'))}!"
    if kind == "LIST":
        return f"[{render_type(type_ref.get('ofType'))}]"
    return str(type_ref.get("name") or "?")


@dataclass
class TypeInfo:
    """One introspected type."""

    name: str
    kind: str
    description: str | None = None
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    enum_values: list[str] = field(default_factory=list)

    def has(self, field_name: str) -> bool:
        return field_name in self.fields

    def field_type(self, field_name: str) -> str | None:
        info = self.fields.get(field_name)
        return render_type(info.get("type")) if info else None

    def arg_names(self, field_name: str) -> list[str]:
        info = self.fields.get(field_name)
        if not info:
            return []
        return [
            str(a["name"]) for a in info.get("args", []) if isinstance(a, dict) and a.get("name")
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "field_count": len(self.fields),
            "fields": {n: self.field_type(n) for n in sorted(self.fields)},
            "enum_values": self.enum_values,
        }


@dataclass
class FieldCheck:
    """Result of checking one hoped-for field against the live schema."""

    type_name: str
    field_name: str
    present: bool
    resolved_type: str | None = None
    deprecated: bool = False
    note: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "type": self.type_name,
            "field": self.field_name,
            "present": self.present,
            "resolved_type": self.resolved_type,
            "deprecated": self.deprecated,
            "note": self.note,
        }


class SchemaIntrospector:
    """Fetches and caches introspection results for chosen types."""

    def __init__(self, client: GraphQLClient) -> None:
        self.client = client
        self._type_list: dict[str, str] | None = None
        self._types: dict[str, TypeInfo | None] = {}

    # -- stage 1 ----------------------------------------------------------

    def type_list(self, *, use_cache: bool = True) -> dict[str, str]:
        """Map of type name -> kind for the whole schema."""
        if self._type_list is None:
            data = self.client.execute(
                TYPE_LIST_QUERY, {}, kind="introspect_type_list", use_cache=use_cache
            )
            types = ((data.get("__schema") or {}).get("types")) or []
            self._type_list = {
                str(t["name"]): str(t.get("kind"))
                for t in types
                if isinstance(t, dict) and t.get("name")
            }
        return self._type_list

    def type_exists(self, name: str) -> bool:
        return name in self.type_list()

    def find_types(self, *substrings: str) -> dict[str, str]:
        """Types whose name contains any of the given substrings.

        Used to locate a renamed type (e.g. if `ReportDungeonPull` moved) so
        recon can report the real name instead of just "missing".
        """
        needles = [s.lower() for s in substrings]
        return {
            name: kind
            for name, kind in self.type_list().items()
            if any(n in name.lower() for n in needles)
        }

    # -- stage 2 ----------------------------------------------------------

    def type_info(self, name: str, *, use_cache: bool = True) -> TypeInfo | None:
        """Detailed info for one type, or None if it does not exist."""
        if name in self._types:
            return self._types[name]
        data = self.client.execute(
            TYPE_DETAIL_QUERY,
            {"name": name},
            kind=f"introspect_type__{name}",
            use_cache=use_cache,
        )
        raw = data.get("__type")
        if not isinstance(raw, dict) or not raw.get("name"):
            logger.info("Type %s is not present in the live schema.", name)
            self._types[name] = None
            return None
        fields = {
            str(f["name"]): f
            for f in (raw.get("fields") or [])
            if isinstance(f, dict) and f.get("name")
        }
        enums = [
            str(e["name"])
            for e in (raw.get("enumValues") or [])
            if isinstance(e, dict) and e.get("name")
        ]
        info = TypeInfo(
            name=str(raw["name"]),
            kind=str(raw.get("kind")),
            description=raw.get("description"),
            fields=fields,
            enum_values=enums,
        )
        self._types[name] = info
        return info

    # -- verification -----------------------------------------------------

    def check_fields(self, type_name: str, wanted: list[str]) -> list[FieldCheck]:
        """Check a list of hoped-for fields against one type."""
        info = self.type_info(type_name)
        if info is None:
            return [
                FieldCheck(
                    type_name=type_name,
                    field_name=name,
                    present=False,
                    note=f"type {type_name!r} not found in the live schema",
                )
                for name in wanted
            ]
        results: list[FieldCheck] = []
        for name in wanted:
            raw = info.fields.get(name)
            results.append(
                FieldCheck(
                    type_name=type_name,
                    field_name=name,
                    present=raw is not None,
                    resolved_type=info.field_type(name),
                    deprecated=bool(raw.get("isDeprecated")) if raw else False,
                    note="" if raw else "absent",
                )
            )
        return results

    def present_fields(self, type_name: str, wanted: list[str]) -> list[str]:
        """Subset of `wanted` that actually exists, preserving order.

        This is what query builders use, so a generated query can never name a
        field the server does not have.
        """
        info = self.type_info(type_name)
        if info is None:
            return []
        return [name for name in wanted if info.has(name)]

    def enum_values(self, type_name: str) -> list[str]:
        info = self.type_info(type_name)
        return list(info.enum_values) if info else []
