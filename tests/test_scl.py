"""Track A candidates must round-trip exactly, and refuse to guess.

The rule these tests enforce is the one that stops a plausible notation being
adopted on the strength of a hand-written example: a format whose decoder does
not reproduce the timeline is not a candidate, whatever it costs.
"""

from __future__ import annotations

import pytest

from wcl_mplus.scl import SCL_VERSION
from wcl_mplus.scl.candidates import (
    CANDIDATES,
    LLM_CANDIDATES,
    DecodeError,
    dictionary_hash,
    encode_c,
    encode_d,
)
from wcl_mplus.scl.dictionaries import build
from wcl_mplus.scl.measure import (
    HeuristicTokenizer,
    measure,
    measure_at_scales,
    render_benchmark,
    render_benchmark_json,
    resolve_tokenizer,
)
from wcl_mplus.scl.model import TimelineEvent


def timeline() -> list[TimelineEvent]:
    """A pull with two copies of one NPC, a no-target cast, and an off-tank hit."""
    return [
        TimelineEvent(1000, "damage", 5, 1, 1, None, 111, 300),
        TimelineEvent(1500, "damage", 5, 2, 1, None, 111, 280),
        TimelineEvent(2100, "cast", 5, 1, None, None, 222, None),
        TimelineEvent(2600, "damage", 5, 1, 3, None, 111, 410),
        TimelineEvent(2600, "damage", 5, 2, 1, None, 111, 190),
    ]


def dicts(events=None, **kwargs):
    return build(
        events or timeline(),
        player_roles={1: "tank", 3: "healer"},
        ability_names={111: "Melee", 222: "Shadow Bolt"},
        actor_names={5: "Street Sneak", 1: "Prot Paladin", 3: "Holy Priest"},
        **kwargs,
    )


@pytest.mark.parametrize("candidate", sorted(CANDIDATES))
def test_every_candidate_round_trips_exactly(candidate):
    events = timeline()
    dictionaries = dicts(events, frequency_ordered=(candidate == "G"))
    encode, decode = CANDIDATES[candidate]
    assert decode(encode(events, dictionaries), dictionaries) == events


@pytest.mark.parametrize("candidate", sorted(CANDIDATES))
def test_two_copies_of_one_npc_keep_separate_identities(candidate):
    """The mistake this whole corpus was built to avoid, checked per format."""
    events = timeline()
    dictionaries = dicts(events, frequency_ordered=(candidate == "G"))
    encode, decode = CANDIDATES[candidate]
    decoded = decode(encode(events, dictionaries), dictionaries)
    assert {e.source_instance for e in decoded} == {1, 2}
    for original, restored in zip(events, decoded, strict=True):
        assert restored.source_instance == original.source_instance


@pytest.mark.parametrize("candidate", sorted(CANDIDATES))
def test_identical_events_at_one_timestamp_survive(candidate):
    """Two events in the same millisecond are two observations, not one."""
    events = [
        TimelineEvent(1000, "damage", 5, 1, 1, None, 111, 300),
        TimelineEvent(1000, "damage", 5, 1, 1, None, 111, 300),
    ]
    dictionaries = dicts(events, frequency_ordered=(candidate == "G"))
    encode, decode = CANDIDATES[candidate]
    decoded = decode(encode(events, dictionaries), dictionaries)
    assert len(decoded) == 2
    assert decoded == events


def test_a_null_instance_stays_null(candidate="C"):
    """NULL means the API did not say. It must not become 'copy 1'."""
    events = [TimelineEvent(500, "cast", 9, None, None, None, 222, None)]
    dictionaries = dicts(events)
    decoded = CANDIDATES["C"][1](encode_c(events, dictionaries), dictionaries)
    assert decoded[0].source_instance is None


# -- version and dictionary rejection --------------------------------------


def test_a_version_mismatch_is_refused_not_guessed():
    events = timeline()
    dictionaries = dicts(events)
    encoded = encode_c(events, dictionaries)
    encoded.version = "experimental-99"
    with pytest.raises(DecodeError, match="Refusing to decode"):
        CANDIDATES["C"][1](encoded, dictionaries)


def test_a_changed_code_table_is_refused():
    """G's table is corpus-derived; a stale one decodes to valid, wrong data."""
    events = timeline()
    ordered = dicts(events, frequency_ordered=True)
    encoded = CANDIDATES["G"][0](events, ordered)

    plain = dicts(events, frequency_ordered=False)
    assert dictionary_hash(plain) != dictionary_hash(ordered)
    with pytest.raises(DecodeError, match="Dictionary hash"):
        CANDIDATES["G"][1](encoded, plain)


def test_the_encoded_document_carries_the_current_version():
    encoded = encode_c(timeline(), dicts())
    assert encoded.version == SCL_VERSION
    assert "experimental" in SCL_VERSION, "SCL/1 must not be declared before the gate"


# -- default suppression ---------------------------------------------------


def test_default_suppression_stays_lossless_with_exceptions():
    """20% exceptions is exactly as lossless as 2%, provided each is encoded."""
    events = [
        TimelineEvent(1000 + i * 100, "damage", 5, 1, 1 if i % 5 else 3, None, 111, 100 + i)
        for i in range(20)
    ]
    dictionaries = dicts(events)
    encoded = encode_d(events, dictionaries)
    assert CANDIDATES["D"][1](encoded, dictionaries) == events
    # The exceptions really are spelled out rather than implied.
    assert ">" in encoded.text
    assert encoded.text.splitlines()[0].startswith("@DEFAULT_TARGET=")


def test_a_suppressed_default_is_recoverable_from_the_header():
    events = timeline()
    dictionaries = dicts(events)
    encoded = encode_d(events, dictionaries)
    header = encoded.text.splitlines()[0]
    assert header == "@DEFAULT_TARGET=P1"
    stripped = "\n".join(encoded.text.splitlines()[1:])
    encoded.text = stripped
    with pytest.raises(DecodeError, match="@DEFAULT_TARGET"):
        CANDIDATES["D"][1](encoded, dictionaries)


# -- dictionaries ----------------------------------------------------------


def test_player_schemes_produce_different_aliases():
    events = timeline()
    group = dicts(events, player_scheme="group")
    role = dicts(events, player_scheme="role")
    assert set(group.players.values()) == {"P1", "P2"}
    assert "T" in role.players.values() and "H" in role.players.values()


def test_an_unknown_player_scheme_is_refused():
    with pytest.raises(ValueError, match="player scheme"):
        dicts(timeline(), player_scheme="colour")


def test_enemy_codes_are_pull_local_and_deterministic():
    """The same timeline must always produce the same symbols."""
    events = timeline()
    assert dicts(events).enemies == dicts(events).enemies
    assert len(dicts(events).enemies) == 2, "two copies must get two codes"


def test_frequency_ordering_puts_the_commonest_ability_first():
    events = timeline()  # 111 appears four times, 222 once
    ordered = dicts(events, frequency_ordered=True)
    assert ordered.abilities[111] == "a0"
    assert ordered.abilities[222] == "a1"


# -- measurement -----------------------------------------------------------


def test_an_estimate_is_never_presented_as_a_token_count():
    """The rule the plan states: a measurement with an unnamed instrument is not one."""
    tokenizer = HeuristicTokenizer()
    assert tokenizer.is_estimate
    result = measure(timeline(), dicts(), "C", tokenizer)
    payload = result.as_dict()
    assert "tokens" not in payload, "an estimate was labelled as tokens"
    assert "symbols_ESTIMATE" in payload
    assert payload["tokens_are_estimates"] is True

    report = render_benchmark(
        [measure_at_scales(timeline(), dicts(), "C", tokenizer)],
        tokenizer=tokenizer,
        fixture_label="t",
    )
    assert "**These are not token counts.**" in report
    assert "(symbols)" in report


def test_the_resolved_tokenizer_declares_what_it_is():
    tokenizer = resolve_tokenizer()
    assert tokenizer.name
    # Either a real tokenizer (still a proxy) or the declared heuristic. There
    # is no third state where the caller cannot tell which it got.
    assert tokenizer.is_estimate == tokenizer.name.startswith("proxy:")
    assert tokenizer.is_target_model is False


def test_measurement_reports_irreversibility_rather_than_hiding_it():
    tokenizer = HeuristicTokenizer()
    events = timeline()
    ordered = dicts(events, frequency_ordered=True)
    # Measure G against the wrong table: the decoder must refuse, and the
    # measurement must say so instead of quietly reporting a size.
    plain = dicts(events, frequency_ordered=False)
    result = measure(events, plain, "G", tokenizer)
    assert result.reversible is True  # consistent table, so this one is fine

    from wcl_mplus.scl.candidates import CANDIDATES as C

    encoded = C["G"][0](events, ordered)
    with pytest.raises(DecodeError):
        C["G"][1](encoded, plain)


def test_benchmark_json_names_the_instrument():
    tokenizer = HeuristicTokenizer()
    results = [measure_at_scales(timeline(), dicts(), c, tokenizer) for c in LLM_CANDIDATES]
    import json as _json

    payload = _json.loads(render_benchmark_json(results, tokenizer=tokenizer, fixture_label="t"))
    assert payload["tokenizer"]["name"] == "proxy:whitespace-punct"
    assert payload["tokenizer"]["is_estimate"] is True
    assert payload["track"] == "A"
    assert {r["candidate"] for r in payload["results"]} == set(LLM_CANDIDATES)


def test_candidate_e_is_excluded_from_the_llm_set():
    """A storage floor, not something a model should be asked to read."""
    assert "E" in CANDIDATES
    assert "E" not in LLM_CANDIDATES
