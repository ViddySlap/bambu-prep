"""Plate builder: turn a list of PlateItems into an unsliced ``.3mf``.

Orchestrates the four supporting modules:

- :mod:`bambu_prep.profiles` — resolves machine/process/filament name strings
  to JSON paths.
- :mod:`bambu_prep.meshes` — pre-scales STLs when a single source appears with
  multiple scales (the CLI's ``--scale`` is global per invocation).
- :mod:`bambu_prep.config` — paths and behavior flags.
- ``subprocess`` — invokes ``bambu-studio.exe``. The actual invocation is
  injected through a ``Runner`` callable so tests can substitute a fake.

CLI semantics (verified experimentally 2026-05-12; see
``ViddyVault/Projects/3d-printing/wiki/research/slicer-control-options.md``
section 10):

- ``--clone-objects "n1,n2,..."`` is **sized per input file**. One count per
  positional input on the command line.
- ``--load-filament-ids "s1,s2,..."`` is also sized per input file. IDs are
  AMS slot numbers (1..4), not indices.
- ``--load-filaments "f1.json;f2.json;..."`` is positional: position N is
  slot N. We always emit positions 1 through ``max(slots_used)`` to keep
  the slot-number-equals-position invariant intact, using slot 1's filament
  as filler for unused intermediate slots.

Overflow handling: ``bambu-studio.exe`` either fits everything or fails.
``prepare_plate`` iterates by dropping the last PlateItem on failure and
retrying, until success or the list is empty. The agent passing the items
should put lowest-priority copies last.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bambu_prep.config import Config
from bambu_prep.meshes import (
    ScaleFactor,
    is_identity_scale,
    make_job_dir,
    prescale,
    scale_suffix,
)
from bambu_prep.patch import patch_filament_slots
from bambu_prep.profiles import resolve as resolve_profile


@dataclass(frozen=True)
class PlateItem:
    """One printable copy on the plate.

    ``scale`` is either a float (uniform across all three axes) or a
    ``tuple[float, float, float]`` for anisotropic X/Y/Z scaling.
    """

    stl_path: Path
    scale: ScaleFactor = 1.0
    ams_slot: int = 1


@dataclass(frozen=True)
class CliInput:
    """One positional input on the ``bambu-studio.exe`` command line."""

    path: Path
    clone_count: int
    ams_slot: int


@dataclass(frozen=True)
class PrepareResult:
    fit: int
    requested: int
    dropped: list[PlateItem]
    output_path: Path | None
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


class PrepareError(RuntimeError):
    """Raised for unrecoverable failures before the CLI is ever invoked."""


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _output_uses_multiple_plates(output_path: Path) -> bool:
    """True if the CLI auto-paginated onto more than one plate.

    Bambu's CLI doesn't fail when objects overflow; it just creates a
    plate_2. Reading model_settings.config for a second ``<plate>`` block
    is the cheapest reliable detector.
    """
    try:
        with zipfile.ZipFile(output_path) as zf:
            cfg = zf.read("Metadata/model_settings.config").decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return False
    return len(re.findall(r"<plate>", cfg)) > 1


def _physical_inputs(items: list[PlateItem], job_dir: Path) -> dict[int, Path]:
    """Return ``{item_index: physical_stl_path}`` for each PlateItem.

    Identity-scale items (scale 1.0 or (1, 1, 1)) map to their source STL
    directly. Anything else is pre-scaled once per ``(source, scale)`` pair
    into ``job_dir``; subsequent items reuse the cached temp STL.
    """
    cache: dict[tuple[Path, ScaleFactor], Path] = {}
    out: dict[int, Path] = {}
    for i, item in enumerate(items):
        if is_identity_scale(item.scale):
            out[i] = item.stl_path
            continue
        key = (item.stl_path, item.scale)
        if key not in cache:
            output = job_dir / f"{item.stl_path.stem}_{scale_suffix(item.scale)}.stl"
            if not output.is_file():
                prescale(item.stl_path, item.scale, output)
            cache[key] = output
        out[i] = cache[key]
    return out


def consolidate(items: list[PlateItem], physical_inputs: dict[int, Path]) -> list[CliInput]:
    """Group items by ``(physical_input_path, ams_slot)`` preserving first-occurrence order."""
    groups: dict[tuple[str, int], list[int]] = {}
    order: list[tuple[str, int]] = []
    for i in range(len(items)):
        key = (str(physical_inputs[i]), items[i].ams_slot)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(i)

    result: list[CliInput] = []
    for key in order:
        indices = groups[key]
        first = indices[0]
        result.append(
            CliInput(
                path=physical_inputs[first],
                clone_count=len(indices),
                ams_slot=items[first].ams_slot,
            )
        )
    return result


def build_cli_args(
    inputs: list[CliInput],
    *,
    machine_profile_path: Path,
    process_profile_path: Path,
    filament_paths_by_slot: dict[int, Path],
    output_path: Path,
    config: Config,
) -> list[str]:
    """Build the ``bambu-studio.exe`` argv for one invocation."""
    if not inputs:
        raise ValueError("at least one CliInput required")
    if not filament_paths_by_slot:
        raise ValueError("at least one filament_paths_by_slot entry required")

    max_slot = max(i.ams_slot for i in inputs)
    filler = filament_paths_by_slot.get(1) or next(iter(filament_paths_by_slot.values()))
    filaments = [
        str(filament_paths_by_slot.get(slot, filler)) for slot in range(1, max_slot + 1)
    ]

    # NOTE: --load-filament-ids is silently ignored by Bambu Studio 02.05.00.66
    # in plate-prep CLI mode (verified 2026-05-11). The per-object slot
    # assignment is applied by bambu_prep.patch.patch_filament_slots after
    # the CLI returns.
    args: list[str] = [
        str(config.paths.bambu_studio_exe),
        "--load-settings",
        f"{machine_profile_path};{process_profile_path}",
        "--load-filaments",
        ";".join(filaments),
        "--clone-objects",
        ",".join(str(i.clone_count) for i in inputs),
        "--arrange",
        "1",
        "--orient",
        "1",
    ]
    if config.behavior.allow_rotations:
        args.append("--allow-rotations")
    if config.behavior.ensure_on_bed:
        args.append("--ensure-on-bed")
    if config.behavior.allow_mix_temp:
        args.extend(["--allow-mix-temp", "1"])
    args.extend(
        [
            "--outputdir",
            str(output_path.parent),
            "--export-3mf",
            output_path.name,
        ]
    )
    args.extend(str(i.path) for i in inputs)
    return args


def prepare_plate(
    items: list[PlateItem],
    *,
    machine_profile: str,
    process_profile: str,
    output_path: Path,
    ams_state: dict[int, str],
    config: Config | None = None,
    runner: Runner | None = None,
    keep_temp: bool = False,
) -> PrepareResult:
    """Build a single unsliced ``.3mf`` for the given PlateItems.

    Returns a :class:`PrepareResult` describing how many items fit and which
    were dropped. The dropped list is most-recently-dropped first.

    Parameters
    ----------
    items
        One PlateItem per intended printable copy. Order matters: on
        overflow, the last item is dropped first.
    machine_profile, process_profile
        Bambu preset *names* (e.g. ``"Bambu Lab A1 0.4 nozzle"``). Resolved
        through :mod:`bambu_prep.profiles`.
    output_path
        Where to write the resulting ``.3mf``. Parent directory must exist.
    ams_state
        ``{slot: filament_preset_name}`` for every slot referenced by ``items``.
        Caller is responsible for obtaining this (e.g. via :mod:`bambu_prep.ams`).
    config
        Defaults to :class:`Config` (auto-detected paths).
    runner
        Subprocess wrapper, for test injection. Defaults to a real
        ``subprocess.run`` of ``bambu-studio.exe``.
    keep_temp
        If True, the per-invocation scratch directory is left in place after
        return. Useful for debugging failed runs.
    """
    if not items:
        raise PrepareError("at least one PlateItem required")
    if not output_path.parent.is_dir():
        raise PrepareError(f"output_path parent does not exist: {output_path.parent}")

    config = config or Config()
    runner = runner or default_runner

    machine = resolve_profile(config, "machine", machine_profile)
    process = resolve_profile(config, "process", process_profile)

    slots_used = {item.ams_slot for item in items}
    missing_slots = slots_used - ams_state.keys()
    if missing_slots:
        raise PrepareError(
            f"ams_state missing entries for slot(s) {sorted(missing_slots)}; "
            f"provide them before calling prepare_plate"
        )

    filament_paths_by_slot: dict[int, Path] = {
        slot: resolve_profile(config, "filament", ams_state[slot]).path
        for slot in slots_used
    }

    job_dir = make_job_dir(config.paths.temp_scratch_dir)

    items_remaining = list(items)
    dropped: list[PlateItem] = []
    last_command: list[str] = []
    last_stdout = ""
    last_stderr = ""

    try:
        while items_remaining:
            physicals = _physical_inputs(items_remaining, job_dir)
            cli_inputs = consolidate(items_remaining, physicals)
            cmd = build_cli_args(
                cli_inputs,
                machine_profile_path=machine.path,
                process_profile_path=process.path,
                filament_paths_by_slot=filament_paths_by_slot,
                output_path=output_path,
                config=config,
            )
            last_command = cmd

            if output_path.is_file():
                output_path.unlink()  # prevent false success on stale file

            completed = runner(cmd)
            last_stdout = completed.stdout or ""
            last_stderr = completed.stderr or ""

            if completed.returncode == 0 and output_path.is_file():
                # Bambu Studio's CLI silently paginates onto additional plates
                # when one isn't enough. We want a single-plate result; treat
                # multi-plate output as overflow and shrink.
                if _output_uses_multiple_plates(output_path):
                    dropped.insert(0, items_remaining.pop())
                    continue
                slot_per_object = [
                    cli_in.ams_slot
                    for cli_in in cli_inputs
                    for _ in range(cli_in.clone_count)
                ]
                patch_filament_slots(output_path, slot_per_object)
                return PrepareResult(
                    fit=len(items_remaining),
                    requested=len(items),
                    dropped=dropped,
                    output_path=output_path,
                    command=cmd,
                    stdout=last_stdout,
                    stderr=last_stderr,
                )

            dropped.insert(0, items_remaining.pop())

        return PrepareResult(
            fit=0,
            requested=len(items),
            dropped=dropped,
            output_path=None,
            command=last_command,
            stdout=last_stdout,
            stderr=last_stderr,
        )
    finally:
        if not keep_temp:
            shutil.rmtree(job_dir, ignore_errors=True)
