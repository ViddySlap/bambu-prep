"""Tests for bambu_prep.slice (OrcaSlicer CLI wrapper)."""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from bambu_prep.config import Config, Paths, load_config
from bambu_prep.slice import (
    SliceError,
    SliceResult,
    _default_output_path,
    _inject_model_id,
    _patch_output_model_id,
    _slice_info_has_model_id,
    slice_3mf,
)


# ---------------------------------------------------------------------------
# unit tests (no OrcaSlicer needed)
# ---------------------------------------------------------------------------


def test_default_output_path_adds_gcode_suffix(tmp_path: Path) -> None:
    src = tmp_path / "model.3mf"
    out = _default_output_path(src)
    assert out.name == "model.gcode.3mf"
    assert out.parent == tmp_path


def test_default_output_path_preserves_existing_gcode_suffix(tmp_path: Path) -> None:
    """Re-slicing a file that already ended in .gcode.3mf shouldn't compound."""
    src = tmp_path / "model.gcode.3mf"
    out = _default_output_path(src)
    assert out.name == "model.gcode.3mf"


def test_slice_info_has_model_id_detects_match() -> None:
    xml = '<metadata key="printer_model_id" value="N2S"/>'
    assert _slice_info_has_model_id(xml, "N2S")
    assert not _slice_info_has_model_id(xml, "BL-P001")


def test_inject_model_id_adds_when_missing() -> None:
    xml = '<config>\n  <plate index="1">\n    <metadata key="other" value="x"/>\n  </plate>\n</config>'
    new = _inject_model_id(xml, "N2S")
    assert 'printer_model_id" value="N2S"' in new
    assert 'key="other"' in new  # didn't destroy existing tags


def test_inject_model_id_returns_input_on_no_plate_tag() -> None:
    xml = "<config></config>"
    new = _inject_model_id(xml, "N2S")
    assert new == xml


def test_patch_output_model_id_sets_empty_to_target(tmp_path: Path) -> None:
    """Simulate OrcaSlicer's output: slice_info.config with empty value."""
    p = tmp_path / "sliced.3mf"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(
            "Metadata/slice_info.config",
            '<config><plate>\n<metadata key="printer_model_id" value=""/>\n</plate></config>',
        )
        zf.writestr("Metadata/plate_1.gcode", "; gcode body\n")
    changed = _patch_output_model_id(p, "N2S")
    assert changed
    with zipfile.ZipFile(p, "r") as zf:
        si = zf.read("Metadata/slice_info.config").decode()
    assert 'value="N2S"' in si
    assert 'value=""' not in si


def test_patch_output_model_id_idempotent_when_already_target(tmp_path: Path) -> None:
    p = tmp_path / "sliced.3mf"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(
            "Metadata/slice_info.config",
            '<config><plate>\n<metadata key="printer_model_id" value="N2S"/>\n</plate></config>',
        )
        zf.writestr("Metadata/plate_1.gcode", "; body\n")
    changed = _patch_output_model_id(p, "N2S")
    assert not changed


def test_patch_output_model_id_injects_when_field_absent(tmp_path: Path) -> None:
    p = tmp_path / "sliced.3mf"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(
            "Metadata/slice_info.config",
            '<config><plate>\n<metadata key="other" value="x"/>\n</plate></config>',
        )
        zf.writestr("Metadata/plate_1.gcode", "; body\n")
    changed = _patch_output_model_id(p, "N2S")
    assert changed
    with zipfile.ZipFile(p, "r") as zf:
        si = zf.read("Metadata/slice_info.config").decode()
    assert 'printer_model_id" value="N2S"' in si
    assert 'key="other"' in si


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_slice_missing_input_raises(tmp_path: Path) -> None:
    cfg = Config()
    with pytest.raises(SliceError):
        slice_3mf(tmp_path / "missing.3mf", config=cfg)


def test_slice_missing_orcaslicer_raises(tmp_path: Path) -> None:
    src = tmp_path / "in.3mf"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("Metadata/project_settings.config", "{}")
    cfg = Config(paths=Paths(orcaslicer_exe=tmp_path / "nonexistent.exe"))
    with pytest.raises(SliceError) as exc:
        slice_3mf(src, config=cfg)
    assert "OrcaSlicer" in str(exc.value)


# ---------------------------------------------------------------------------
# integration: real OrcaSlicer on the retargeted clicker fidget
# ---------------------------------------------------------------------------

_CLICKER_FETCH = Path(
    "D:/Baros Design Co. Dropbox/Ben Baros/AI AGENTS/3D PRINTING/clicker-fidget/"
    "Single_Color_-_Let_Print_Cool_Before_Clicking!!!_2026-05-11.3mf"
)


def test_slice_real_retargeted_clicker_fidget(tmp_path: Path) -> None:
    """End-to-end: slice the retargeted clicker fidget; check gcode has full
    A1 start macros and slice_info has N2S printer_model_id.

    Skips when OrcaSlicer or the local artifact isn't present.
    """
    if not _CLICKER_FETCH.is_file():
        pytest.skip("local retargeted clicker artifact not present")

    cfg = load_config()
    if not cfg.paths.orcaslicer_exe.is_file():
        pytest.skip(f"OrcaSlicer not installed at {cfg.paths.orcaslicer_exe}")

    # Copy the file so we don't mutate the canonical artifact
    src = tmp_path / "clicker.3mf"
    src.write_bytes(_CLICKER_FETCH.read_bytes())

    result = slice_3mf(src, config=cfg, output_path=tmp_path / "clicker.gcode.3mf")
    assert isinstance(result, SliceResult)
    assert result.output_path.is_file()
    assert result.duration_seconds > 0

    # Inspect the produced gcode for A1 start macros
    with zipfile.ZipFile(result.output_path, "r") as zf:
        gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")
        si = zf.read("Metadata/slice_info.config").decode("utf-8")

    # The litmus test: full A1 start template, not a stub.
    m1006_count = sum(1 for line in gcode.splitlines() if line.startswith("M1006"))
    assert m1006_count >= 10, f"expected many M1006 calibration moves, got {m1006_count}"
    assert "G29" in gcode, "expected bed-leveling G29 macro"
    assert ";===== machine: A1" in gcode, "expected A1 header in machine_start_gcode"

    # slice_info.config printer_model_id must be N2S after our post-patch
    assert 'value="N2S"' in si