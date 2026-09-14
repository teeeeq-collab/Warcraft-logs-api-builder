"""Player identity: pseudonyms must be stable, and a corpus must know its scheme.

Every cross-run question about a person is answered by comparing pseudonyms,
so the pseudonym function is part of the data format. These tests pin the two
properties that makes true: the same name always produces the same pseudonym,
and a database records which scheme produced the names it holds.
"""

from __future__ import annotations

import pytest

from wcl_mplus.auth import Token
from wcl_mplus.client import GraphQLClient
from wcl_mplus.collect import CollectionError, Collector
from wcl_mplus.configs import ProjectConfig
from wcl_mplus.db import Database
from wcl_mplus.ratelimit import RateLimiter
from wcl_mplus.rawcache import RawCache
from wcl_mplus.sanitize import (
    IDENTITY_SCHEME_VERSION,
    identity_hash,
    is_pseudonym,
    player_name_candidates,
    pseudonym,
    salt_fingerprint,
)
from wcl_mplus.version import provenance


@pytest.fixture
def collector_db(settings, tmp_path):
    """A collector over an empty database. No network: nothing here fetches."""
    client = GraphQLClient(
        settings,
        token_provider=lambda: Token(access_token="t", expires_at=1e12),
        rate_limiter=RateLimiter(min_points_reserve=0),
        cache=RawCache(settings.raw_cache_dir),
        sleep=lambda s: None,
    )
    db = Database(tmp_path / "identity.sqlite")
    yield Collector(client, db, ProjectConfig.load()), db
    db.close()


def test_pseudonyms_are_stable_across_calls_and_case():
    """Roster matching across reports depends on this and nothing else."""
    assert pseudonym("Tankadin") == pseudonym("Tankadin")
    assert pseudonym("Tankadin") == pseudonym(" tankadin ")
    assert pseudonym("Tankadin") != pseudonym("Brewhealz")


def test_the_scheme_is_pinned_not_incidental():
    """A changed digest renames every player in every existing corpus.

    If this fails, the identity scheme changed. That is allowed -- but it needs
    IDENTITY_SCHEME_VERSION bumped alongside it, or two generations of
    pseudonym become indistinguishable.
    """
    assert identity_hash("Tankadin") == "a9c90dd21e39"
    assert IDENTITY_SCHEME_VERSION == 1
    assert salt_fingerprint() == "ef4736efbf4c"


def test_provenance_records_the_scheme():
    prov = provenance()
    assert prov["identity_scheme_version"] == IDENTITY_SCHEME_VERSION
    assert prov["identity_salt_fingerprint"] == salt_fingerprint()


@pytest.mark.parametrize(
    "spelling",
    ["Tankadin", " Tankadin ", "Tankadin-Draenor", "Tankadin-Twisting Nether"],
)
def test_every_spelling_a_person_has_to_hand_resolves_to_one_pseudonym(spelling):
    assert pseudonym("Tankadin") in player_name_candidates(spelling)


def test_a_pseudonym_is_passed_through_not_hashed_again():
    stored = pseudonym("Tankadin")
    assert player_name_candidates(stored) == [stored]
    assert is_pseudonym(stored)
    assert not is_pseudonym("Tankadin")


def test_an_empty_name_resolves_to_nothing():
    assert player_name_candidates("   ") == []


def test_a_fresh_database_is_claimed_for_the_current_scheme(collector_db):
    collector, db = collector_db
    collector.assert_identity_scheme()

    rows = db.query("SELECT scheme_version, salt_fingerprint FROM corpus_identity")
    assert len(rows) == 1
    assert rows[0]["scheme_version"] == IDENTITY_SCHEME_VERSION
    assert rows[0]["salt_fingerprint"] == salt_fingerprint()


def test_claiming_twice_under_the_same_scheme_is_a_no_op(collector_db):
    collector, db = collector_db
    collector.assert_identity_scheme()
    collector.assert_identity_scheme()
    assert len(db.query("SELECT * FROM corpus_identity")) == 1


def test_a_corpus_from_another_scheme_is_refused(collector_db):
    """Two schemes in one database do not merge -- they stop matching."""
    collector, db = collector_db
    db.upsert(
        "corpus_identity",
        {
            "scheme_version": 99,
            "salt_fingerprint": "deadbeefcafe",
            "first_written_at": 0.0,
            "software_version": "0.0.0",
            "notes": "written by a different scheme",
        },
    )
    db.conn.commit()

    with pytest.raises(CollectionError) as excinfo:
        collector.assert_identity_scheme()
    message = str(excinfo.value)
    assert "v99" in message and "deadbeefcafe" in message
    assert "raw cache" in message, "the message must say how to recover"
