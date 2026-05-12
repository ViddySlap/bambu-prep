"""Headless slicing via OrcaSlicer CLI.

Why OrcaSlicer and not Bambu Studio: Bambu Studio 02.05.00.66's CLI
``--slice 0`` mode SIGSEGVs at exactly ``percent=71, message=Detect
overhangs for auto-lift`` on the A1 vendor profile (upstream bug
[bambulab/BambuStudio#9636](https://github.com/bambulab/BambuStudio/issues/9636)).
The crash bites whether the project is freshly A1-flavored or carried
over from another printer. Confirmed 2026-05-12 against the retargeted
clicker fidget.

OrcaSlicer is a Bambu Studio fork (SoftFever/OrcaSlicer) with a separate
slicing-engine implementation. Verified 2026-05-12 against the same
retargeted clicker fidget: OrcaSlicer's CLI slices to completion and
produces a sliced .3mf with the full A1 start-gcode template embedded
(38 M1006 calibration moves, G29 bed leveling, A1 header — equivalent
to a Bambu Studio GUI slice).

Post-slice patch: OrcaSlicer's output leaves ``printer_model_id`` empty
in ``Metadata/slice_info.config``. We patch it to ``N2S`` so the
``send`` preflight validation has a definitive match on the A1 model_id
(rather than the "absent = skip check" branch).

Default OrcaSlicer install path is ``~/Documents/tools/OrcaSlicer/
orca-slicer.exe`` (the portable Windows zip extracted there). Override
via ``[paths] orcaslicer_exe`` in ``bambu_prep_config.toml`` for other
locations (or eventually a Linux build for OpenClaw).
"""

from __future__ import annotations

import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from bambu_prep.config import Config
from bambu_prep.retarget import _patch_slice_info


SLICE_INFO_MEMBER = "Metadata/slice_info.config"


@dataclass(frozen=True)
class SliceResult:
    """Result of a successful :func:`slice_3mf` call."""

    input_path: Path
    output_path: Path
    plate: int
    """``0`` = sliced all plates; otherwise the specific plate number sliced."""
    duration_seconds: float
    """Wall-clock seconds the CLI invocation took. Useful telemetry; doesn't
    affect correctness."""
    model_id_patched: bool
    """``True`` when we post-patched the output's slice_info.config to set
    ``printer_model_id``. ``False`` when the field was already correct
    or absent (e.g. multi-plate edge cases)."""


class SliceError(RuntimeError):
    """Raised when slicing can't proceed or fails."""


def slice_3mf(
    file_path: Path,
    *,
    config: Config,
    output_path: Path | None = None,
    plate: int = 0,
    target_printer_model_id: str = "N2S",
    timeout_seconds: float = 600.0,
) -> SliceResult:
    """Slice a retargeted ``.3mf`` using OrcaSlicer's CLI.

    The input must already be A1-targeted (printer_model = "Bambu Lab A1",
    A1 process+filament identifiers, etc.). Run :func:`bambu_prep.retarget`
    first if the file came from MakerWorld or any other source. Slicing
    a non-A1-targeted file would produce gcode for the wrong machine.

    Returns a :class:`SliceResult`. Raises :class:`SliceError` on
    OrcaSlicer-not-found, invalid input, or non-zero exit.
    """
    if not file_path.is_file():
        raise SliceError(f"slice: file not found: {file_path}")

    orca_exe = config.paths.orcaslicer_exe
    if not orca_exe.is_file():
        raise SliceError(
            f"slice: OrcaSlicer CLI not found at {orca_exe}. "
            "Download the portable Windows release from "
            "https://github.com/SoftFever/OrcaSlicer/releases and extract "
            "to ~/Documents/tools/OrcaSlicer/, or set [paths] "
            "orcaslicer_exe in bambu_prep_config.toml to the install path."
        )

    out = output_path if output_path is not None else _default_output_path(file_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    args: list[str] = [
        str(orca_exe),
        "--slice",
        str(plate),
        "--outputdir",
        str(out.parent),
        "--export-3mf",
        out.name,
        str(file_path),
    ]

    start = time.monotonic()
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise SliceError(
            f"slice: OrcaSlicer timed out after {timeout_seconds}s. "
            "Either the model is unusually large or the slicer is stuck; "
            "raise timeout_seconds or check OrcaSlicer's foreground "
            "behavior on this input."
        ) from e
    except OSError as e:
        raise SliceError(f"slice: failed to invoke OrcaSlicer: {e}") from e
    duration = time.monotonic() - start

    if result.returncode != 0:
        raise SliceError(
            f"slice: OrcaSlicer exited with code {result.returncode}. "
            f"stderr: {result.stderr[-500:] if result.stderr else '<empty>'}"
        )

    if not out.is_file():
        raise SliceError(
            f"slice: OrcaSlicer reported success but {out} was not produced. "
            f"stderr: {result.stderr[-500:] if result.stderr else '<empty>'}"
        )

    # Verify the sliced output actually contains gcode. If OrcaSlicer
    # silently produced an unsliced project (rare but possible if the
    # input is malformed), catch it here rather than letting the user
    # discover it when send refuses the file.
    plate_for_check = max(plate, 1)
    gcode_member = f"Metadata/plate_{plate_for_check}.gcode"
    with zipfile.ZipFile(out, "r") as zf:
        if gcode_member not in zf.namelist():
            raise SliceError(
                f"slice: OrcaSlicer produced {out.name} but it has no "
                f"{gcode_member} inside (not actually sliced). "
                "Open the file in OrcaSlicer's GUI to investigate."
            )

    # Post-patch slice_info.config to set printer_model_id.
    model_id_patched = _patch_output_model_id(out, target_printer_model_id)

    return SliceResult(
        input_path=file_path,
        output_path=out,
        plate=plate,
        duration_seconds=duration,
        model_id_patched=model_id_patched,
    )


def _default_output_path(file_path: Path) -> Path:
    """Default output: same dir, ``.gcode.3mf`` suffix (matches Studio
    GUI's 'Export Plate Sliced 3mf' naming convention)."""
    stem = file_path.stem
    if stem.endswith(".gcode"):
        # Already has .gcode in the stem (re-slicing a sliced file).
        return file_path.with_name(f"{stem}.3mf")
    return file_path.with_name(f"{stem}.gcode.3mf")


def _patch_output_model_id(file_path: Path, target_model_id: str) -> bool:
    """Rewrite Metadata/slice_info.config's printer_model_id in place.

    OrcaSlicer leaves the field as ``value=""`` in its output (it doesn't
    know which printer it was targeting beyond what the project says).
    Our preflight validator treats empty as "skip the check", but a
    definite match is more useful, so we always set it explicitly.

    Returns ``True`` if the field was changed; ``False`` if it was
    already correct or the file has no slice_info.config (rare).
    """
    with zipfile.ZipFile(file_path, "r") as zin:
        names = zin.namelist()
        if SLICE_INFO_MEMBER not in names:
            return False
        members: dict[str, bytes] = {name: zin.read(name) for name in names}

    si_blob = members[SLICE_INFO_MEMBER].decode("utf-8", errors="replace")
    new_si, changed = _patch_slice_info(si_blob, target_model_id)
    if not changed:
        # Either already correct or the printer_model_id key wasn't present.
        # In OrcaSlicer's case, the key IS present with an empty value; the
        # regex matches and rewrites. If we got here changed=False, the value
        # was already what we wanted — that's a no-op success.
        if _slice_info_has_model_id(si_blob, target_model_id):
            return False
        # Field absent entirely; inject it.
        new_si = _inject_model_id(si_blob, target_model_id)
        if new_si == si_blob:
            return False
        members[SLICE_INFO_MEMBER] = new_si.encode("utf-8")
    else:
        members[SLICE_INFO_MEMBER] = new_si.encode("utf-8")

    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in members.items():
            zout.writestr(name, data)
    tmp.replace(file_path)
    return True


def _slice_info_has_model_id(xml: str, value: str) -> bool:
    import re

    return bool(
        re.search(
            rf'key\s*=\s*"printer_model_id"\s+value\s*=\s*"{re.escape(value)}"',
            xml,
            re.IGNORECASE,
        )
    )


def _inject_model_id(xml: str, value: str) -> str:
    """If slice_info.config has no printer_model_id metadata tag at all,
    inject one inside the first ``<plate>`` element. Best-effort; if the
    structure doesn't match what we expect, return the input unchanged."""
    import re

    match = re.search(r"(<plate[^>]*>)", xml)
    if not match:
        return xml
    insertion = f'\n    <metadata key="printer_model_id" value="{value}"/>'
    pos = match.end()
    return xml[:pos] + insertion + xml[pos:]
