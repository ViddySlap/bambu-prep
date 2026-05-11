import io
import re
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from bambu_prep.patch import PatchError, patch_filament_slots


def _make_3mf(tmp_path: Path, *, object_count: int, existing_extruders: list[int] | None = None) -> Path:
    """Build a minimal .3mf with N objects. Optionally bake in stale extruder
    entries (one per object) to verify idempotent replacement."""
    existing_extruders = existing_extruders or []
    bodies = []
    for i in range(object_count):
        body = f'    <metadata key="name" value="cube_{i + 1}"/>'
        if i < len(existing_extruders):
            body += f'\n    <metadata key="extruder" value="{existing_extruders[i]}"/>'
        bodies.append(f'  <object id="{2 * (i + 1)}">\n{body}\n  </object>')
    config_text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>\n' + "\n".join(bodies) + '\n</config>\n'
    )
    path = tmp_path / "out.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/model_settings.config", config_text)
        zf.writestr("Metadata/other.txt", "untouched")
    return path


def _extruder_counts(path: Path) -> Counter[str]:
    with zipfile.ZipFile(path) as zf:
        cfg = zf.read("Metadata/model_settings.config").decode("utf-8")
    return Counter(re.findall(r'<metadata\s+key="extruder"\s+value="(\d+)"', cfg))


def test_patch_injects_metadata(tmp_path: Path) -> None:
    path = _make_3mf(tmp_path, object_count=5)
    patch_filament_slots(path, [1, 1, 2, 2, 2])
    assert _extruder_counts(path) == Counter({"1": 2, "2": 3})


def test_patch_idempotent_replaces_existing(tmp_path: Path) -> None:
    """Running patch twice with different slots yields the second pass's slots."""
    path = _make_3mf(tmp_path, object_count=3)
    patch_filament_slots(path, [1, 1, 1])
    patch_filament_slots(path, [2, 3, 4])
    assert _extruder_counts(path) == Counter({"2": 1, "3": 1, "4": 1})


def test_patch_replaces_stale_extruder(tmp_path: Path) -> None:
    """If a .3mf already has extruder metadata, the patch overwrites it."""
    path = _make_3mf(tmp_path, object_count=2, existing_extruders=[3, 4])
    patch_filament_slots(path, [1, 2])
    assert _extruder_counts(path) == Counter({"1": 1, "2": 1})


def test_patch_preserves_other_zip_members(tmp_path: Path) -> None:
    path = _make_3mf(tmp_path, object_count=1)
    patch_filament_slots(path, [1])
    with zipfile.ZipFile(path) as zf:
        assert "Metadata/other.txt" in zf.namelist()
        assert zf.read("Metadata/other.txt") == b"untouched"


def test_patch_mismatched_object_count_raises(tmp_path: Path) -> None:
    path = _make_3mf(tmp_path, object_count=3)
    with pytest.raises(PatchError, match="object count"):
        patch_filament_slots(path, [1, 2])


def test_patch_missing_config_raises(tmp_path: Path) -> None:
    path = tmp_path / "out.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/other.txt", "no model settings")
    with pytest.raises(PatchError, match="model_settings"):
        patch_filament_slots(path, [1])


def test_patch_bad_zip_raises(tmp_path: Path) -> None:
    path = tmp_path / "out.3mf"
    path.write_bytes(b"not a zip")
    with pytest.raises(PatchError, match="not a valid zip"):
        patch_filament_slots(path, [1])


def test_patch_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PatchError, match="no .3mf"):
        patch_filament_slots(tmp_path / "nope.3mf", [1])


def test_patch_handles_unescaped_quotes_in_config(tmp_path: Path) -> None:
    """Bambu's real config has unescaped double quotes (compatible_printers).
    The patch must not choke on them."""
    path = tmp_path / "out.3mf"
    config_text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>\n'
        '  <object id="2">\n'
        '    <metadata key="name" value="cube"/>\n'
        '    <metadata key="compatible_printers" value=""Bambu Lab A1 0.4 nozzle""/>\n'
        '  </object>\n'
        '</config>\n'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/model_settings.config", config_text)

    patch_filament_slots(path, [3])
    assert _extruder_counts(path) == Counter({"3": 1})
    # The unescaped-quote line should still be in the output verbatim.
    with zipfile.ZipFile(path) as zf:
        out = zf.read("Metadata/model_settings.config").decode("utf-8")
    assert '""Bambu Lab A1 0.4 nozzle""' in out
