"""Configuration parsing tests, including the discovery overlay."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from wcl_mplus.configs import (
    ConfigFileError,
    DungeonRegistry,
    HotfixEpochs,
    ProjectConfig,
    SamplingConfig,
    config_hash,
)


def write(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# -- shipped config --------------------------------------------------------


def test_shipped_config_loads():
    config = ProjectConfig.load()
    assert config.dungeons.season_id == "midnight-s2"
    assert config.sampling.sample_profiles
    assert config.hotfixes.epochs


def test_shipped_config_has_no_hardcoded_dungeon_ids():
    """Phase 0 must ship with every dungeon ID unverified (brief section 33)."""
    registry = DungeonRegistry.load()
    for entry in registry.dungeons:
        assert entry.wcl_zone_id is None, f"{entry.key} has a hardcoded zone ID"
        assert entry.verified is False
        assert entry.encounter_ids == []


def test_config_hash_is_stable_and_sensitive(tmp_path):
    a = write(tmp_path / "a.yml", {"x": 1})
    first = config_hash(a)
    assert first == config_hash(a)
    write(tmp_path / "a.yml", {"x": 2})
    assert config_hash(a) != first


# -- dungeons --------------------------------------------------------------


def test_resolve_by_name_alias_and_key(tmp_path):
    registry = DungeonRegistry.load()
    for needle in ("Murder Row", "murder row", "mr", "murder-row"):
        assert registry.resolve(needle).key == "murder-row"


def test_resolve_unknown_dungeon_lists_options():
    with pytest.raises(ConfigFileError, match="Configured:"):
        DungeonRegistry.load().resolve("Karazhan")


def test_unverified_zone_id_raises_with_instructions():
    entry = DungeonRegistry.load().resolve("murder row")
    with pytest.raises(ConfigFileError, match="discover-dungeons"):
        entry.require_zone_id()


def test_duplicate_dungeon_keys_rejected(tmp_path):
    path = write(
        tmp_path / "dungeons.yml",
        {"season": {"id": "s"}, "dungeons": [{"key": "a"}, {"key": "a"}]},
    )
    with pytest.raises(ConfigFileError, match="duplicate dungeon keys"):
        DungeonRegistry.load(path)


def test_dungeon_without_key_rejected(tmp_path):
    path = write(tmp_path / "dungeons.yml", {"dungeons": [{"display_name": "x"}]})
    with pytest.raises(ConfigFileError, match="needs a 'key'"):
        DungeonRegistry.load(path)


# -- discovery overlay -----------------------------------------------------


def test_overlay_fills_ids_without_touching_authored_fields(tmp_path):
    base = write(
        tmp_path / "dungeons.yml",
        {
            "season": {"id": "midnight-s2", "verified": False},
            "dungeons": [
                {
                    "key": "murder-row",
                    "display_name": "Murder Row",
                    "aliases": ["mr"],
                    "role": "primary-pilot",
                    "wcl_zone_id": None,
                    "verified": False,
                    "validation_targets": [{"npc": "Shivan Punisher"}],
                }
            ],
        },
    )
    write(
        tmp_path / "dungeons.discovered.yml",
        {
            "season": {"verified": True},
            "dungeons": [
                {"key": "murder-row", "wcl_zone_id": 44, "encounter_ids": [1, 2], "verified": True}
            ],
        },
    )
    registry = DungeonRegistry.load(base)
    entry = registry.resolve("mr")
    assert entry.require_zone_id() == 44
    assert entry.encounter_ids == [1, 2]
    # Authored metadata survives the merge.
    assert entry.role == "primary-pilot"
    assert entry.aliases == ["mr"]
    assert entry.validation_targets == [{"npc": "Shivan Punisher"}]
    assert registry.season_verified is True


def test_overlay_can_add_newly_discovered_dungeons(tmp_path):
    base = write(tmp_path / "dungeons.yml", {"season": {"id": "s"}, "dungeons": []})
    write(
        tmp_path / "dungeons.discovered.yml",
        {"dungeons": [{"key": "new-place", "wcl_zone_id": 99, "verified": True}]},
    )
    registry = DungeonRegistry.load(base)
    assert registry.resolve("new-place").require_zone_id() == 99


def test_overlay_absence_leaves_everything_unverified(tmp_path):
    base = write(
        tmp_path / "dungeons.yml",
        {"season": {"id": "s"}, "dungeons": [{"key": "a", "display_name": "A"}]},
    )
    registry = DungeonRegistry.load(base)
    assert registry.verified_count == 0
    assert registry.overlay_path is None


def test_overlay_is_part_of_the_config_hash(tmp_path):
    for name, data in (
        ("dungeons.yml", {"season": {"id": "s"}, "dungeons": [{"key": "a"}]}),
        (
            "sampling.yml",
            yaml.safe_load((__import__("pathlib").Path("config/sampling.yml")).read_text()),
        ),
        ("hotfix_epochs.yml", {"epochs": [{"name": "unclassified"}]}),
    ):
        write(tmp_path / name, data)
    before = ProjectConfig.load(tmp_path).hash()
    write(
        tmp_path / "dungeons.discovered.yml",
        {"dungeons": [{"key": "a", "wcl_zone_id": 1, "verified": True}]},
    )
    assert ProjectConfig.load(tmp_path).hash() != before


# -- sampling --------------------------------------------------------------


def test_bracket_assignment():
    sampling = SamplingConfig.load()
    assert sampling.bracket_for(7) == "7-8"
    assert sampling.bracket_for(10) == "10"
    assert sampling.bracket_for(14) == "13-14"
    assert sampling.bracket_for(30) == "15+"
    assert sampling.bracket_for(2) == "2-6", "keystones start at 2; the bottom is closed"
    assert sampling.bracket_for(1) is None, "below every bracket"
    assert sampling.bracket_for(None) is None


def test_bracket_boundaries_do_not_overlap():
    sampling = SamplingConfig.load()
    for level in range(1, 40):
        hits = [b.key for b in sampling.key_brackets if b.contains(level)]
        assert len(hits) <= 1, f"key {level} falls in multiple brackets: {hits}"


def test_unknown_event_profile_reference_rejected(tmp_path):
    path = write(
        tmp_path / "sampling.yml",
        {
            "key_brackets": [{"key": "10", "min": 10, "max": 10}],
            "event_profiles": {"metadata": {"event_types": []}},
            "sample_profiles": {"pilot": {"event_profile": "nope", "brackets": ["10"]}},
        },
    )
    with pytest.raises(ConfigFileError, match="unknown event profile"):
        SamplingConfig.load(path)


def test_unknown_bracket_reference_rejected(tmp_path):
    path = write(
        tmp_path / "sampling.yml",
        {
            "key_brackets": [{"key": "10", "min": 10, "max": 10}],
            "event_profiles": {"metadata": {"event_types": []}},
            "sample_profiles": {"pilot": {"event_profile": "metadata", "brackets": ["99"]}},
        },
    )
    with pytest.raises(ConfigFileError, match="unknown key bracket"):
        SamplingConfig.load(path)


def test_event_types_validated_against_live_enum():
    sampling = SamplingConfig.load()
    problems = sampling.validate_event_types(["Casts", "Deaths", "All"])
    assert "Dispels" in problems["mechanics"]
    assert "metadata" not in problems, "a profile with no event types has no problems"
    assert sampling.validate_event_types([]) == {}, "unknown enum means no verdict"


def test_unknown_profile_names_raise():
    sampling = SamplingConfig.load()
    with pytest.raises(ConfigFileError, match="Unknown event profile"):
        sampling.event_profile("nope")
    with pytest.raises(ConfigFileError, match="Unknown sample profile"):
        sampling.sample_profile("nope")


def test_no_brackets_rejected(tmp_path):
    path = write(tmp_path / "sampling.yml", {"key_brackets": []})
    with pytest.raises(ConfigFileError, match="at least one key bracket"):
        SamplingConfig.load(path)


# -- hotfix epochs ---------------------------------------------------------


def test_shipped_epochs_classify_everything_as_unclassified():
    """No real hotfix dates are invented by this repository."""
    epochs = HotfixEpochs.load()
    assert epochs.epoch_for(dt.datetime(2026, 6, 1, tzinfo=dt.UTC)) == "unclassified"
    assert epochs.current is None


def test_epoch_assignment_is_half_open(tmp_path):
    path = write(
        tmp_path / "hotfix_epochs.yml",
        {
            "epochs": [
                {"name": "early", "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
                {"name": "late", "start": "2026-02-01T00:00:00Z", "end": None},
            ]
        },
    )
    epochs = HotfixEpochs.load(path)
    assert (
        epochs.epoch_for("2026-01-15T00:00:00+00:00" and dt.datetime(2026, 1, 15, tzinfo=dt.UTC))
        == "early"
    )
    # The boundary instant belongs to the new epoch, never both.
    assert epochs.epoch_for(dt.datetime(2026, 2, 1, tzinfo=dt.UTC)) == "late"
    assert epochs.epoch_for(dt.datetime(2025, 1, 1, tzinfo=dt.UTC)) == "unclassified"
    assert epochs.current.name == "late"


def test_millisecond_and_second_timestamps_both_accepted(tmp_path):
    path = write(
        tmp_path / "hotfix_epochs.yml",
        {"epochs": [{"name": "e", "start": "2026-01-01T00:00:00Z", "end": None}]},
    )
    epochs = HotfixEpochs.load(path)
    moment = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    assert epochs.epoch_for(moment.timestamp()) == "e"
    assert epochs.epoch_for(moment.timestamp() * 1000) == "e", "WCL times are milliseconds"


def test_overlapping_epochs_rejected(tmp_path):
    path = write(
        tmp_path / "hotfix_epochs.yml",
        {
            "epochs": [
                {"name": "a", "start": "2026-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
                {"name": "b", "start": "2026-02-01T00:00:00Z", "end": None},
            ]
        },
    )
    with pytest.raises(ConfigFileError, match="overlap"):
        HotfixEpochs.load(path)


def test_open_ended_earlier_epoch_rejected(tmp_path):
    path = write(
        tmp_path / "hotfix_epochs.yml",
        {
            "epochs": [
                {"name": "a", "start": "2026-01-01T00:00:00Z", "end": None},
                {"name": "b", "start": "2026-02-01T00:00:00Z", "end": None},
            ]
        },
    )
    with pytest.raises(ConfigFileError, match="overlap"):
        HotfixEpochs.load(path)


def test_bad_timestamp_rejected(tmp_path):
    path = write(
        tmp_path / "hotfix_epochs.yml", {"epochs": [{"name": "a", "start": "last tuesday"}]}
    )
    with pytest.raises(ConfigFileError, match="ISO-8601"):
        HotfixEpochs.load(path)


def test_missing_file_and_bad_yaml(tmp_path):
    with pytest.raises(ConfigFileError, match="not found"):
        HotfixEpochs.load(tmp_path / "nope.yml")
    bad = tmp_path / "bad.yml"
    bad.write_text("epochs: [oops\n")
    with pytest.raises(ConfigFileError, match="not valid YAML"):
        HotfixEpochs.load(bad)


def test_top_level_list_rejected(tmp_path):
    path = tmp_path / "x.yml"
    path.write_text("- a\n- b\n")
    with pytest.raises(ConfigFileError, match="mapping at the top level"):
        HotfixEpochs.load(path)
