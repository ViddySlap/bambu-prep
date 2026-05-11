import json
from pathlib import Path

import pytest

from bambu_prep.config import Config, Paths
from bambu_prep.profiles import (
    Profile,
    ProfileError,
    list_profiles,
    resolve,
)


def _write_preset(
    path: Path,
    *,
    name: str,
    kind: str,
    instantiation: str = "true",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": kind, "name": name, "instantiation": instantiation}),
        encoding="utf-8",
    )


def _build_config(tmp_path: Path, *, with_user: bool) -> Config:
    resources = tmp_path / "BambuStudio" / "resources"
    user = tmp_path / "user" if with_user else None
    return Config(
        paths=Paths(
            bambu_studio_exe=tmp_path / "bambu-studio.exe",
            bambu_resources_dir=resources,
            bambu_user_dir=user,
            temp_scratch_dir=tmp_path / "tmp",
        )
    )


def test_list_vendor_only(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=False)
    vendor_machine_dir = cfg.paths.bambu_resources_dir / "profiles" / "BBL" / "machine"
    _write_preset(vendor_machine_dir / "Fake A1 0.4.json", name="Fake A1 0.4", kind="machine")
    _write_preset(vendor_machine_dir / "Fake A1 0.6.json", name="Fake A1 0.6", kind="machine")

    machines = list_profiles(cfg, "machine")
    assert [m.name for m in machines] == ["Fake A1 0.4", "Fake A1 0.6"]
    assert all(m.source == "vendor" for m in machines)


def test_list_filters_templates_and_base(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=False)
    machine_dir = cfg.paths.bambu_resources_dir / "profiles" / "BBL" / "machine"
    filament_dir = cfg.paths.bambu_resources_dir / "profiles" / "BBL" / "filament"

    _write_preset(machine_dir / "Real A1.json", name="Real A1", kind="machine")
    _write_preset(
        machine_dir / "Real A1 template change_filament_gcode.json",
        name="should be hidden",
        kind="machine",
    )
    _write_preset(filament_dir / "PLA Real.json", name="PLA Real", kind="filament")
    _write_preset(filament_dir / "PLA @base.json", name="PLA @base", kind="filament")

    assert [m.name for m in list_profiles(cfg, "machine")] == ["Real A1"]
    assert [f.name for f in list_profiles(cfg, "filament")] == ["PLA Real"]


def test_list_filters_non_instantiable_and_wrong_kind(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=False)
    process_dir = cfg.paths.bambu_resources_dir / "profiles" / "BBL" / "process"

    _write_preset(process_dir / "Real Process.json", name="Real Process", kind="process")
    _write_preset(
        process_dir / "Not Instantiable.json",
        name="Not Instantiable",
        kind="process",
        instantiation="false",
    )
    _write_preset(process_dir / "Mistyped.json", name="Mistyped", kind="machine")

    names = [p.name for p in list_profiles(cfg, "process")]
    assert names == ["Real Process"]


def test_user_wins_over_vendor(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=True)
    vendor = cfg.paths.bambu_resources_dir / "profiles" / "BBL" / "filament"
    user = cfg.paths.bambu_user_dir / "filament"

    _write_preset(vendor / "Shared.json", name="Shared", kind="filament")
    _write_preset(vendor / "VendorOnly.json", name="VendorOnly", kind="filament")
    _write_preset(user / "Shared.json", name="Shared", kind="filament")
    _write_preset(user / "UserOnly.json", name="UserOnly", kind="filament")

    by_name = {p.name: p for p in list_profiles(cfg, "filament")}
    assert by_name["Shared"].source == "user"
    assert by_name["VendorOnly"].source == "vendor"
    assert by_name["UserOnly"].source == "user"


def test_resolve_fast_path(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=False)
    machine_dir = cfg.paths.bambu_resources_dir / "profiles" / "BBL" / "machine"
    _write_preset(machine_dir / "My Printer.json", name="My Printer", kind="machine")

    hit = resolve(cfg, "machine", "My Printer")
    assert hit.path.name == "My Printer.json"
    assert hit.source == "vendor"


def test_resolve_user_overrides(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=True)
    vendor = cfg.paths.bambu_resources_dir / "profiles" / "BBL" / "process"
    user = cfg.paths.bambu_user_dir / "process"

    _write_preset(vendor / "Std.json", name="Std", kind="process")
    _write_preset(user / "Std.json", name="Std", kind="process")

    hit = resolve(cfg, "process", "Std")
    assert hit.source == "user"


def test_resolve_falls_back_when_filename_differs(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=False)
    filament_dir = cfg.paths.bambu_resources_dir / "profiles" / "BBL" / "filament"
    # Filename intentionally doesn't match the preset's name field
    _write_preset(filament_dir / "renamed-file.json", name="Canonical Name", kind="filament")

    hit = resolve(cfg, "filament", "Canonical Name")
    assert hit.path.name == "renamed-file.json"


def test_resolve_miss_raises(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=False)
    machine_dir = cfg.paths.bambu_resources_dir / "profiles" / "BBL" / "machine"
    _write_preset(machine_dir / "Other.json", name="Other", kind="machine")

    with pytest.raises(ProfileError):
        resolve(cfg, "machine", "Nope")


def test_unknown_kind_raises(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=False)
    with pytest.raises(ValueError):
        list_profiles(cfg, "bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        resolve(cfg, "bogus", "x")  # type: ignore[arg-type]


def test_missing_vendor_dir_returns_empty(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path, with_user=False)
    # No directories created at all
    assert list_profiles(cfg, "machine") == []


# Optional: validate against the real Bambu Studio install if present.
_REAL_VENDOR = Path("C:/Program Files/Bambu Studio/resources/profiles/BBL/machine")


@pytest.mark.skipif(
    not _REAL_VENDOR.is_dir(),
    reason="Bambu Studio vendor profiles not installed",
)
def test_real_vendor_resolves_canonical_a1_preset() -> None:
    cfg = Config()
    hit = resolve(cfg, "machine", "Bambu Lab A1 0.4 nozzle")
    assert hit.path.is_file()
    assert hit.source == "vendor"
