import io
import re
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from bambu_prep.patch import PatchError, finalize_cli_output, patch_filament_slots


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


# ----------------------------------------------------------------------- finalize_cli_output


def _make_split_form_cli_output(tmp_path: Path, *, slot_per_object: int = 1) -> Path:
    """Build a .3mf that mimics bambu-studio.exe's broken CLI output:
    split-file 3MF Production Extension, malformed model_settings.config,
    production-extension cruft.
    """
    root_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
        'requiredextensions="p">\n'
        ' <resources>\n'
        '  <object id="2" p:UUID="00000001-aaaa-bbbb-cccc-dddddddddddd" type="model">\n'
        '   <components>\n'
        '    <component p:path="/3D/Objects/object_1.model" objectid="1" '
        'p:UUID="00010000-aaaa-bbbb-cccc-dddddddddddd" '
        'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
        '   </components>\n'
        '  </object>\n'
        ' </resources>\n'
        ' <build p:UUID="aaaa-bbbb-cccc-dddd-eeee">\n'
        '  <item objectid="2" p:UUID="00000002-aaaa-bbbb-cccc-dddddddddddd" '
        'transform="1 0 0 0 1 0 0 0 1 100 100 5" printable="1"/>\n'
        ' </build>\n'
        '</model>\n'
    )
    object_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model>\n'
        ' <resources>\n'
        '  <object id="1" type="model">\n'
        '   <mesh>\n'
        '    <vertices>\n'
        '     <vertex x="0" y="0" z="0"/>\n'
        '     <vertex x="1" y="0" z="0"/>\n'
        '     <vertex x="0" y="1" z="0"/>\n'
        '    </vertices>\n'
        '    <triangles>\n'
        '     <triangle v1="0" v2="1" v3="2"/>\n'
        '    </triangles>\n'
        '   </mesh>\n'
        '  </object>\n'
        ' </resources>\n'
        '</model>\n'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships>\n'
        ' <Relationship Target="/3D/Objects/object_1.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        '</Relationships>\n'
    )
    model_settings_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>\n'
        '  <object id="2">\n'
        '    <metadata key="name" value="cube.stl"/>\n'
        '    <metadata key="compatible_printers" value=""Bambu Lab A1 0.4 nozzle""/>\n'
        '    <metadata key="default_acceleration" value="6000"/>\n'
        '    <metadata key="inherits" value="fdm_process_single_0.20"/>\n'
        '    <metadata key="print_settings_id" value="0.20mm Standard @BBL A1"/>\n'
        '    <metadata key="travel_speed" value="700"/>\n'
        '    <metadata face_count="1"/>\n'
        '  </object>\n'
        '  <plate>\n'
        '    <metadata key="plater_id" value="1"/>\n'
        '    <metadata key="gcode_file" value=""/>\n'
        '  </plate>\n'
        '</config>\n'
    )

    path = tmp_path / "cli_output.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", root_xml)
        zf.writestr("3D/Objects/object_1.model", object_xml)
        zf.writestr("3D/_rels/3dmodel.model.rels", rels_xml)
        zf.writestr("Metadata/model_settings.config", model_settings_xml)
    return path


def test_finalize_inlines_geometry(tmp_path: Path) -> None:
    """After finalize, 3D/Objects/ is gone and geometry is inside 3D/3dmodel.model."""
    path = _make_split_form_cli_output(tmp_path)
    finalize_cli_output(path, [1])

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        root = zf.read("3D/3dmodel.model").decode("utf-8")

    assert not any(n.startswith("3D/Objects/") for n in names)
    assert "3D/_rels/3dmodel.model.rels" not in names
    assert "<mesh>" in root
    assert "<vertices>" in root
    assert "<components>" not in root


def test_finalize_strips_production_extension(tmp_path: Path) -> None:
    """xmlns:p, requiredextensions, p:UUID must all be removed after inlining."""
    path = _make_split_form_cli_output(tmp_path)
    finalize_cli_output(path, [1])

    with zipfile.ZipFile(path) as zf:
        root = zf.read("3D/3dmodel.model").decode("utf-8")

    assert "xmlns:p=" not in root
    assert 'requiredextensions="p"' not in root
    assert "p:UUID=" not in root


def test_finalize_sanitizes_model_settings(tmp_path: Path) -> None:
    """Malformed CLI-injected metadata keys are stripped from model_settings.config."""
    path = _make_split_form_cli_output(tmp_path)
    finalize_cli_output(path, [1])

    with zipfile.ZipFile(path) as zf:
        cfg = zf.read("Metadata/model_settings.config").decode("utf-8")

    # The malformed line and its siblings should all be gone.
    assert "compatible_printers" not in cfg
    assert "default_acceleration" not in cfg
    assert "inherits" not in cfg
    assert "print_settings_id" not in cfg
    assert "travel_speed" not in cfg
    assert 'gcode_file" value=""' not in cfg
    # But the valid name metadata is preserved.
    assert 'key="name" value="cube.stl"' in cfg


def test_finalize_injects_extruder_metadata(tmp_path: Path) -> None:
    path = _make_split_form_cli_output(tmp_path)
    finalize_cli_output(path, [2])

    with zipfile.ZipFile(path) as zf:
        cfg = zf.read("Metadata/model_settings.config").decode("utf-8")

    assert '<metadata key="extruder" value="2"/>' in cfg


def test_finalize_idempotent(tmp_path: Path) -> None:
    """Running finalize twice produces the same output."""
    path = _make_split_form_cli_output(tmp_path)
    finalize_cli_output(path, [1])
    with zipfile.ZipFile(path) as zf:
        first_root = zf.read("3D/3dmodel.model")
        first_cfg = zf.read("Metadata/model_settings.config")

    finalize_cli_output(path, [1])
    with zipfile.ZipFile(path) as zf:
        second_root = zf.read("3D/3dmodel.model")
        second_cfg = zf.read("Metadata/model_settings.config")

    assert first_root == second_root
    assert first_cfg == second_cfg
