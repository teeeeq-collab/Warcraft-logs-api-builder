"""Normalizer identity rules.

These pin the derived identifiers the corpus is indexed by. A signature
that changes meaning silently would re-partition every cross-log question
without anything in the data saying so.
"""

from __future__ import annotations


def test_species_signature_survives_how_the_pack_was_pulled():
    """A pack grabbed with three of something is the same pack as with five.

    The exact composition signature splits one trash group into as many
    identities as there are ways to pull it -- which is the variation the corpus
    exists to measure, not a difference in which pack it is.
    """
    from wcl_mplus.normalize import composition_signature, species_signature

    small = [
        {"gameID": 100, "minimumInstanceID": 1, "maximumInstanceID": 3},
        {"gameID": 200, "minimumInstanceID": 1, "maximumInstanceID": 1},
    ]
    large = [
        {"gameID": 100, "minimumInstanceID": 1, "maximumInstanceID": 5},
        {"gameID": 200, "minimumInstanceID": 1, "maximumInstanceID": 1},
    ]
    assert species_signature(small) == species_signature(large) == "100|200"
    # The exact signature must still tell them apart: that gap is the signal.
    assert composition_signature(small)[0] != composition_signature(large)[0]


def test_species_signature_is_order_independent():
    from wcl_mplus.normalize import species_signature

    a = [{"gameID": 300}, {"gameID": 100}, {"gameID": 200}]
    b = [{"gameID": 200}, {"gameID": 300}, {"gameID": 100}]
    assert species_signature(a) == species_signature(b) == "100|200|300"


def test_species_signature_deduplicates_repeated_species():
    """Two rows for one species is one species, however many copies each holds."""
    from wcl_mplus.normalize import species_signature

    assert species_signature([{"gameID": 7}, {"gameID": 7}, {"gameID": 9}]) == "7|9"


def test_species_signature_ignores_npcs_without_an_id():
    from wcl_mplus.normalize import species_signature

    assert species_signature([{"gameID": 5}, {"name": "no id"}, {"gameID": None}]) == "5"
    assert species_signature([]) == ""
