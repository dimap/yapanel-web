"""Tests for party_store.py: normalization, atomic JSON storage, concurrency.

The tests follow the current implementation.  Booleans are parsed strictly
(``"false"``/``"0"``/``"off"`` are false), keys are stored as-is, and the lock
is per store instance.
"""
from __future__ import annotations

import json
import re
import threading

import pytest

import party_store


DEFAULT_SETTINGS = party_store.DEFAULT_SETTINGS


def make_store(tmp_path):
    return party_store.PartyStore(
        tmp_path / "settings.json",
        tmp_path / "bulbs.json",
        tmp_path / "strip.json",
        log=lambda *args, **kwargs: None,
    )


def bulb(bulb_id, **extra):
    data = {
        "id": bulb_id,
        "name": f"bulb-{bulb_id}",
        "ip": "192.168.1.10",
        "key": f"key-{bulb_id}",
    }
    data.update(extra)
    return data


def ids(items):
    return [item["id"] for item in items]


# ---------------------------------------------------------------------------
# normalize_settings
# ---------------------------------------------------------------------------
def test_normalize_settings_defaults_for_missing_or_non_dict():
    for raw in (None, [], "garbage", 42):
        result = party_store.normalize_settings(raw)
        assert result == DEFAULT_SETTINGS
        assert result is not DEFAULT_SETTINGS


@pytest.mark.parametrize(
    "value, expected",
    [
        (-5, 0.2),
        (0.2, 0.2),
        (1.7, 1.7),
        (2.5, 2.5),
        (99, 2.5),
        ("1.25", 1.25),
        ("junk", 1.0),
        (None, 1.0),
        (float("nan"), 1.0),
        (float("inf"), 1.0),
    ],
)
def test_normalize_settings_clamps_speed(value, expected):
    result = party_store.normalize_settings({"speed": value})
    assert result["speed"] == pytest.approx(expected)
    assert isinstance(result["speed"], float)


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, 10),
        (10, 10),
        (10.9, 10),
        (57.9, 57),
        (100, 100),
        (1000, 100),
        ("42.9", 42),
        ("junk", 80),
        (None, 80),
        (float("nan"), 80),
    ],
)
def test_normalize_settings_clamps_brightness_to_int(value, expected):
    result = party_store.normalize_settings({"brightness": value})
    assert result["brightness"] == expected
    assert isinstance(result["brightness"], int)


@pytest.mark.parametrize(
    "value, expected",
    [
        (-1, 1),
        (0, 1),
        (1, 1),
        (33.9, 33),
        (256, 256),
        (9999, 256),
        ("junk", 32),
        (None, 32),
    ],
)
def test_normalize_settings_clamps_color_interval_to_int(value, expected):
    result = party_store.normalize_settings({"color_interval": value})
    assert result["color_interval"] == expected
    assert isinstance(result["color_interval"], int)


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, 1.0),
        (1, 1.0),
        (42.5, 42.5),
        (120, 120.0),
        (999, 120.0),
        ("junk", 10.0),
        (None, 10.0),
        (float("inf"), 10.0),
    ],
)
def test_normalize_settings_clamps_silence_timeout(value, expected):
    result = party_store.normalize_settings({"silence_timeout": value})
    assert result["silence_timeout"] == pytest.approx(expected)
    assert isinstance(result["silence_timeout"], float)


@pytest.mark.parametrize(
    "field, good",
    [
        ("active_mode", " PARTY "),
        ("bt_mode", " Ambilight "),
        ("sync_mode", "UNISON"),
        ("ambilight_target", "off"),
        ("effects_target", "Sconces_Only"),
    ],
)
def test_normalize_settings_accepts_known_enums_case_insensitively(field, good):
    result = party_store.normalize_settings({field: good})
    assert result[field] == good.strip().lower()


@pytest.mark.parametrize(
    "field",
    ["active_mode", "bt_mode", "sync_mode", "ambilight_target", "effects_target"],
)
def test_normalize_settings_unknown_enums_fall_back_to_defaults(field):
    for bad in ("bogus", "", None, 123, ["party"]):
        result = party_store.normalize_settings({field: bad})
        assert result[field] == DEFAULT_SETTINGS[field]


def test_normalize_settings_running_state_defaults_off_and_roundtrips():
    assert party_store.normalize_settings({})["running"] is False
    assert party_store.normalize_settings({"running": True})["running"] is True
    assert party_store.normalize_settings({"running": "false"})["running"] is False


def test_normalize_settings_bool_fields_are_python_truthiness():
    # Missing keys keep the (True) defaults.
    assert party_store.normalize_settings({})["master_enabled"] is True
    assert party_store.normalize_settings({})["effects_enabled"] is True

    # Real booleans / numbers / empty containers are converted with bool().
    falsey = party_store.normalize_settings(
        {"master_enabled": False, "effects_enabled": 0}
    )
    assert falsey["master_enabled"] is False
    assert falsey["effects_enabled"] is False

    none_values = party_store.normalize_settings(
        {"master_enabled": None, "effects_enabled": ""}
    )
    assert none_values["master_enabled"] is False
    assert none_values["effects_enabled"] is False

    truthy = party_store.normalize_settings(
        {"master_enabled": 1, "effects_enabled": "yes"}
    )
    assert truthy["master_enabled"] is True
    assert truthy["effects_enabled"] is True


def test_normalize_settings_bool_strings_are_parsed_safely():
    """Строковые false/0/off не должны включать настройку."""
    result = party_store.normalize_settings(
        {"master_enabled": "false", "effects_enabled": "0"}
    )
    assert result["master_enabled"] is False
    assert result["effects_enabled"] is False
    truthy = party_store.normalize_settings(
        {"master_enabled": "true", "effects_enabled": "on"}
    )
    assert truthy["master_enabled"] is True
    assert truthy["effects_enabled"] is True


def test_normalize_settings_output_is_complete_and_ignores_unknown_keys():
    result = party_store.normalize_settings({"bogus": 1, "speed": 1.5})
    assert set(result) == set(DEFAULT_SETTINGS)
    assert "bogus" not in result


def test_normalize_settings_does_not_mutate_defaults():
    before = dict(DEFAULT_SETTINGS)
    result = party_store.normalize_settings({"speed": 2.0})
    result["speed"] = 0.2
    result["new_key"] = True
    assert DEFAULT_SETTINGS == before


# ---------------------------------------------------------------------------
# normalize_bulb
# ---------------------------------------------------------------------------
def test_normalize_bulb_full_payload_is_sanitized():
    raw = {
        "id": "  bf112233  ",
        "name": "  Люстра  ",
        "ip": " 192.168.1.50 ",
        "key": " secret ",
        "ver": 3.7,
        "role": " SNARE ",
        "side": " LEFT ",
        "pos_x": 2.5,
        "pos_y": -99,
        "enabled": 1,
    }
    result = party_store.normalize_bulb(raw)

    assert result == {
        "id": "bf112233",
        "name": "Люстра",
        "ip": "192.168.1.50",
        "key": "secret",
        "ver": 3.5,
        "role": "snare",
        "side": "left",
        "pos_x": 2.5,
        "pos_y": -5.0,
        "enabled": True,
    }


def test_normalize_bulb_id_aliases_and_priority():
    assert party_store.normalize_bulb({"dev_id": "dev-1"})["id"] == "dev-1"
    assert party_store.normalize_bulb({"gwId": "gw-1"})["id"] == "gw-1"
    assert (
        party_store.normalize_bulb(
            {"id": "id-1", "dev_id": "dev-1", "gwId": "gw-1"}
        )["id"]
        == "id-1"
    )
    # Empty strings are falsy, so the next alias is used.
    assert party_store.normalize_bulb({"id": "", "dev_id": "dev-1"})["id"] == "dev-1"
    assert party_store.normalize_bulb({"dev_id": "", "gwId": "gw-1"})["id"] == "gw-1"
    # Zero is falsy as well and is treated as "missing".
    assert party_store.normalize_bulb({"id": 0}) is None
    assert party_store.normalize_bulb({"id": 0, "gwId": "gw-1"})["id"] == "gw-1"


@pytest.mark.parametrize("raw", [None, [], "bulb", 42, (1, 2), ["id"]])
def test_normalize_bulb_rejects_non_dict_and_missing_id(raw):
    assert party_store.normalize_bulb(raw) is None


def test_normalize_bulb_rejects_empty_id():
    assert party_store.normalize_bulb({}) is None
    assert party_store.normalize_bulb({"id": "   "}) is None
    assert party_store.normalize_bulb({"dev_id": "\t"}) is None


def test_normalize_bulb_key_aliases_and_priority():
    assert party_store.normalize_bulb({"id": "a", "local_key": "lk"})["key"] == "lk"
    assert (
        party_store.normalize_bulb({"id": "a", "key": "k", "local_key": "lk"})["key"]
        == "k"
    )
    assert party_store.normalize_bulb({"id": "a"})["key"] == ""
    # Falsy key falls through to local_key.
    assert (
        party_store.normalize_bulb({"id": "a", "key": "", "local_key": "lk"})["key"]
        == "lk"
    )


def test_normalize_bulb_generated_names():
    assert (
        party_store.normalize_bulb({"id": "abcdef123", "ip": "10.1.2.3"})["name"]
        == "Лампа 10.1.2.3"
    )
    assert (
        party_store.normalize_bulb({"id": "abcdef123"})["name"] == "Лампа abcdef"
    )
    assert party_store.normalize_bulb({"id": "ab"})["name"] == "Лампа ab"
    # Empty name is falsy -> generated default (which still uses the id).
    assert party_store.normalize_bulb({"id": "x", "name": ""})["name"] == "Лампа x"
    # A whitespace-only name strips to "" and then falls back to plain "Лампа".
    assert party_store.normalize_bulb({"id": "x", "name": "   "})["name"] == "Лампа"


def test_normalize_bulb_name_is_truncated_to_80_chars():
    result = party_store.normalize_bulb({"id": "x", "name": "z" * 200})
    assert result["name"] == "z" * 80


def test_normalize_bulb_role_and_side_validation():
    assert party_store.normalize_bulb({"id": "x", "role": " KICK "})["role"] == "kick"
    assert party_store.normalize_bulb({"id": "x", "role": "snare"})["role"] == "snare"
    assert party_store.normalize_bulb({"id": "x", "role": "ambient"})["role"] == "ambient"
    for bad in ("alien", "", None, 5):
        assert party_store.normalize_bulb({"id": "x", "role": bad})["role"] == "kick"

    assert party_store.normalize_bulb({"id": "x", "side": " RIGHT "})["side"] == "right"
    assert party_store.normalize_bulb({"id": "x", "side": "left"})["side"] == "left"
    for bad in ("up", "center", None, 1):
        assert party_store.normalize_bulb({"id": "x", "side": bad})["side"] == ""


def test_normalize_bulb_position_defaults_follow_side():
    assert party_store.normalize_bulb({"id": "x"})["pos_x"] == 0.0
    assert party_store.normalize_bulb({"id": "x", "side": "left"})["pos_x"] == -1.0
    assert party_store.normalize_bulb({"id": "x", "side": "right"})["pos_x"] == 1.0
    # Explicit values win, but are clamped.
    assert party_store.normalize_bulb({"id": "x", "pos_x": 2.5})["pos_x"] == 2.5
    assert party_store.normalize_bulb({"id": "x", "pos_x": -99})["pos_x"] == -5.0
    assert party_store.normalize_bulb({"id": "x", "pos_x": 99})["pos_x"] == 5.0
    # Broken explicit values use the side-based default.
    assert (
        party_store.normalize_bulb({"id": "x", "side": "left", "pos_x": "junk"})[
            "pos_x"
        ]
        == -1.0
    )
    assert (
        party_store.normalize_bulb({"id": "x", "side": "left", "pos_x": None})[
            "pos_x"
        ]
        == -1.0
    )


def test_normalize_bulb_pos_y_is_clamped():
    assert party_store.normalize_bulb({"id": "x"})["pos_y"] == 0.0
    assert party_store.normalize_bulb({"id": "x", "pos_y": 2.25})["pos_y"] == 2.25
    assert party_store.normalize_bulb({"id": "x", "pos_y": -99})["pos_y"] == -5.0
    for bad in ("junk", None, float("nan")):
        assert party_store.normalize_bulb({"id": "x", "pos_y": bad})["pos_y"] == 0.0


def test_normalize_bulb_version_alias_clamping_and_rounding():
    assert party_store.normalize_bulb({"id": "x"})["ver"] == 3.5
    assert party_store.normalize_bulb({"id": "x", "ver": 3.0})["ver"] == 3.1
    assert party_store.normalize_bulb({"id": "x", "ver": 4.0})["ver"] == 3.5
    assert party_store.normalize_bulb({"id": "x", "ver": 3.33})["ver"] == 3.3
    assert party_store.normalize_bulb({"id": "x", "ver": "3.4"})["ver"] == 3.4
    # "version" is only used when "ver" is absent.
    assert party_store.normalize_bulb({"id": "x", "version": 3.2})["ver"] == 3.2
    assert (
        party_store.normalize_bulb({"id": "x", "ver": None, "version": 3.2})["ver"]
        == 3.5
    )
    assert party_store.normalize_bulb({"id": "x", "ver": "junk"})["ver"] == 3.5


def test_normalize_bulb_enabled_uses_strict_bool():
    assert party_store.normalize_bulb({"id": "x"})["enabled"] is True
    assert party_store.normalize_bulb({"id": "x", "enabled": 1})["enabled"] is True
    assert party_store.normalize_bulb({"id": "x", "enabled": False})["enabled"] is False
    assert party_store.normalize_bulb({"id": "x", "enabled": 0})["enabled"] is False
    assert party_store.normalize_bulb({"id": "x", "enabled": None})["enabled"] is False
    assert party_store.normalize_bulb({"id": "x", "enabled": "false"})["enabled"] is False
    assert party_store.normalize_bulb({"id": "x", "enabled": "true"})["enabled"] is True


def test_normalize_bulb_survives_garbage_values():
    raw = {
        "id": ["nested"],
        "name": ["x"],
        "ip": 5,
        "key": 12,
        "ver": "abc",
        "role": 5,
        "side": ["left"],
        "pos_x": {"a": 1},
        "pos_y": object(),
        "enabled": object(),
    }
    result = party_store.normalize_bulb(raw)
    assert isinstance(result, dict)
    assert isinstance(result["id"], str) and result["id"]
    assert isinstance(result["name"], str)
    assert isinstance(result["ip"], str)
    assert isinstance(result["key"], str)
    assert result["ver"] == 3.5
    assert result["role"] == "kick"
    assert result["side"] == ""
    assert result["pos_x"] == 0.0
    assert result["pos_y"] == 0.0


# ---------------------------------------------------------------------------
# normalize_bulbs
# ---------------------------------------------------------------------------
def test_normalize_bulbs_accepts_list_and_dict_wrappers():
    a = {"id": "a", "name": "A"}
    b = {"dev_id": "b", "name": "B"}

    assert ids(party_store.normalize_bulbs([a, b])) == ["a", "b"]
    assert ids(party_store.normalize_bulbs({"bulbs": [a]})) == ["a"]
    assert ids(party_store.normalize_bulbs({"devices": [b]})) == ["b"]
    # A bare bulb dict is treated as a single-element list.
    assert ids(party_store.normalize_bulbs(a)) == ["a"]


def test_normalize_bulbs_wrapper_priority():
    a = {"id": "a"}
    b = {"id": "b"}

    assert ids(party_store.normalize_bulbs({"bulbs": [a], "devices": [b]})) == ["a"]
    # An empty "bulbs" list still wins over "devices".
    assert party_store.normalize_bulbs({"bulbs": [], "devices": [b]}) == []
    # "bulbs" is not a list and there is no "devices" list: the whole dict is
    # wrapped and normalized as one bulb.
    assert ids(party_store.normalize_bulbs({"id": "solo", "bulbs": "nope"})) == [
        "solo"
    ]


def test_normalize_bulbs_deduplicates_by_id_first_wins():
    result = party_store.normalize_bulbs(
        [
            {"id": "x", "name": "first"},
            {"id": "x", "name": "second"},
            {"dev_id": "x", "name": "third"},
        ]
    )
    assert ids(result) == ["x"]
    assert result[0]["name"] == "first"


def test_normalize_bulbs_skips_non_dict_and_invalid_items():
    result = party_store.normalize_bulbs(
        [None, 1, "x", [], {"id": ""}, {"id": "ok"}]
    )
    assert ids(result) == ["ok"]


@pytest.mark.parametrize("raw", [None, "text", 42, object()])
def test_normalize_bulbs_rejects_non_list_non_dict(raw):
    assert party_store.normalize_bulbs(raw) == []


def test_normalize_bulbs_is_idempotent():
    once = party_store.normalize_bulbs(
        [{"id": "a", "side": "left", "ver": 3.33, "pos_y": 1.234}]
    )
    twice = party_store.normalize_bulbs(once)
    assert once == twice


# ---------------------------------------------------------------------------
# PartyStore: atomic settings/bulbs writes
# ---------------------------------------------------------------------------
def test_save_settings_writes_valid_json_atomically(tmp_path):
    store = make_store(tmp_path)
    settings_path = tmp_path / "settings.json"

    saved = store.save_settings({"speed": 1.5, "brightness": 55, "active_mode": " PARTY "})

    raw = settings_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data == saved
    assert saved["active_mode"] == "party"
    assert data["speed"] == 1.5
    assert data["brightness"] == 55
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_bulbs_writes_valid_json_atomically_and_keeps_unicode(tmp_path):
    store = make_store(tmp_path)
    bulbs_path = tmp_path / "bulbs.json"

    saved = store.save_bulbs([bulb("a", name="Люстра")])

    raw = bulbs_path.read_text(encoding="utf-8")
    assert json.loads(raw) == saved
    assert "Люстра" in raw  # ensure_ascii=False
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_replace_keeps_previous_file_and_removes_tmp(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    settings_path = tmp_path / "settings.json"
    store.save_settings({"brightness": 40})
    before = settings_path.read_text(encoding="utf-8")

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(party_store.os, "replace", failing_replace)

    with pytest.raises(OSError):
        store.save_settings({"brightness": 90})

    assert settings_path.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_unserializable_strip_write_leaves_no_tmp_and_previous_file(tmp_path):
    store = make_store(tmp_path)
    strip_path = tmp_path / "strip.json"
    store.save_strip({"ok": 1})
    before = strip_path.read_text(encoding="utf-8")

    with pytest.raises(TypeError):
        store.save_strip({"bad": object()})

    assert strip_path.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_settings_are_backed_up_and_defaults_returned(tmp_path):
    store = make_store(tmp_path)
    settings_path = tmp_path / "settings.json"
    original = '{"speed": '
    settings_path.write_text(original, encoding="utf-8")

    loaded = store.load_settings()

    assert loaded == DEFAULT_SETTINGS
    assert store.last_error and "settings.json" in store.last_error
    backups = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(backups) == 1
    assert re.fullmatch(
        r"settings\.json\.corrupt-\d{8}-\d{6}", backups[0].name
    )
    assert backups[0].read_text(encoding="utf-8") == original
    # The corrupt original is kept in place, only a copy is created.
    assert settings_path.read_text(encoding="utf-8") == original


def test_corrupt_bulbs_are_backed_up_and_empty_list_returned(tmp_path):
    store = make_store(tmp_path)
    bulbs_path = tmp_path / "bulbs.json"
    original = '[{"id": "a", '
    bulbs_path.write_text(original, encoding="utf-8")

    assert store.load_bulbs() == []
    assert store.last_error and "bulbs.json" in store.last_error
    backups = list(tmp_path.glob("bulbs.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert bulbs_path.read_text(encoding="utf-8") == original


def test_corrupt_utf8_file_is_backed_up_byte_for_byte(tmp_path):
    store = make_store(tmp_path)
    settings_path = tmp_path / "settings.json"
    payload = b'\xff\xfe{"speed": 1}'
    settings_path.write_bytes(payload)

    assert store.load_settings() == DEFAULT_SETTINGS
    backups = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == payload


def test_missing_files_return_defaults_without_backup(tmp_path):
    store = make_store(tmp_path)

    assert store.load_settings() == DEFAULT_SETTINGS
    assert store.load_bulbs() == []
    assert store.load_strip() == {}
    assert store.last_error is None
    assert list(tmp_path.glob("*.corrupt-*")) == []


def test_none_paths_are_safe_noops(tmp_path):
    store = party_store.PartyStore(None, None, None, log=lambda *a, **k: None)

    settings = store.save_settings({"brightness": 15})
    bulbs = store.save_bulbs([bulb("a")])
    updated = store.update_settings(lambda s: {"speed": 2.0})
    bulbs_after = store.update_bulbs(lambda items: items + [bulb("b")])

    assert settings["brightness"] == 15
    assert ids(bulbs) == ["a"]
    assert updated["speed"] == 2.0
    # update_bulbs starts from an empty list because save_bulbs was a no-op.
    assert ids(bulbs_after) == ["b"]
    # Nothing was persisted, reads still produce defaults.
    assert store.load_settings() == DEFAULT_SETTINGS
    assert store.load_bulbs() == []


# ---------------------------------------------------------------------------
# PartyStore: update_* read-modify-write
# ---------------------------------------------------------------------------
def test_update_settings_is_read_modify_write(tmp_path):
    store = make_store(tmp_path)
    settings_path = tmp_path / "settings.json"
    store.save_settings({"speed": 1.2, "brightness": 30})

    seen = []

    def mutator(settings):
        seen.append(dict(settings))
        settings["brightness"] = 66
        settings["speed"] = 9  # clamped
        settings["bogus"] = "must be dropped"

    result = store.update_settings(mutator)

    assert seen and seen[0]["speed"] == 1.2
    assert seen[0]["brightness"] == 30
    assert result["brightness"] == 66
    assert result["speed"] == 2.5
    assert "bogus" not in result
    assert json.loads(settings_path.read_text(encoding="utf-8")) == result


def test_update_settings_mutator_may_return_replacement_dict(tmp_path):
    store = make_store(tmp_path)
    store.save_settings({"brightness": 70, "active_mode": "party"})

    result = store.update_settings(lambda settings: {"active_mode": "ocean"})

    # Returning a dict replaces the whole settings mapping; omitted fields
    # fall back to defaults.
    assert result["active_mode"] == "ocean"
    assert result["brightness"] == DEFAULT_SETTINGS["brightness"]
    assert result["master_enabled"] is True


def test_update_settings_ignores_non_dict_return(tmp_path):
    store = make_store(tmp_path)

    def mutator(settings):
        settings["silence_timeout"] = 0
        return ["not", "a", "dict"]

    result = store.update_settings(mutator)

    assert result["silence_timeout"] == 1.0
    assert json.loads(
        (tmp_path / "settings.json").read_text(encoding="utf-8")
    )["silence_timeout"] == 1.0


def test_update_bulbs_is_read_modify_write_in_place(tmp_path):
    store = make_store(tmp_path)
    bulbs_path = tmp_path / "bulbs.json"
    store.save_bulbs([bulb("a")])

    def mutator(items):
        items[0]["name"] = "renamed"
        items.append(bulb("b"))

    result = store.update_bulbs(mutator)

    assert ids(result) == ["a", "b"]
    assert result[0]["name"] == "renamed"
    assert json.loads(bulbs_path.read_text(encoding="utf-8")) == result


def test_update_bulbs_mutator_may_return_new_list(tmp_path):
    store = make_store(tmp_path)
    store.save_bulbs([bulb("a"), bulb("b")])

    result = store.update_bulbs(lambda items: [bulb("c")])

    assert ids(result) == ["c"]
    assert ids(store.load_bulbs()) == ["c"]


def test_update_bulbs_reads_current_disk_state(tmp_path):
    first = make_store(tmp_path)
    second = make_store(tmp_path)
    first.save_bulbs([bulb("a")])

    result = second.update_bulbs(lambda items: items + [bulb("b")])

    assert ids(result) == ["a", "b"]
    assert ids(first.load_bulbs()) == ["a", "b"]


def test_update_bulbs_empty_missing_file(tmp_path):
    store = make_store(tmp_path)

    result = store.update_bulbs(lambda items: items + [bulb("a")])

    assert ids(result) == ["a"]
    assert json.loads(
        (tmp_path / "bulbs.json").read_text(encoding="utf-8")
    ) == result


def test_update_bulbs_exception_leaves_file_untouched(tmp_path):
    store = make_store(tmp_path)
    bulbs_path = tmp_path / "bulbs.json"
    store.save_bulbs([bulb("a")])
    before = bulbs_path.read_text(encoding="utf-8")

    def mutator(items):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.update_bulbs(mutator)

    assert bulbs_path.read_text(encoding="utf-8") == before


def test_update_settings_exception_leaves_file_untouched(tmp_path):
    store = make_store(tmp_path)
    settings_path = tmp_path / "settings.json"
    store.save_settings({"brightness": 42})
    before = settings_path.read_text(encoding="utf-8")

    def mutator(settings):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.update_settings(mutator)

    assert settings_path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# strip load/save
# ---------------------------------------------------------------------------
def test_strip_load_save_roundtrip(tmp_path):
    store = make_store(tmp_path)
    strip_path = tmp_path / "strip.json"

    assert store.load_strip() == {}
    assert store.save_strip(
        {"enabled": True, "name": "Лента", "points": [1, 2, 3]}
    ) is None
    assert store.load_strip() == {
        "enabled": True,
        "name": "Лента",
        "points": [1, 2, 3],
    }
    assert json.loads(strip_path.read_text(encoding="utf-8")) == {
        "enabled": True,
        "name": "Лента",
        "points": [1, 2, 3],
    }
    assert list(tmp_path.glob("*.tmp")) == []


def test_strip_non_dict_values_are_replaced_with_empty_object(tmp_path):
    store = make_store(tmp_path)
    strip_path = tmp_path / "strip.json"
    strip_path.write_text("[1, 2, 3]", encoding="utf-8")

    # Valid JSON but not an object: no backup, just an empty dict.
    assert store.load_strip() == {}
    assert list(tmp_path.glob("*.corrupt-*")) == []

    store.save_strip(["not", "a", "dict"])
    assert json.loads(strip_path.read_text(encoding="utf-8")) == {}


# ---------------------------------------------------------------------------
# Key handling (current implementation: no masking in PartyStore)
# ---------------------------------------------------------------------------
def test_save_bulbs_stores_keys_in_plaintext_without_masking(tmp_path):
    """PartyStore does not mask keys; they are persisted verbatim.

    No secret redaction happens at this layer, so the local key must be
    readable again by load_bulbs (and is present in the JSON file).
    """
    store = make_store(tmp_path)
    bulbs_path = tmp_path / "bulbs.json"
    secret = "0123456789abcdef"

    saved = store.save_bulbs([{"id": "b1", "ip": "10.0.0.2", "key": secret}])

    assert saved[0]["key"] == secret
    raw = json.loads(bulbs_path.read_text(encoding="utf-8"))
    assert raw[0]["key"] == secret
    assert secret in bulbs_path.read_text(encoding="utf-8")
    assert store.load_bulbs()[0]["key"] == secret


def test_save_bulbs_does_not_mask_or_unmask_pre_masked_values(tmp_path):
    store = make_store(tmp_path)
    bulbs_path = tmp_path / "bulbs.json"
    masked = "0123***cdef"

    saved = store.save_bulbs([{"id": "b1", "key": masked}])

    assert saved[0]["key"] == masked
    assert json.loads(bulbs_path.read_text(encoding="utf-8"))[0]["key"] == masked


def test_save_bulbs_maps_local_key_to_key_without_masking(tmp_path):
    store = make_store(tmp_path)
    secret = "fedcba9876543210"

    saved = store.save_bulbs([{"id": "b1", "local_key": secret}])

    assert saved[0]["key"] == secret
    raw = json.loads(
        (tmp_path / "bulbs.json").read_text(encoding="utf-8")
    )
    assert raw[0]["key"] == secret


def test_update_bulbs_keeps_existing_key_in_plaintext(tmp_path):
    store = make_store(tmp_path)
    secret = "0011223344556677"
    store.save_bulbs([bulb("a", key=secret)])

    store.update_bulbs(lambda items: [dict(items[0], name="renamed")])

    raw = json.loads(
        (tmp_path / "bulbs.json").read_text(encoding="utf-8")
    )
    assert raw[0]["key"] == secret
    assert store.load_bulbs()[0]["key"] == secret


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_concurrent_update_bulbs_does_not_lose_writes(tmp_path):
    """10 threads x 50 updates: every append and every increment must survive.

    PartyStore.update_bulbs holds an RLock for the whole read-modify-write, so
    all updates on the *same store instance* are serialized.  Note that the
    lock belongs to the instance, not to the file: two PartyStore objects
    pointed at the same path are not serialized against each other.
    """
    store = make_store(tmp_path)
    bulbs_path = tmp_path / "bulbs.json"
    store.save_bulbs([{"id": "seed", "name": "0", "key": "seed-key"}])

    threads_count = 10
    iterations = 50
    barrier = threading.Barrier(threads_count)
    errors = []

    def worker(worker_id):
        try:
            barrier.wait(timeout=30)
            for index in range(iterations):
                def mutator(items, index=index):
                    seed = next(item for item in items if item["id"] == "seed")
                    seed["name"] = str(int(seed["name"]) + 1)
                    items.append(
                        bulb(
                            f"w{worker_id}-{index}",
                            name=f"bulb {worker_id}/{index}",
                        )
                    )

                store.update_bulbs(mutator)
        except BaseException as error:  # noqa: BLE001 - reported to the main thread
            errors.append((worker_id, error))

    threads = [
        threading.Thread(target=worker, args=(worker_id,), daemon=True)
        for worker_id in range(threads_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []

    data = json.loads(bulbs_path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 1 + threads_count * iterations
    assert len({item["id"] for item in data}) == len(data)
    assert {
        f"w{worker_id}-{index}"
        for worker_id in range(threads_count)
        for index in range(iterations)
    } <= {item["id"] for item in data}

    seed = next(item for item in data if item["id"] == "seed")
    assert seed["name"] == str(threads_count * iterations)
    assert seed["key"] == "seed-key"

    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob("*.corrupt-*")) == []
