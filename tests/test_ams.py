import pytest

from bambu_prep.ams import (
    AMSError,
    TrayInfo,
    filament_suffix_for,
    get_ams_state,
    match_preset,
)
from bambu_prep.config import Config


# ----------------------------------------------------------------------- filament_suffix_for


@pytest.mark.parametrize(
    "machine_name, expected",
    [
        ("Bambu Lab A1 0.4 nozzle", "@BBL A1"),
        ("Bambu Lab A1 0.2 nozzle", "@BBL A1 0.2 nozzle"),
        ("Bambu Lab A1 0.6 nozzle", "@BBL A1 0.6 nozzle"),
        ("Bambu Lab A1 0.8 nozzle", "@BBL A1 0.8 nozzle"),
        ("Bambu Lab A1 mini 0.4 nozzle", "@BBL A1M"),
        ("Bambu Lab A1 mini 0.2 nozzle", "@BBL A1M 0.2 nozzle"),
        ("Bambu Lab P1S 0.4 nozzle", "@BBL P1S"),
        ("Bambu Lab P1P 0.4 nozzle", "@BBL P1P"),
        ("Bambu Lab X1 Carbon 0.4 nozzle", "@BBL X1C"),
    ],
)
def test_filament_suffix_known_printers(machine_name: str, expected: str) -> None:
    assert filament_suffix_for(machine_name) == expected


def test_filament_suffix_unknown_falls_back() -> None:
    # Garbage in -> @BBL <whole-string-as-is>; not a great preset name, but
    # the match_preset step will fail to find it and interactive will catch it.
    assert filament_suffix_for("FreshPrinterXL").startswith("@BBL ")


# ----------------------------------------------------------------------- match_preset


def _tray(name_hint: str = "", sub_brand: str = "") -> TrayInfo:
    return TrayInfo(
        slot=1,
        type="PLA",
        sub_brand=sub_brand,
        color="#1a1a1a",
        info_idx="GFA00",
        name_hint=name_hint,
    )


def test_match_preset_exact_target_wins() -> None:
    tray = _tray(name_hint="Bambu PLA Matte")
    available = [
        "Bambu PLA Basic @BBL A1",
        "Bambu PLA Matte @BBL A1",
        "Bambu PLA Matte @BBL A1M",
    ]
    assert match_preset(tray, "@BBL A1", available) == "Bambu PLA Matte @BBL A1"


def test_match_preset_substring_when_exact_misses() -> None:
    tray = _tray(name_hint="Bambu PLA Matte")
    available = [
        "Bambu PLA Matte Black 1.75 @BBL A1",  # only one A1 candidate containing the hint
        "Bambu PLA Basic @BBL A1",
    ]
    assert match_preset(tray, "@BBL A1", available) == "Bambu PLA Matte Black 1.75 @BBL A1"


def test_match_preset_ambiguous_returns_none() -> None:
    tray = _tray(name_hint="Bambu PLA Matte")
    available = [
        "Bambu PLA Matte Black @BBL A1",
        "Bambu PLA Matte White @BBL A1",
    ]
    assert match_preset(tray, "@BBL A1", available) is None


def test_match_preset_filters_at_base() -> None:
    tray = _tray(name_hint="Bambu PLA Matte")
    available = [
        "Bambu PLA Matte @base",  # parent preset, never instantiable
        "Bambu PLA Matte @BBL A1",
    ]
    assert match_preset(tray, "@BBL A1", available) == "Bambu PLA Matte @BBL A1"


def test_match_preset_falls_through_to_subbrand() -> None:
    tray = _tray(name_hint="", sub_brand="Bambu PLA Matte")
    available = ["Bambu PLA Matte @BBL A1"]
    assert match_preset(tray, "@BBL A1", available) == "Bambu PLA Matte @BBL A1"


def test_match_preset_empty_hint_returns_none() -> None:
    tray = _tray(name_hint="", sub_brand="")
    available = ["Bambu PLA Matte @BBL A1"]
    assert match_preset(tray, "@BBL A1", available) is None


def test_match_preset_wrong_suffix_excluded() -> None:
    tray = _tray(name_hint="Bambu PLA Matte")
    available = ["Bambu PLA Matte @BBL A1M"]  # mini variant only
    assert match_preset(tray, "@BBL A1", available) is None


def test_match_preset_real_a1_pla_basic() -> None:
    """Real data from Ben's A1 on 2026-05-11: sub_brand='PLA Basic', SKU name_hint='A00-P06'."""
    tray = TrayInfo(
        slot=1,
        type="PLA",
        sub_brand="PLA Basic",
        color="EC008CFF",
        info_idx="GFA00",
        name_hint="A00-P06",
    )
    available = [
        "Bambu PLA Basic @BBL A1",
        "Bambu PLA Matte @BBL A1",
        "Bambu PETG Translucent @BBL A1",
    ]
    assert match_preset(tray, "@BBL A1", available) == "Bambu PLA Basic @BBL A1"


def test_match_preset_real_a1_petg_translucent() -> None:
    """Real data: sub_brand='PETG Translucent', name_hint SKU 'G01-P1'."""
    tray = TrayInfo(
        slot=2,
        type="PETG",
        sub_brand="PETG Translucent",
        color="F9C1BD80",
        info_idx="GFG01",
        name_hint="G01-P1",
    )
    available = [
        "Bambu PLA Basic @BBL A1",
        "Bambu PETG Translucent @BBL A1",
    ]
    assert match_preset(tray, "@BBL A1", available) == "Bambu PETG Translucent @BBL A1"


def test_match_preset_ignores_sku_name_hint() -> None:
    """SKU-style name_hint must NOT trigger spurious substring matches."""
    tray = TrayInfo(
        slot=1,
        type="PLA",
        sub_brand="",
        color="",
        info_idx="GFA00",
        name_hint="A00-P06",  # SKU only, no sub_brand
    )
    available = ["Bambu PLA Basic @BBL A1"]
    # No way to resolve without sub_brand - falls to interactive
    assert match_preset(tray, "@BBL A1", available) is None


# ----------------------------------------------------------------------- get_ams_state


def _avail() -> list[str]:
    return [
        "Bambu PLA Basic @BBL A1",
        "Bambu PLA Matte @BBL A1",
        "Bambu PETG-HF Black @BBL A1",
        "Bambu PLA Matte @BBL A1M",  # decoy from a different printer
    ]


def test_get_ams_empty_slots_returns_empty() -> None:
    out = get_ams_state(
        set(),
        config=Config(),
        machine_profile_name="Bambu Lab A1 0.4 nozzle",
        list_filaments=_avail,
        live_query=lambda c: pytest.fail("should not query for empty slot set"),  # noqa: ARG005
        interactive=lambda *_: pytest.fail("should not prompt for empty slot set"),
    )
    assert out == {}


def test_get_ams_live_query_resolves_all_slots() -> None:
    trays = {
        1: TrayInfo(1, "PLA", "PLA Matte", "#000000", "GFA01", "Bambu PLA Matte"),
        2: TrayInfo(2, "PLA", "PLA Basic", "#ffffff", "GFA00", "Bambu PLA Basic"),
    }
    out = get_ams_state(
        {1, 2},
        config=Config(),
        machine_profile_name="Bambu Lab A1 0.4 nozzle",
        list_filaments=_avail,
        live_query=lambda c: trays,
        interactive=lambda *_: pytest.fail("should not prompt when live query covers all slots"),
    )
    assert out == {
        1: "Bambu PLA Matte @BBL A1",
        2: "Bambu PLA Basic @BBL A1",
    }


def test_get_ams_unmatched_slot_falls_to_interactive() -> None:
    # Slot 1 reports a Polymaker filament whose name has no @BBL A1 preset.
    # Slot 2 resolves fine. Interactive only sees slot 1.
    trays = {
        1: TrayInfo(1, "PLA", "Polymaker", "#ff0000", "GFG00", "Polymaker PolyTerra"),
        2: TrayInfo(2, "PLA", "PLA Matte", "#000000", "GFA01", "Bambu PLA Matte"),
    }

    captured: dict[str, object] = {}

    def fake_interactive(slots, detected, suggestions):
        captured["slots"] = slots
        captured["detected"] = detected
        captured["suggestions"] = suggestions
        return {1: "Polymaker PolyTerra PLA @BBL A1"}

    out = get_ams_state(
        {1, 2},
        config=Config(),
        machine_profile_name="Bambu Lab A1 0.4 nozzle",
        list_filaments=_avail,
        live_query=lambda c: trays,
        interactive=fake_interactive,
    )
    assert out == {
        1: "Polymaker PolyTerra PLA @BBL A1",
        2: "Bambu PLA Matte @BBL A1",
    }
    assert captured["slots"] == {1}
    assert captured["suggestions"] == {1: "Polymaker PolyTerra @BBL A1"}


def test_get_ams_missing_slot_in_live_falls_to_interactive() -> None:
    # Slot 1 reports nothing (empty AMS slot); slot 2 resolves.
    trays = {
        2: TrayInfo(2, "PLA", "PLA Matte", "#000000", "GFA01", "Bambu PLA Matte"),
    }
    out = get_ams_state(
        {1, 2},
        config=Config(),
        machine_profile_name="Bambu Lab A1 0.4 nozzle",
        list_filaments=_avail,
        live_query=lambda c: trays,
        interactive=lambda slots, *_: {1: "Bambu PLA Basic @BBL A1"},
    )
    assert out == {
        1: "Bambu PLA Basic @BBL A1",
        2: "Bambu PLA Matte @BBL A1",
    }


def test_get_ams_live_returns_none_full_interactive() -> None:
    captured: dict[str, object] = {}

    def fake_interactive(slots, detected, suggestions):
        captured["slots"] = slots
        captured["detected"] = detected
        captured["suggestions"] = suggestions
        return {s: "Bambu PLA Matte @BBL A1" for s in slots}

    out = get_ams_state(
        {1, 3},
        config=Config(),
        machine_profile_name="Bambu Lab A1 0.4 nozzle",
        list_filaments=_avail,
        live_query=lambda c: None,
        interactive=fake_interactive,
    )
    assert out == {1: "Bambu PLA Matte @BBL A1", 3: "Bambu PLA Matte @BBL A1"}
    assert captured["slots"] == {1, 3}
    assert captured["detected"] is None
    assert captured["suggestions"] == {}


def test_get_ams_interactive_omits_slot_raises() -> None:
    """If interactive doesn't return all requested slots, AMSError surfaces."""
    with pytest.raises(AMSError, match="slot 1"):
        get_ams_state(
            {1, 2},
            config=Config(),
            machine_profile_name="Bambu Lab A1 0.4 nozzle",
            list_filaments=_avail,
            live_query=lambda c: None,
            interactive=lambda slots, *_: {2: "Bambu PLA Basic @BBL A1"},  # slot 1 missing
        )


def test_get_ams_passes_machine_context_to_match() -> None:
    """A1 mini machine should match A1M filament presets, not A1 ones."""
    trays = {
        1: TrayInfo(1, "PLA", "PLA Matte", "#000000", "GFA01", "Bambu PLA Matte"),
    }
    out = get_ams_state(
        {1},
        config=Config(),
        machine_profile_name="Bambu Lab A1 mini 0.4 nozzle",
        list_filaments=_avail,
        live_query=lambda c: trays,
        interactive=lambda *_: pytest.fail("should resolve to A1M filament"),
    )
    assert out == {1: "Bambu PLA Matte @BBL A1M"}
