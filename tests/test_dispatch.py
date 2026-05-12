import zipfile
from pathlib import Path

import pytest

from bambu_prep.ams import TrayInfo
from bambu_prep.config import Config, Printer
from bambu_prep.dispatch import (
    DispatchError,
    FilamentRequirement,
    PreflightReport,
    SlotCompatibility,
    _family,
    _materials_match,
    extract_filament_requirements,
    preflight,
    start,
    upload,
)


def _config_with_printer() -> Config:
    return Config(printer=Printer(ip="192.168.1.17", access_code="12345678", serial="SN"))


def _make_sliced_3mf(path: Path, filaments_xml: str) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "Metadata/slice_info.config",
            f"<config><plate>{filaments_xml}</plate></config>",
        )
    return path


def _make_unsliced_3mf(path: Path, extruder_metas: str) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "Metadata/model_settings.config",
            f"<config>{extruder_metas}</config>",
        )
    return path


def _make_empty_3mf(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


# ---------------------------------------------------------------------------
# family / materials_match
# ---------------------------------------------------------------------------


def test_family_normalizes_pla_variants() -> None:
    assert _family("PLA") == "PLA"
    assert _family("PLA Basic") == "PLA"
    assert _family("PLA Matte") == "PLA"
    assert _family("pla") == "PLA"


def test_family_handles_carbon_fiber_separately() -> None:
    assert _family("PLA-CF") == "PLA-CF"
    assert _family("PETG-CF") == "PETG-CF"
    # plain PLA must NOT match PLA-CF
    assert _family("PLA") != _family("PLA-CF")


def test_family_empty_string() -> None:
    assert _family("") == ""
    assert _family("   ") == ""


def test_materials_match_basic() -> None:
    assert _materials_match("PLA", "PLA")
    assert _materials_match("PLA", "PLA Basic")
    assert _materials_match("PETG", "PETG Translucent")
    assert not _materials_match("PLA", "PETG")
    assert not _materials_match("", "PLA")
    assert not _materials_match("PLA", "")


# ---------------------------------------------------------------------------
# extract_filament_requirements
# ---------------------------------------------------------------------------


def test_extract_from_sliced_3mf(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        '<filament id="1" type="PLA" color="#FF0000FF" used_g="11.5"/>'
        '<filament id="2" type="PETG" color="#00FF00FF" used_g="2.0"/>',
    )
    fils, source = extract_filament_requirements(p)
    assert source == "slice_info"
    assert len(fils) == 2
    assert fils[0] == FilamentRequirement(1, "PLA", "#FF0000FF", 11.5)
    assert fils[1] == FilamentRequirement(2, "PETG", "#00FF00FF", 2.0)


def test_extract_dedups_repeated_filament_ids(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        # Bambu sometimes writes per-plate <filament> tags so the same id repeats.
        '<filament id="1" type="PLA" used_g="3.0"/>'
        '<filament id="1" type="PLA" used_g="5.0"/>',
    )
    fils, _ = extract_filament_requirements(p)
    assert len(fils) == 1
    assert fils[0].filament_index == 1


def test_extract_from_unsliced_3mf(tmp_path: Path) -> None:
    p = _make_unsliced_3mf(
        tmp_path / "x.3mf",
        '<object id="1"><metadata key="extruder" value="1"/></object>'
        '<object id="2"><metadata key="extruder" value="2"/></object>'
        '<object id="3"><metadata key="extruder" value="1"/></object>',  # dup slot
    )
    fils, source = extract_filament_requirements(p)
    assert source == "model_settings"
    assert len(fils) == 2  # dedup of slots
    assert all(r.material_type == "" for r in fils)


def test_extract_unknown_3mf(tmp_path: Path) -> None:
    p = _make_empty_3mf(tmp_path / "x.3mf")
    fils, source = extract_filament_requirements(p)
    assert source == "unknown"
    assert fils == []


def test_extract_handles_malformed_filament_ids(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        '<filament id="not-a-number" type="PLA"/>'
        '<filament id="2" type="PETG"/>',
    )
    fils, _ = extract_filament_requirements(p)
    assert len(fils) == 1
    assert fils[0].filament_index == 2


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def _tray(slot: int, mtype: str, color: str = "FFFFFFFF") -> TrayInfo:
    return TrayInfo(
        slot=slot,
        type=mtype,
        sub_brand=f"Bambu {mtype}",
        color=color,
        info_idx="",
        name_hint="",
    )


def test_preflight_sliced_all_ok(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        '<filament id="1" type="PLA" color="#FFFFFFFF" used_g="11"/>',
    )
    trays = {1: _tray(1, "PLA Basic")}
    rep = preflight(p, config=_config_with_printer(), ams_query=lambda _c: trays)
    assert rep.source == "slice_info"
    assert rep.ok
    assert rep.ams_mapping == [0]
    assert len(rep.compatibility) == 1
    assert rep.compatibility[0].target_slot == 1
    assert rep.compatibility[0].ok


def test_preflight_sliced_type_mismatch(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        '<filament id="1" type="PETG" used_g="11"/>',
    )
    trays = {1: _tray(1, "PLA Basic")}
    rep = preflight(p, config=_config_with_printer(), ams_query=lambda _c: trays)
    assert not rep.ok
    assert "mismatch" in rep.compatibility[0].reason


def test_preflight_sliced_empty_slot(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        '<filament id="1" type="PLA" used_g="11"/>',
    )
    rep = preflight(p, config=_config_with_printer(), ams_query=lambda _c: {})
    assert not rep.ok
    assert "empty" in rep.compatibility[0].reason.lower()


def test_preflight_multi_filament_default_mapping(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        '<filament id="1" type="PLA"/>'
        '<filament id="2" type="PETG"/>',
    )
    trays = {1: _tray(1, "PLA"), 2: _tray(2, "PETG")}
    rep = preflight(p, config=_config_with_printer(), ams_query=lambda _c: trays)
    assert rep.ams_mapping == [0, 1]
    assert rep.ok


def test_preflight_custom_mapping_remaps_slots(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        '<filament id="1" type="PLA"/>'
        '<filament id="2" type="PETG"/>',
    )
    # Tell the printer: filament 1 -> slot 3, filament 2 -> slot 1
    trays = {1: _tray(1, "PETG"), 3: _tray(3, "PLA")}
    rep = preflight(
        p,
        config=_config_with_printer(),
        ams_mapping=[2, 0],  # 0-indexed -> slot 3 and slot 1
        ams_query=lambda _c: trays,
    )
    assert rep.ams_mapping == [2, 0]
    assert rep.compatibility[0].target_slot == 3
    assert rep.compatibility[1].target_slot == 1
    assert rep.ok


def test_preflight_unsliced_3mf_skips_type_check(tmp_path: Path) -> None:
    p = _make_unsliced_3mf(
        tmp_path / "x.3mf",
        '<object id="1"><metadata key="extruder" value="1"/></object>',
    )
    trays = {1: _tray(1, "PLA")}
    rep = preflight(p, config=_config_with_printer(), ams_query=lambda _c: trays)
    assert rep.source == "model_settings"
    # Empty expected_type means we can't check, but slot is populated -> ok
    assert rep.ok
    assert rep.compatibility[0].expected_type == ""


def test_preflight_unknown_3mf(tmp_path: Path) -> None:
    p = _make_empty_3mf(tmp_path / "x.3mf")
    rep = preflight(p, config=_config_with_printer(), ams_query=lambda _c: {1: _tray(1, "PLA")})
    assert rep.source == "unknown"
    assert rep.filaments == []
    assert rep.compatibility == []
    assert rep.ok  # vacuously


def test_preflight_ams_unreachable_warns(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        '<filament id="1" type="PLA"/>',
    )
    rep = preflight(p, config=_config_with_printer(), ams_query=lambda _c: None)
    assert any("unreachable" in w.lower() for w in rep.warnings)
    # Slot reported empty because we treat None trays as no info.
    assert not rep.ok


def test_preflight_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(DispatchError):
        preflight(tmp_path / "nope.3mf", config=_config_with_printer())


def test_preflight_human_summary_contains_filaments(tmp_path: Path) -> None:
    p = _make_sliced_3mf(
        tmp_path / "x.3mf",
        '<filament id="1" type="PLA"/>'
        '<filament id="2" type="PETG"/>',
    )
    trays = {1: _tray(1, "PLA"), 2: _tray(2, "ABS")}
    rep = preflight(p, config=_config_with_printer(), ams_query=lambda _c: trays)
    text = rep.human_summary()
    assert "PLA" in text
    assert "PETG" in text
    assert "ABS" in text
    assert "FAIL" in text


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def test_upload_happy_path(tmp_path: Path) -> None:
    f = tmp_path / "print.3mf"
    f.write_bytes(b"x")
    captured: dict[str, object] = {}

    def fake_upload(path: Path, name: str, config: Config) -> str:
        captured["path"] = path
        captured["name"] = name
        return "226 Transfer complete."

    name = upload(f, config=_config_with_printer(), upload_fn=fake_upload)
    assert name == "print.3mf"
    assert captured["path"] == f
    assert captured["name"] == "print.3mf"


def test_upload_custom_remote_name(tmp_path: Path) -> None:
    f = tmp_path / "long-name-here.3mf"
    f.write_bytes(b"x")
    name = upload(
        f,
        config=_config_with_printer(),
        remote_name="short.3mf",
        upload_fn=lambda _p, _n, _c: "226",
    )
    assert name == "short.3mf"


def test_upload_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DispatchError) as exc:
        upload(
            tmp_path / "nope.3mf",
            config=_config_with_printer(),
            upload_fn=lambda *_: "226",
        )
    assert "not found" in str(exc.value)


def test_upload_no_226_in_response(tmp_path: Path) -> None:
    f = tmp_path / "x.3mf"
    f.write_bytes(b"x")
    with pytest.raises(DispatchError) as exc:
        upload(f, config=_config_with_printer(), upload_fn=lambda *_: "500 Server error")
    assert "226" in str(exc.value)


def test_upload_missing_credentials(tmp_path: Path) -> None:
    f = tmp_path / "x.3mf"
    f.write_bytes(b"x")
    with pytest.raises(DispatchError) as exc:
        upload(f, config=Config(), upload_fn=lambda *_: "226")
    assert "credentials" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_happy_path() -> None:
    captured: dict[str, object] = {}

    def fake_start(filename, plate, mapping, use_ams, config):
        captured.update(
            filename=filename, plate=plate, mapping=mapping, use_ams=use_ams
        )
        return True

    ok = start(
        "print.3mf",
        config=_config_with_printer(),
        plate=2,
        ams_mapping=[1, 0],
        start_fn=fake_start,
    )
    assert ok
    assert captured == {
        "filename": "print.3mf",
        "plate": 2,
        "mapping": [1, 0],
        "use_ams": True,
    }


def test_start_default_mapping() -> None:
    captured: dict[str, list[int]] = {}

    def fake_start(filename, plate, mapping, use_ams, config):
        captured["m"] = mapping
        return True

    start("print.3mf", config=_config_with_printer(), start_fn=fake_start)
    assert captured["m"] == [0]


def test_start_bad_plate() -> None:
    with pytest.raises(DispatchError):
        start("p.3mf", config=_config_with_printer(), plate=0, start_fn=lambda *_: True)


def test_start_missing_credentials() -> None:
    with pytest.raises(DispatchError):
        start("p.3mf", config=Config(), start_fn=lambda *_: True)


def test_start_printer_rejects() -> None:
    with pytest.raises(DispatchError) as exc:
        start("p.3mf", config=_config_with_printer(), start_fn=lambda *_: False)
    assert "rejected" in str(exc.value).lower()
