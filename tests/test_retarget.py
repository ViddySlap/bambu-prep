"""Tests for bambu_prep.retarget."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from bambu_prep.config import load_config
from bambu_prep.retarget import (
    RetargetError,
    RetargetResult,
    _load_profile_chain,
    _patch_project_settings,
    _patch_slice_info,
    retarget,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _x1c_project_settings() -> dict:
    """Minimal subset of project_settings.config as a MakerWorld X1C file ships it."""
    return {
        "printer_model": "Bambu Lab X1 Carbon",
        "printer_settings_id": "Bambu Lab X1 Carbon 0.4 nozzle",
        "print_settings_id": "0.20mm Standard @BBL X1C",
        "inherits_group": ["0.20mm Standard @BBL X1C", "Bambu PLA Matte @BBL X1C", "", "", "", "Bambu Lab X1 Carbon 0.4 nozzle"],
        "printer_structure": "corexy",
        "filament_settings_id": [
            "Bambu PLA Matte @BBL X1C",
            "Bambu PLA Matte @BBL X1C",
            "Bambu PLA Matte @BBL X1C",
            "Bambu PLA Matte @BBL X1C",
        ],
        "print_compatible_printers": [
            "Bambu Lab X1 Carbon 0.4 nozzle",
            "Bambu Lab X1 0.4 nozzle",
            "Bambu Lab P1S 0.4 nozzle",
        ],
        "bed_exclude_area": ["0x0", "18x0", "18x28", "0x28"],
        "machine_start_gcode": ";===== machine: X1 ===\nM73 P0 R19\n...stub of X1 start gcode...",
        "machine_end_gcode": ";===== X1 end ===\nM104 S0\n",
        "machine_max_speed_x": ["500", "500"],
        "machine_max_speed_y": ["500", "500"],
        # process-bound fields that should NOT change
        "layer_height": "0.20",
        "sparse_infill_density": "20%",
        "wall_loops": "3",
    }


def _make_3mf(
    path: Path,
    *,
    project_settings: dict,
    slice_info_model_id: str | None = None,
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    """Build a minimal .3mf zip with the given project_settings JSON."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps(project_settings, indent=4),
        )
        if slice_info_model_id is not None:
            zf.writestr(
                "Metadata/slice_info.config",
                f'<?xml version="1.0"?>\n<config><plate>\n'
                f'<metadata key="printer_model_id" value="{slice_info_model_id}"/>\n'
                f"</plate></config>",
            )
        for name, data in (extra_members or {}).items():
            zf.writestr(name, data)
    return path


def _read_project_settings(path: Path) -> dict:
    with zipfile.ZipFile(path, "r") as zf:
        return json.loads(zf.read("Metadata/project_settings.config").decode())


def _read_zip_member(path: Path, member: str) -> str:
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read(member).decode()


# ---------------------------------------------------------------------------
# _load_profile_chain
# ---------------------------------------------------------------------------


def test_load_profile_chain_resolves_a1_machine_includes() -> None:
    """Loading the real A1 machine profile must surface the start_gcode template
    (the field that comes from one of the included template files)."""
    cfg = load_config()
    # Resolve to find the directory holding the A1 machine profile.
    from bambu_prep.profiles import resolve as profile_resolve

    a1_machine = profile_resolve(cfg, "machine", "Bambu Lab A1 0.4 nozzle")
    chain = _load_profile_chain("Bambu Lab A1 0.4 nozzle", a1_machine.path.parent)
    assert chain.get("printer_model") == "Bambu Lab A1"
    assert chain.get("printer_variant") == "0.4"
    # The start_gcode lives in a template file referenced via "include".
    # If our chain loader honors includes, we should see the full A1 template.
    assert "machine_start_gcode" in chain
    start = chain["machine_start_gcode"]
    assert "M1006" in start, "A1's start template should contain M1006 calibration moves"
    assert "G29" in start, "A1's start template should contain bed leveling (G29)"
    # The header comment is the cleanest X1C-vs-A1 differentiator
    assert ";===== machine: A1" in start
    assert ";===== machine: X1" not in start


def test_load_profile_chain_handles_missing_profile_gracefully(tmp_path: Path) -> None:
    """An empty dir = empty result, not a crash."""
    chain = _load_profile_chain("does not exist", tmp_path)
    assert chain == {}


# ---------------------------------------------------------------------------
# _patch_slice_info
# ---------------------------------------------------------------------------


def test_patch_slice_info_replaces_existing_model_id() -> None:
    xml = (
        '<config><plate>\n'
        '<metadata key="printer_model_id" value="BL-P001"/>\n'
        "</plate></config>"
    )
    new_xml, changed = _patch_slice_info(xml, "N2S")
    assert changed
    assert 'value="N2S"' in new_xml
    assert 'value="BL-P001"' not in new_xml


def test_patch_slice_info_no_op_when_already_target() -> None:
    xml = '<metadata key="printer_model_id" value="N2S"/>'
    new_xml, changed = _patch_slice_info(xml, "N2S")
    assert not changed
    assert new_xml == xml


def test_patch_slice_info_no_op_when_field_absent() -> None:
    xml = "<config></config>"
    new_xml, changed = _patch_slice_info(xml, "N2S")
    assert not changed
    assert new_xml == xml


# ---------------------------------------------------------------------------
# _patch_project_settings (unit-level)
# ---------------------------------------------------------------------------


def test_patch_project_settings_flips_identity() -> None:
    ps = _x1c_project_settings()
    machine_fields = {"machine_max_speed_x": ["500", "200"], "bed_exclude_area": []}
    filament_fields = {"nozzle_temperature": ["220"]}
    n_changed = _patch_project_settings(
        ps,
        machine_fields=machine_fields,
        filament_fields=filament_fields,
        target_machine_model="Bambu Lab A1",
        target_printer_settings_id="Bambu Lab A1 0.4 nozzle",
        target_print_settings_id="0.20mm Standard @BBL A1",
        target_filament_settings_id="Bambu PLA Basic @BBL A1",
    )
    assert ps["printer_model"] == "Bambu Lab A1"
    assert ps["printer_settings_id"] == "Bambu Lab A1 0.4 nozzle"
    assert ps["print_settings_id"] == "0.20mm Standard @BBL A1"
    assert ps["filament_settings_id"] == ["Bambu PLA Basic @BBL A1"] * 4
    assert ps["print_compatible_printers"] == ["Bambu Lab A1 0.4 nozzle"]
    assert ps["machine_max_speed_x"] == ["500", "200"]
    assert ps["bed_exclude_area"] == []
    assert ps["nozzle_temperature"] == ["220"] * 4  # broadcast to filament count
    # Process-bound fields untouched
    assert ps["layer_height"] == "0.20"
    assert ps["sparse_infill_density"] == "20%"
    assert n_changed > 0


def test_patch_project_settings_idempotent_when_already_target() -> None:
    """Running retarget on an already-A1 file should report zero changes."""
    ps = {
        "printer_model": "Bambu Lab A1",
        "printer_settings_id": "Bambu Lab A1 0.4 nozzle",
        "filament_settings_id": ["Bambu PLA Basic @BBL A1"],
        "print_compatible_printers": ["Bambu Lab A1 0.4 nozzle"],
        "machine_max_speed_x": ["500", "200"],
    }
    ps["print_settings_id"] = "0.20mm Standard @BBL A1"
    n_changed = _patch_project_settings(
        ps,
        machine_fields={"machine_max_speed_x": ["500", "200"]},
        filament_fields={},
        target_machine_model="Bambu Lab A1",
        target_printer_settings_id="Bambu Lab A1 0.4 nozzle",
        target_print_settings_id="0.20mm Standard @BBL A1",
        target_filament_settings_id="Bambu PLA Basic @BBL A1",
    )
    assert n_changed == 0


# ---------------------------------------------------------------------------
# retarget (integration with synthetic .3mf)
# ---------------------------------------------------------------------------


def test_retarget_synthetic_x1c_to_a1(tmp_path: Path) -> None:
    src = _make_3mf(
        tmp_path / "x1c.3mf",
        project_settings=_x1c_project_settings(),
        slice_info_model_id="BL-P001",
        extra_members={"3D/3dmodel.model": b"<model/>"},
    )
    cfg = load_config()
    result = retarget(src, config=cfg)

    assert isinstance(result, RetargetResult)
    assert result.fields_changed > 0
    assert not result.was_already_target
    assert result.target_machine == "Bambu Lab A1 0.4 nozzle"
    assert result.target_machine_model_id == "N2S"

    ps = _read_project_settings(src)
    assert ps["printer_model"] == "Bambu Lab A1"
    assert ps["printer_settings_id"] == "Bambu Lab A1 0.4 nozzle"
    assert ps["print_settings_id"] == "0.20mm Standard @BBL A1"
    # inherits_group: first entry → A1 process; later entries cleared
    assert ps["inherits_group"][0] == "0.20mm Standard @BBL A1"
    assert all(e == "" for e in ps["inherits_group"][1:])
    # All 4 filament slots get the A1 default
    assert ps["filament_settings_id"] == ["Bambu PLA Basic @BBL A1"] * 4
    # bed_exclude_area should be A1's empty list (A1 has no exclusion zone)
    assert ps["bed_exclude_area"] == []
    # machine_start_gcode must now be the full A1 template, not the X1C stub
    assert "M1006" in ps["machine_start_gcode"]
    assert "G29" in ps["machine_start_gcode"]
    # Unique-to-A1 header comment present, X1 absent
    assert ";===== machine: A1" in ps["machine_start_gcode"]
    assert ";===== machine: X1" not in ps["machine_start_gcode"]
    # Process-bound settings preserved
    assert ps["layer_height"] == "0.20"
    assert ps["sparse_infill_density"] == "20%"
    assert ps["wall_loops"] == "3"

    # slice_info.config flipped
    si = _read_zip_member(src, "Metadata/slice_info.config")
    assert 'value="N2S"' in si
    assert 'value="BL-P001"' not in si

    # 3D geometry preserved
    assert _read_zip_member(src, "3D/3dmodel.model") == "<model/>"


def test_retarget_writes_to_explicit_output_path(tmp_path: Path) -> None:
    src = _make_3mf(tmp_path / "in.3mf", project_settings=_x1c_project_settings())
    dst = tmp_path / "out.3mf"
    cfg = load_config()
    result = retarget(src, config=cfg, output_path=dst)
    assert result.output_path == dst
    assert dst.is_file()
    # The original is untouched
    src_ps = _read_project_settings(src)
    assert src_ps["printer_model"] == "Bambu Lab X1 Carbon"
    # The new file is A1
    dst_ps = _read_project_settings(dst)
    assert dst_ps["printer_model"] == "Bambu Lab A1"


def test_retarget_idempotent_on_already_a1(tmp_path: Path) -> None:
    """Run retarget twice; the second pass reports zero further changes."""
    src = _make_3mf(tmp_path / "x.3mf", project_settings=_x1c_project_settings())
    cfg = load_config()
    retarget(src, config=cfg)
    second = retarget(src, config=cfg)
    assert second.was_already_target, (
        f"second pass should report no changes, got fields_changed={second.fields_changed}"
    )


def test_retarget_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        retarget(tmp_path / "missing.3mf", config=load_config())


def test_retarget_without_project_settings_raises(tmp_path: Path) -> None:
    """A .3mf with no project_settings.config can't be retargeted."""
    path = tmp_path / "naked.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(RetargetError):
        retarget(path, config=load_config())


# ---------------------------------------------------------------------------
# retarget against the real clicker fidget artifact
# ---------------------------------------------------------------------------

_CLICKER_FETCH = Path(
    "D:/Baros Design Co. Dropbox/Ben Baros/AI AGENTS/3D PRINTING/clicker-fidget/"
    "Single_Color_-_Let_Print_Cool_Before_Clicking!!!_2026-05-11.3mf"
)


def test_retarget_real_clicker_fidget_to_a1(tmp_path: Path) -> None:
    """End-to-end test against the actual MakerWorld download from 2026-05-11.

    Skips if the artifact isn't on this machine (e.g. CI / clean checkout).
    """
    if not _CLICKER_FETCH.is_file():
        pytest.skip("local MakerWorld clicker artifact not present")

    # Copy the file so we don't mutate the original
    src = tmp_path / "clicker.3mf"
    src.write_bytes(_CLICKER_FETCH.read_bytes())

    # Read pre-retarget state for the layer_height invariant check (process
    # settings shouldn't change). We don't assert printer_model here because
    # the file on disk may have already been retargeted by a prior session;
    # the test verifies the post-retarget end state regardless.
    before = _read_project_settings(src)

    cfg = load_config()
    retarget(src, config=cfg)

    after = _read_project_settings(src)
    assert after["printer_model"] == "Bambu Lab A1"
    assert after["printer_settings_id"] == "Bambu Lab A1 0.4 nozzle"
    assert after["print_settings_id"] == "0.20mm Standard @BBL A1"
    # Geometry-and-process settings preserved
    assert after.get("layer_height") == before.get("layer_height")
    # All filament slots retargeted
    assert all(f == "Bambu PLA Basic @BBL A1" for f in after["filament_settings_id"])
    # The full A1 start gcode template is present (the critical bit)
    assert "M1006" in after["machine_start_gcode"]
    assert "G29" in after["machine_start_gcode"]
    # The A1 header comment is there; the X1 header isn't.
    assert ";===== machine: A1" in after["machine_start_gcode"]
    assert ";===== machine: X1" not in after["machine_start_gcode"]
    # bed_exclude_area cleared (A1 has none)
    assert after["bed_exclude_area"] == []
