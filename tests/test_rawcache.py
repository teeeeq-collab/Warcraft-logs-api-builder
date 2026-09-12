"""Raw-cache tests: identity, atomicity, corruption tolerance, secret safety."""

from __future__ import annotations

import gzip
import json

import pytest

from wcl_mplus import redaction
from wcl_mplus.rawcache import CacheSecurityError, RawCache, cache_key, canonical_params

SECRET = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def cache(tmp_path) -> RawCache:
    return RawCache(tmp_path / "raw_cache")


# -- identity --------------------------------------------------------------


def test_key_is_order_independent():
    assert cache_key("k", {"a": 1, "b": 2}) == cache_key("k", {"b": 2, "a": 1})


def test_key_depends_on_kind_params_and_query_version():
    base = cache_key("events", {"a": 1})
    assert base != cache_key("fights", {"a": 1})
    assert base != cache_key("events", {"a": 2})
    assert base != cache_key("events", {"a": 1}, query_version=99)


def test_canonical_params_is_deterministic():
    assert canonical_params({"b": 1, "a": [3, 2]}) == '{"a":[3,2],"b":1}'


def test_page_cursor_is_part_of_identity(cache):
    """Two pages of the same query must not collide."""
    p1 = cache.path_for("events_casts", {"startTime": 0}, report_code="AbC")
    p2 = cache.path_for("events_casts", {"startTime": 5000}, report_code="AbC")
    assert p1 != p2


def test_path_layout_groups_by_kind_and_report(cache):
    path = cache.path_for("events_casts", {"a": 1}, report_code="AbCd1234")
    assert path.parent.name == "AbCd1234"
    assert path.parent.parent.name == "events_casts"
    assert path.name.endswith(".json.gz")


def test_unsafe_report_code_is_sanitized(cache):
    path = cache.path_for("k", {"a": 1}, report_code="../../etc/passwd")
    assert ".." not in path.parent.name
    assert path.is_relative_to(cache.root)


def test_missing_report_code_uses_global_bucket(cache):
    assert cache.path_for("schema", {}).parent.name == "_global"


# -- roundtrip -------------------------------------------------------------


def test_put_then_get_roundtrip(cache):
    payload = {"reportData": {"report": {"code": "AbC", "fights": [{"id": 1}]}}}
    cache.put("report_fights", {"code": "AbC"}, payload, report_code="AbC")
    entry = cache.get("report_fights", {"code": "AbC"}, report_code="AbC")
    assert entry is not None
    assert entry.response == payload
    assert entry.fetched_at is not None


def test_envelope_records_provenance(cache):
    cache.put("k", {"a": 1}, {"x": 1})
    env = cache.get("k", {"a": 1}).envelope
    assert env["cache_format_version"] == 1
    assert set(env["provenance"]) >= {"software_version", "normalizer_version", "query_version"}


def test_miss_returns_none(cache):
    assert cache.get("k", {"a": 1}) is None


def test_unicode_survives_roundtrip(cache):
    """NPC and ability names can be non-ASCII."""
    cache.put("k", {"a": 1}, {"name": "Zul'jin — Vol'jin ☠"})
    assert cache.get("k", {"a": 1}).response["name"] == "Zul'jin — Vol'jin ☠"


# -- robustness ------------------------------------------------------------


def test_truncated_file_is_discarded_not_fatal(cache):
    cache.put("k", {"a": 1}, {"x": 1})
    path = cache.path_for("k", {"a": 1})
    path.write_bytes(b"\x1f\x8b\x08truncated-garbage")
    assert cache.get("k", {"a": 1}) is None, "a half-written entry must not poison a rerun"


def test_non_object_envelope_is_discarded(cache):
    path = cache.path_for("k", {"a": 1})
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump([1, 2, 3], handle)
    assert cache.get("k", {"a": 1}) is None


def test_no_temp_files_left_behind(cache):
    cache.put("k", {"a": 1}, {"x": 1})
    assert not list(cache.root.rglob("*.tmp*"))


def test_rewrite_is_idempotent(cache):
    first = cache.put("k", {"a": 1}, {"x": 1})
    second = cache.put("k", {"a": 1}, {"x": 2})
    assert first == second
    assert cache.get("k", {"a": 1}).response == {"x": 2}


# -- security --------------------------------------------------------------


def test_write_containing_a_secret_is_blocked(cache):
    redaction.register_secret(SECRET)
    with pytest.raises(CacheSecurityError, match="registered credential"):
        cache.put("k", {"a": 1}, {"echoed": f"prefix {SECRET} suffix"})
    assert not list(cache.root.rglob("*.json.gz")), "nothing reached disk"


def test_authorization_params_are_stripped(cache):
    redaction.register_secret(SECRET)
    cache.put("k", {"Authorization": f"Bearer {SECRET}", "code": "AbC"}, {"ok": 1})
    stored = cache.get("k", {"Authorization": f"Bearer {SECRET}", "code": "AbC"})
    assert stored.envelope["params"] == {"code": "AbC"}


def test_audit_reports_nothing_for_clean_cache(cache):
    redaction.register_secret(SECRET)
    cache.put("k", {"a": 1}, {"ok": 1})
    assert cache.audit_for_secrets() == []


def test_audit_detects_a_planted_secret(cache):
    """Proves the audit would actually catch a leak, not just always pass."""
    redaction.register_secret(SECRET)
    path = cache.path_for("k", {"a": 1})
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"response": {"leak": SECRET}}))
    assert cache.audit_for_secrets() == [str(path)]


# -- stats -----------------------------------------------------------------


def test_stats_counts_by_kind(cache):
    cache.put("events_casts", {"p": 1}, {"x": 1}, report_code="A")
    cache.put("events_casts", {"p": 2}, {"x": 1}, report_code="A")
    cache.put("report_fights", {"p": 1}, {"x": 1}, report_code="A")
    stats = cache.stats()
    assert stats["files"] == 3
    assert stats["kinds"]["events_casts"]["files"] == 2
    assert stats["bytes"] > 0


def test_stats_on_empty_root(tmp_path):
    stats = RawCache(tmp_path / "nothing").stats()
    assert stats["exists"] is False and stats["files"] == 0
