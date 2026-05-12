import io
import json
import re
import subprocess
import zipfile
from collections import Counter
from pathlib import Path

import pytest
import trimesh

from bambu_prep.config import Behavior, Config, Paths
from bambu_prep.plate import (
    CliInput,
    PlateItem,
    PrepareError,
    build_cli_args,
    consolidate,
    prepare_plate,
)


def _write_cube(path: Path, side: float = 10.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    trimesh.creation.box(extents=(side, side, side)).export(path)


def _write_preset(path: Path, *, name: str, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": kind, "name": name, "instantiation": "true"}),
        encoding="utf-8",
    )


def _build_config(tmp_path: Path) -> Config:
    resources = tmp_path / "BambuStudio" / "resources"
    base = resources / "profiles" / "BBL"
    _write_preset(base / "machine" / "Test A1.json", name="Test A1", kind="machine")
    _write_preset(base / "process" / "Test 0.2mm.json", name="Test 0.2mm", kind="process")
    _write_preset(
        base / "filament" / "Test Matte Black.json", name="Test Matte Black", kind="filament"
    )
    _write_preset(base / "filament" / "Test White.json", name="Test White", kind="filament")
    return Config(
        paths=Paths(
            bambu_studio_exe=tmp_path / "bambu-studio.exe",
            bambu_resources_dir=resources,
            bambu_user_dir=None,
            temp_scratch_dir=tmp_path / "scratch",
        ),
        behavior=Behavior(ensure_on_bed=True),
    )


def _build_fake_3mf(cmd: list[str], *, plate_count: int = 1) -> bytes:
    """Construct a minimal .3mf zip with one <object> per CLI input clone
    plus matching <item> entries in 3D/3dmodel.model's <build> section.
    This is enough for prepare_plate's finalize_cli_output post-process
    (slot patching + transform patching) to run end-to-end.

    ``plate_count`` is no longer used (kept for back-compat with older
    tests; layout-based overflow detection means the CLI never auto-
    paginates in our pipeline).
    """
    del plate_count  # noqa: unused; retained for older test signatures
    idx = cmd.index("--clone-objects")
    counts = [int(x) for x in cmd[idx + 1].split(",")]
    total = sum(counts)

    object_ids = [2 * (i + 1) for i in range(total)]
    objects_xml = "\n".join(
        f'  <object id="{oid}">\n'
        f'    <metadata key="name" value="cube_{i + 1}"/>\n'
        f"  </object>"
        for i, oid in enumerate(object_ids)
    )
    config_text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<config>\n{objects_xml}\n  <plate>\n  </plate>\n</config>\n'
    )

    build_items_xml = "\n".join(
        f'  <item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>'
        for oid in object_ids
    )
    root_model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        ' <resources>\n'
        + "\n".join(
            f'  <object id="{oid}" type="model"><mesh/></object>'
            for oid in object_ids
        )
        + "\n </resources>\n"
        f" <build>\n{build_items_xml}\n </build>\n</model>\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/model_settings.config", config_text)
        zf.writestr("Metadata/project_settings.config", "{}")
        zf.writestr("3D/3dmodel.model", root_model)
    return buf.getvalue()


class FakeRunner:
    """Pluggable subprocess.run replacement for plate.py tests.

    Each entry in ``results`` is either ``(returncode, write_output)`` or
    ``(returncode, write_output, plate_count)`` to simulate the CLI
    auto-paginating across plates.
    """

    def __init__(
        self,
        results: list[tuple],
        output_path: Path,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._results = list(results)
        self._output_path = output_path
        self._stdout = stdout
        self._stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if not self._results:
            raise AssertionError("FakeRunner called more times than scripted")
        entry = self._results.pop(0)
        rc, write_output = entry[0], entry[1]
        plate_count = entry[2] if len(entry) >= 3 else 1
        if write_output:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            self._output_path.write_bytes(_build_fake_3mf(cmd, plate_count=plate_count))
        return subprocess.CompletedProcess(
            args=cmd, returncode=rc, stdout=self._stdout, stderr=self._stderr
        )


def _extruder_counts_in_3mf(path: Path) -> Counter[str]:
    """Read model_settings.config from a .3mf and tally extruder values."""
    with zipfile.ZipFile(path) as zf:
        cfg = zf.read("Metadata/model_settings.config").decode("utf-8")
    return Counter(re.findall(r'<metadata\s+key="extruder"\s+value="(\d+)"', cfg))


# ----------------------------------------------------------------------- consolidate


def test_consolidate_same_source_same_slot_collapses(tmp_path: Path) -> None:
    stl = tmp_path / "a.stl"
    items = [PlateItem(stl, 1.0, 1), PlateItem(stl, 1.0, 1), PlateItem(stl, 1.0, 1)]
    physicals = {0: stl, 1: stl, 2: stl}
    inputs = consolidate(items, physicals)
    assert inputs == [CliInput(path=stl, clone_count=3, ams_slot=1)]


def test_consolidate_same_source_diff_slot_splits(tmp_path: Path) -> None:
    stl = tmp_path / "a.stl"
    items = [PlateItem(stl, 1.0, 1), PlateItem(stl, 1.0, 2), PlateItem(stl, 1.0, 1)]
    physicals = {0: stl, 1: stl, 2: stl}
    inputs = consolidate(items, physicals)
    assert inputs == [
        CliInput(path=stl, clone_count=2, ams_slot=1),
        CliInput(path=stl, clone_count=1, ams_slot=2),
    ]


def test_consolidate_diff_physical_paths(tmp_path: Path) -> None:
    a, b = tmp_path / "a.stl", tmp_path / "b.stl"
    items = [PlateItem(a, 1.0, 1), PlateItem(b, 1.0, 1), PlateItem(a, 1.0, 1)]
    physicals = {0: a, 1: b, 2: a}
    inputs = consolidate(items, physicals)
    assert inputs == [
        CliInput(path=a, clone_count=2, ams_slot=1),
        CliInput(path=b, clone_count=1, ams_slot=1),
    ]


def test_consolidate_preserves_first_occurrence_order(tmp_path: Path) -> None:
    a, b = tmp_path / "a.stl", tmp_path / "b.stl"
    items = [PlateItem(b, 1.0, 1), PlateItem(a, 1.0, 1), PlateItem(b, 1.0, 1)]
    physicals = {0: b, 1: a, 2: b}
    inputs = consolidate(items, physicals)
    assert [i.path for i in inputs] == [b, a]


# ----------------------------------------------------------------------- build_cli_args


def test_build_cli_args_basic_shape(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    out = tmp_path / "out.3mf"
    inputs = [
        CliInput(path=tmp_path / "a.stl", clone_count=3, ams_slot=1),
        CliInput(path=tmp_path / "b.stl", clone_count=2, ams_slot=2),
    ]
    args = build_cli_args(
        inputs,
        machine_profile_path=Path("/m/Test A1.json"),
        process_profile_path=Path("/p/Test 0.2mm.json"),
        filament_paths_by_slot={1: Path("/f/Black.json"), 2: Path("/f/White.json")},
        output_path=out,
        config=cfg,
    )

    assert args[0] == str(cfg.paths.bambu_studio_exe)
    assert "--load-settings" in args
    assert args[args.index("--load-settings") + 1] == r"\m\Test A1.json;\p\Test 0.2mm.json".replace(
        "\\", "/"
    ) or args[args.index("--load-settings") + 1].endswith(
        "Test A1.json;" + str(Path("/p/Test 0.2mm.json"))
    )
    assert args[args.index("--clone-objects") + 1] == "3,2"
    assert "--load-filament-ids" not in args  # broken on Studio 02.05.00.66; patch.py handles
    assert args[args.index("--export-3mf") + 1] == "out.3mf"
    assert args[args.index("--outputdir") + 1] == str(tmp_path)
    # Positional inputs come at the very end, in order
    assert args[-2:] == [str(inputs[0].path), str(inputs[1].path)]


def test_build_cli_args_emits_filament_for_every_slot_up_to_max(tmp_path: Path) -> None:
    """When slots 1 and 3 are used, --load-filaments needs 3 entries (slot 2 filler)."""
    cfg = _build_config(tmp_path)
    inputs = [
        CliInput(path=tmp_path / "a.stl", clone_count=1, ams_slot=1),
        CliInput(path=tmp_path / "b.stl", clone_count=1, ams_slot=3),
    ]
    args = build_cli_args(
        inputs,
        machine_profile_path=Path("m.json"),
        process_profile_path=Path("p.json"),
        filament_paths_by_slot={1: Path("slot1.json"), 3: Path("slot3.json")},
        output_path=tmp_path / "out.3mf",
        config=cfg,
    )
    filaments = args[args.index("--load-filaments") + 1].split(";")
    assert len(filaments) == 3
    assert filaments[0] == "slot1.json"
    assert filaments[2] == "slot3.json"
    # Slot 2 (unused) gets slot 1's JSON as filler — content doesn't matter, never referenced
    assert filaments[1] == "slot1.json"


def test_build_cli_args_respects_behavior_flags(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    cfg = Config(
        paths=cfg.paths,
        behavior=Behavior(allow_rotations=True, ensure_on_bed=False, allow_mix_temp=True),
    )
    inputs = [CliInput(path=tmp_path / "a.stl", clone_count=1, ams_slot=1)]
    args = build_cli_args(
        inputs,
        machine_profile_path=Path("m.json"),
        process_profile_path=Path("p.json"),
        filament_paths_by_slot={1: Path("f.json")},
        output_path=tmp_path / "out.3mf",
        config=cfg,
    )
    assert "--allow-rotations" in args
    assert "--ensure-on-bed" not in args
    # --allow-mix-temp takes a value per bambu-studio.exe --help
    idx = args.index("--allow-mix-temp")
    assert args[idx + 1] == "1"


def test_build_cli_args_arrange_and_orient_take_values(tmp_path: Path) -> None:
    """Both flags take a 0/1/auto value, not a bare flag form."""
    cfg = _build_config(tmp_path)
    inputs = [CliInput(path=tmp_path / "a.stl", clone_count=1, ams_slot=1)]
    args = build_cli_args(
        inputs,
        machine_profile_path=Path("m.json"),
        process_profile_path=Path("p.json"),
        filament_paths_by_slot={1: Path("f.json")},
        output_path=tmp_path / "out.3mf",
        config=cfg,
    )
    # prepare_plate runs --arrange 0 / --orient 0 because layout is DIY'd
    # post-CLI (see bambu_prep.layout); the CLI's auto-arrange packs too
    # conservatively for our needs.
    assert args[args.index("--arrange") + 1] == "0"
    assert args[args.index("--orient") + 1] == "0"


def test_build_cli_args_rejects_empty_inputs(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    with pytest.raises(ValueError, match="CliInput"):
        build_cli_args(
            [],
            machine_profile_path=Path("m.json"),
            process_profile_path=Path("p.json"),
            filament_paths_by_slot={1: Path("f.json")},
            output_path=tmp_path / "out.3mf",
            config=cfg,
        )


# ----------------------------------------------------------------------- prepare_plate (integration)


def test_prepare_plate_happy_path(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    runner = FakeRunner(results=[(0, True)], output_path=out)
    result = prepare_plate(
        items=[PlateItem(stl, 1.0, 1)] * 3,
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
    )

    assert result.fit == 3
    assert result.requested == 3
    assert result.dropped == []
    assert result.output_path == out
    assert len(runner.calls) == 1
    # One CLI input, clone_count=3
    cmd = runner.calls[0]
    assert cmd[cmd.index("--clone-objects") + 1] == "3"
    assert "--load-filament-ids" not in cmd


def test_prepare_plate_anisotropic_scaling_pre_scales(tmp_path: Path) -> None:
    """Per-axis scale tuples are pre-scaled to temp STLs like uniform scales."""
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    runner = FakeRunner(results=[(0, True)], output_path=out)
    result = prepare_plate(
        items=[
            PlateItem(stl, (1.02, 1.02, 1.04), 1),
            PlateItem(stl, (1.023, 1.023, 1.045), 1),
            PlateItem(stl, (1.025, 1.025, 1.05), 1),
        ],
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
        keep_temp=True,
    )
    assert result.fit == 3
    cmd = runner.calls[0]
    assert cmd[cmd.index("--clone-objects") + 1] == "1,1,1"
    # Three distinct pre-scaled temp STLs, filename encodes all three axes
    tail = cmd[-3:]
    assert all(name.endswith(".stl") for name in tail)
    assert all("x1.0" in name for name in tail)


def test_prepare_plate_pre_scales_when_scales_differ(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    runner = FakeRunner(results=[(0, True)], output_path=out)
    result = prepare_plate(
        items=[PlateItem(stl, 1.01, 1), PlateItem(stl, 1.05, 1), PlateItem(stl, 1.10, 1)],
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
        keep_temp=True,  # keep so we can inspect the pre-scaled files
    )

    assert result.fit == 3
    cmd = runner.calls[0]
    # Three distinct input files, one clone each
    assert cmd[cmd.index("--clone-objects") + 1] == "1,1,1"
    assert "--load-filament-ids" not in cmd
    # Inputs at the tail are temp pre-scaled STLs, not the source
    inputs_at_tail = cmd[-3:]
    assert all(str(stl) != tail for tail in inputs_at_tail)
    assert all("_s1." in tail for tail in inputs_at_tail)


def test_prepare_plate_overflow_drops_last(tmp_path: Path) -> None:
    """First call fails, second succeeds → fit = len-1, dropped = [last]."""
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    items = [
        PlateItem(stl, 1.0, 1),
        PlateItem(stl, 1.0, 1),
        PlateItem(stl, 1.0, 1),
    ]
    runner = FakeRunner(
        results=[(1, False), (0, True)],
        output_path=out,
        stderr="out of plate area",
    )
    result = prepare_plate(
        items=items,
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
    )

    assert result.fit == 2
    assert result.requested == 3
    assert result.dropped == [items[-1]]
    assert len(runner.calls) == 2


def test_prepare_plate_full_failure(tmp_path: Path) -> None:
    """Every retry fails → fit=0, dropped is the full list (most-recently-dropped first)."""
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    items = [PlateItem(stl, 1.0, 1), PlateItem(stl, 1.0, 1)]
    runner = FakeRunner(results=[(1, False)] * 2, output_path=out)
    result = prepare_plate(
        items=items,
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
    )

    assert result.fit == 0
    assert result.output_path is None
    assert result.dropped == [items[-1], items[0]]
    assert len(runner.calls) == 2


def test_prepare_plate_rejects_missing_ams_entry(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    with pytest.raises(PrepareError, match="slot"):
        prepare_plate(
            items=[PlateItem(stl, 1.0, 2)],
            machine_profile="Test A1",
            process_profile="Test 0.2mm",
            output_path=out,
            ams_state={1: "Test Matte Black"},  # slot 2 missing
            config=cfg,
            runner=FakeRunner(results=[], output_path=out),
        )


def test_prepare_plate_rejects_empty_items(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    out = tmp_path / "out.3mf"
    with pytest.raises(PrepareError):
        prepare_plate(
            items=[],
            machine_profile="Test A1",
            process_profile="Test 0.2mm",
            output_path=out,
            ams_state={},
            config=cfg,
            runner=FakeRunner(results=[], output_path=out),
        )


def test_prepare_plate_rejects_missing_output_dir(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "does" / "not" / "exist" / "out.3mf"
    with pytest.raises(PrepareError, match="parent"):
        prepare_plate(
            items=[PlateItem(stl, 1.0, 1)],
            machine_profile="Test A1",
            process_profile="Test 0.2mm",
            output_path=out,
            ams_state={1: "Test Matte Black"},
            config=cfg,
            runner=FakeRunner(results=[], output_path=out),
        )


def test_prepare_plate_cleans_up_temp_by_default(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    runner = FakeRunner(results=[(0, True)], output_path=out)
    prepare_plate(
        items=[PlateItem(stl, 1.05, 1)],  # forces a temp file
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
    )

    # scratch root may or may not exist depending on first-use; if it exists,
    # there should be no leftover job subdirs.
    scratch = cfg.paths.temp_scratch_dir
    if scratch.is_dir():
        assert list(scratch.iterdir()) == []


def test_prepare_plate_patches_extruder_metadata_multi_slot(tmp_path: Path) -> None:
    """End-to-end: prepare_plate's post-CLI patch injects per-object extruder."""
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    items = [
        PlateItem(stl, 1.0, 1),
        PlateItem(stl, 1.0, 1),
        PlateItem(stl, 1.0, 2),
        PlateItem(stl, 1.0, 2),
        PlateItem(stl, 1.0, 2),
    ]
    runner = FakeRunner(results=[(0, True)], output_path=out)
    result = prepare_plate(
        items=items,
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black", 2: "Test White"},
        config=cfg,
        runner=runner,
    )
    assert result.fit == 5
    assert _extruder_counts_in_3mf(out) == Counter({"1": 2, "2": 3})


def test_prepare_plate_patches_single_slot_uses_slot_1(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    runner = FakeRunner(results=[(0, True)], output_path=out)
    prepare_plate(
        items=[PlateItem(stl, 1.0, 1)] * 3,
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
    )
    # Single-slot plates still get explicit slot=1 metadata written by the patch.
    assert _extruder_counts_in_3mf(out) == Counter({"1": 3})


def test_prepare_plate_layout_overflow_drops_without_cli(tmp_path: Path) -> None:
    """Layout pre-flight catches plate overflow; CLI is not invoked
    on iterations where the layout would fail."""
    cfg = _build_config(tmp_path)
    stl = tmp_path / "big.stl"
    # 200mm cube. Row of 3 = 602mm; grid of 2x2 = 401mm depth. No way to
    # fit more than 1 on a 256x256 plate.
    _write_cube(stl, side=200.0)
    out = tmp_path / "out.3mf"

    items = [PlateItem(stl, 1.0, 1)] * 3
    runner = FakeRunner(results=[(0, True)], output_path=out)
    result = prepare_plate(
        items=items,
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
    )
    # Only one fits (200x200 < 256x256). Two drops occurred before the
    # CLI was invoked even once.
    assert result.fit == 1
    assert len(result.dropped) == 2
    assert len(runner.calls) == 1


def test_prepare_plate_passes_transforms_to_finalize(tmp_path: Path) -> None:
    """The output .3mf should have the layout-computed transforms applied,
    not the (0,0,0) the CLI emits with --arrange 0."""
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl, side=10.0)
    out = tmp_path / "out.3mf"

    runner = FakeRunner(results=[(0, True)], output_path=out)
    prepare_plate(
        items=[PlateItem(stl, 1.0, 1)] * 3,
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
    )

    # CLI was invoked with --arrange 0
    assert runner.calls[0][runner.calls[0].index("--arrange") + 1] == "0"

    with zipfile.ZipFile(out) as zf:
        root = zf.read("3D/3dmodel.model").decode("utf-8")
    transforms = re.findall(r'transform="([^"]+)"', root)
    # All three items got non-origin positions (post-patch transforms)
    assert len(transforms) == 3
    for t in transforms:
        parts = t.split()
        cx, cy = float(parts[9]), float(parts[10])
        assert cx > 0 and cy > 0, f"transform left at origin: {t}"


def test_prepare_plate_keep_temp_preserves_job_dir(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.3mf"

    runner = FakeRunner(results=[(0, True)], output_path=out)
    prepare_plate(
        items=[PlateItem(stl, 1.05, 1)],
        machine_profile="Test A1",
        process_profile="Test 0.2mm",
        output_path=out,
        ams_state={1: "Test Matte Black"},
        config=cfg,
        runner=runner,
        keep_temp=True,
    )

    scratch = cfg.paths.temp_scratch_dir
    assert scratch.is_dir()
    job_dirs = list(scratch.iterdir())
    assert len(job_dirs) == 1
    # Pre-scaled file should be inside
    contents = list(job_dirs[0].iterdir())
    assert any(c.name.endswith(".stl") for c in contents)
