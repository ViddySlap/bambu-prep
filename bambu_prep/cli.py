"""bambu-prep CLI.

Two ways to describe a plate:

1. **Single-source shorthand** for the common case (one STL, N copies at
   varying scales/slots)::

       python -m bambu_prep prepare \\
         --stl path/to/case.stl \\
         --scales "1.02x1.02x1.04,1.023x1.023x1.045" \\
         --slot 1 \\
         --output out.3mf

2. **JSON manifest** for heterogeneous batches (5 of A in slot 1, 3 of B
   in slot 2, etc.)::

       python -m bambu_prep prepare --manifest plate.json

Diagnostics:

- ``python -m bambu_prep ams-status`` - read the printer's current AMS loadout
- ``python -m bambu_prep list-profiles machine|process|filament`` - enumerate
  installed Bambu Studio presets

Defaults read from ``bambu_prep_config.toml``; CLI flags override the config.
Live AMS query is mandatory; if the printer is unreachable, the command
aborts with a clear message instead of falling back to a manual prompt
(this keeps the command identical when invoked by an agent like OpenClaw).
"""

from __future__ import annotations

import json
import subprocess  # noqa: F401 (used in _launch_studio when --open is given)
import sys
from datetime import date
from pathlib import Path

import click

from bambu_prep.ams import (
    AMSError,
    _default_live_query,
    filament_suffix_for,
    get_ams_state,
    match_preset,
)
from bambu_prep.config import Config, load_config
from bambu_prep.dispatch import (
    DispatchError,
    PreflightReport,
    preflight as dispatch_preflight,
    start as dispatch_start,
    upload as dispatch_upload,
    validate_file as dispatch_validate_file,
)
from bambu_prep.makerworld import MakerWorldError, fetch as makerworld_fetch
from bambu_prep.retarget import (
    RetargetError,
    RetargetResult,
    retarget as retarget_3mf,
)
from bambu_prep.slice import (
    SliceError,
    SliceResult,
    slice_3mf,
)
from bambu_prep.meshes import ScaleFactor
from bambu_prep.plate import PlateItem, PrepareError, prepare_plate
from bambu_prep.profiles import ProfileError, list_profiles


WINDOWS_DROPBOX_DEFAULT = Path(
    "D:/Baros Design Co. Dropbox/Ben Baros/AI AGENTS/3D PRINTING"
)
"""Hardcoded fallback when config doesn't specify ``defaults.output_dir``.
Override per-invocation with ``--output`` or per-environment via the config
file's ``[defaults]`` section (which OpenClaw on the server should populate
with its own path)."""


@click.group()
def main() -> None:
    """bambu-prep: build unsliced Bambu Studio .3mf plates."""


@main.command()
def version() -> None:
    """Print the installed version."""
    from bambu_prep import __version__

    click.echo(__version__)


@main.command()
@click.option(
    "--stl",
    "stl_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Single-source: input STL or .3mf path.",
)
@click.option(
    "--scales",
    "scales_str",
    type=str,
    default="",
    help='Single-source: comma-separated scale list. Uniform "1.02" or '
    'anisotropic "1.02x1.02x1.04". Empty = identity scale.',
)
@click.option(
    "--slot",
    type=int,
    default=1,
    show_default=True,
    help="Single-source: AMS slot for every copy.",
)
@click.option(
    "--count",
    type=int,
    default=1,
    show_default=True,
    help="Single-source: number of copies per scale.",
)
@click.option(
    "--manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Multi-source: JSON manifest. Mutually exclusive with --stl/--scales.",
)
@click.option(
    "--machine",
    default=None,
    help="Machine preset name. Default from config; falls back to "
    '"Bambu Lab A1 0.4 nozzle".',
)
@click.option(
    "--process",
    "process_profile",
    default=None,
    help="Process preset name. Default from config; falls back to "
    '"0.20mm Standard @BBL A1".',
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Output .3mf path. Default: <output_dir>/<auto-name>.3mf",
)
@click.option(
    "--subfolder",
    default="",
    help="Optional subfolder under the default output dir (e.g. project slug).",
)
@click.option(
    "--open",
    "auto_open",
    is_flag=True,
    default=False,
    help="Launch Bambu Studio with the generated file. Desktop-only; "
    "the skill leaves this off by default so the command works on a "
    "headless agent (OpenClaw) too.",
)
def prepare(
    stl_path: Path | None,
    scales_str: str,
    slot: int,
    count: int,
    manifest: Path | None,
    machine: str | None,
    process_profile: str | None,
    output_path: Path | None,
    subfolder: str,
    auto_open: bool,
) -> None:
    """Build an unsliced .3mf plate for Bambu Studio."""
    config = load_config()

    if manifest and (stl_path or scales_str):
        raise click.UsageError(
            "--manifest is mutually exclusive with --stl / --scales"
        )

    if manifest:
        items, manifest_machine, manifest_process, manifest_output = _items_from_manifest(
            manifest
        )
        machine = machine or manifest_machine
        process_profile = process_profile or manifest_process
        output_path = output_path or manifest_output
    elif stl_path:
        items = _items_from_flags(stl_path, scales_str, slot, count)
    else:
        raise click.UsageError("provide --stl + --scales, or --manifest")

    if not items:
        raise click.UsageError("no items to print (resolved item list is empty)")

    machine = machine or config.defaults.machine_profile
    process_profile = process_profile or config.defaults.process_profile

    if output_path is None:
        output_path = _default_output_path(items, config, subfolder)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    slots_needed = {item.ams_slot for item in items}
    try:
        ams_state = _live_ams_or_fail(config, machine, slots_needed)
    except AMSError as e:
        click.echo(f"ams: {e}", err=True)
        sys.exit(2)

    try:
        result = prepare_plate(
            items=items,
            machine_profile=machine,
            process_profile=process_profile,
            output_path=output_path,
            ams_state=ams_state,
            config=config,
        )
    except (PrepareError, ProfileError) as e:
        click.echo(f"prepare: {e}", err=True)
        sys.exit(2)

    click.echo(
        f"fit={result.fit} requested={result.requested} dropped={len(result.dropped)}"
    )
    if result.dropped:
        click.echo("dropped (lowest priority first):")
        for it in result.dropped:
            click.echo(f"  - {it.stl_path.name} scale={it.scale} slot={it.ams_slot}")
    if result.output_path is None:
        click.echo("no .3mf produced (every item dropped)", err=True)
        sys.exit(3)

    click.echo(f"output: {result.output_path}")

    if auto_open:
        _launch_studio(config.paths.bambu_studio_exe, result.output_path)


@main.command(name="ams-status")
@click.option("--machine", default=None, help="Machine preset name for filament matching.")
def ams_status(machine: str | None) -> None:
    """Print the printer's current AMS slot loadout.

    Reports every populated slot with its matched filament preset (or a
    descriptive label when the printer's RFID hint doesn't map to a single
    preset). Empty slots are listed as empty. Exits non-zero only when the
    printer can't be reached at all.
    """
    config = load_config()
    machine = machine or config.defaults.machine_profile

    if not config.printer.ip:
        click.echo(
            "ams: no printer ip in config; populate [printer] or [secret_refs] "
            "in bambu_prep_config.toml",
            err=True,
        )
        sys.exit(2)

    trays = _default_live_query(config)
    if trays is None:
        click.echo(
            f"ams: printer unreachable at {config.printer.ip}. Power on the A1, "
            "verify it's on the network, and that LAN Only Mode + Developer Mode "
            "are still enabled.",
            err=True,
        )
        sys.exit(2)

    suffix = filament_suffix_for(machine)
    available = [p.name for p in list_profiles(config, "filament")]

    if not trays:
        click.echo("AMS connected; no slots populated")
        return

    for slot in range(1, 5):
        tray = trays.get(slot)
        if tray is None:
            click.echo(f"  slot {slot}: <empty>")
            continue
        matched = match_preset(tray, suffix, available)
        if matched:
            click.echo(f"  slot {slot}: {matched}  (color #{tray.color})")
        else:
            label = tray.sub_brand or tray.name_hint or "<unknown>"
            click.echo(
                f"  slot {slot}: {label}  (color #{tray.color}; no preset match for {suffix!r})"
            )


@main.command()
@click.argument("url", type=str)
@click.option(
    "--subfolder",
    default="",
    help="Optional subfolder under the default output dir for the downloaded file.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Explicit output .3mf path. Default: <output_dir>/<subfolder>/<sanitized-name>.3mf",
)
@click.option(
    "--no-retarget",
    is_flag=True,
    default=False,
    help="Skip the post-download retarget step. By default, every fetched .3mf "
    "is unconditionally retargeted to the A1 (Bambu Lab A1 0.4 nozzle + "
    "Bambu PLA Basic @BBL A1 + N2S model_id) so the file opens A1-ready in "
    "Studio. Use this flag only when intentionally keeping the maker's "
    "profile (rare; e.g. you're forwarding the file to a different printer).",
)
def fetch(url: str, subfolder: str, output_path: Path | None, no_retarget: bool) -> None:
    """Download a curated .3mf from a MakerWorld URL.

    Auth is automatic: a Bambu Cloud token is resolved via the local cache,
    then Bambu Studio's installed cache, then programmatic login using
    ``BAMBU_CLOUD_EMAIL`` / ``BAMBU_CLOUD_PASSWORD`` env vars.

    Example:

        python -m bambu_prep fetch \\
          "https://makerworld.com/en/models/707208-clicker-fidget?from=x#profileId-637253"
    """
    config = load_config()
    try:
        if output_path is None:
            base = config.defaults.output_dir or WINDOWS_DROPBOX_DEFAULT
            if subfolder:
                base = base / subfolder
            # Resolve the maker's filename via a no-write probe: we don't have it
            # yet, so use a placeholder; makerworld_fetch returns the real name and
            # we rename after download.
            base.mkdir(parents=True, exist_ok=True)
            today = date.today().isoformat()
            output_path = base / f"makerworld_{today}.3mf"
        result = makerworld_fetch(url, output_path=output_path)
    except MakerWorldError as e:
        click.echo(f"fetch: {e}", err=True)
        sys.exit(2)

    # Rename to a clean, name-based filename now that we know what the maker called it.
    final_path = result.path
    if output_path.name.startswith("makerworld_"):
        safe = _sanitize_filename(result.name)
        renamed = output_path.with_name(f"{safe}_{date.today().isoformat()}.3mf")
        if renamed != output_path:
            # Use replace() not rename() so an existing target (e.g. from a prior
            # partial run) is overwritten rather than crashing on Windows.
            output_path.replace(renamed)
            final_path = renamed

    click.echo(f"name: {result.name}")
    click.echo(f"design_id: {result.design_id}")
    click.echo(f"instance_id: {result.instance_id}")
    click.echo(f"profile_id: {result.profile_id}  (resolved slicer profile)")
    click.echo(f"output: {final_path}")

    if not no_retarget:
        try:
            rt = retarget_3mf(final_path, config=config)
        except RetargetError as e:
            click.echo(f"fetch: retarget failed: {e}", err=True)
            click.echo(
                "fetch: file downloaded successfully but was not retargeted to the A1. "
                "Re-run `python -m bambu_prep retarget <path>` to fix, or open in "
                "Studio and switch the printer manually.",
                err=True,
            )
            return
        if rt.was_already_target:
            click.echo(
                "retarget: file was already targeting the A1; no changes needed."
            )
        else:
            click.echo(
                f"retarget: rewrote {rt.fields_changed} field(s) to "
                f"target '{rt.target_machine}' (model_id={rt.target_machine_model_id}) "
                f"with filament '{rt.target_filament}'. Open in Studio and the "
                "printer selector will read 'Bambu Lab A1' already."
            )


@main.command()
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--plate",
    type=int,
    default=1,
    show_default=True,
    help="Plate number (1-indexed) within the .3mf to print.",
)
@click.option(
    "--ams-mapping",
    "ams_mapping_str",
    default="",
    help="Comma-separated 0-indexed AMS slots for each filament in the .3mf. "
    'Default: natural mapping ([0,1,...]). Example: "2,0" maps filament 1 to slot 3, filament 2 to slot 1.',
)
@click.option(
    "--start",
    "do_start",
    is_flag=True,
    default=False,
    help="Actually start the print after upload + preflight. Default: stop after "
    "preflight (the skill calls this command twice: once without --start to show "
    "the preflight, then again with --start after the user confirms).",
)
@click.option(
    "--no-ams",
    "no_ams",
    is_flag=True,
    default=False,
    help="Disable AMS for this print (single-spool feed instead).",
)
@click.option(
    "--skip-preflight",
    is_flag=True,
    default=False,
    help="Skip preflight (AMS compatibility check). Use ONLY when you know the AMS "
    "state is correct independently. Workaround for bambulabs-api MQTT state issues "
    "when upload + preflight happen back-to-back in the same process.",
)
@click.option(
    "--allow-printer-mismatch",
    is_flag=True,
    default=False,
    help="Permit sending a .3mf whose embedded printer_model_id doesn't match the A1. "
    "Only use this when intentionally dispatching hand-crafted or experimental gcode "
    "(e.g. Blender-manipulated non-linear layers). Default is hard-fail: the file-"
    "validation check prevents the X1C-sliced-but-sent-to-A1 failure mode.",
)
def send(
    file_path: Path,
    plate: int,
    ams_mapping_str: str,
    do_start: bool,
    no_ams: bool,
    skip_preflight: bool,
    allow_printer_mismatch: bool,
) -> None:
    """Upload a sliced .3mf to the A1, run preflight, and optionally start the print.

    Default behavior (no ``--start``):

    1. Upload the .3mf to the printer over FTPS.
    2. Run preflight: compare what the print needs against the live AMS state.
    3. Print the preflight summary and exit. The skill (or a human) reviews,
       then re-invokes with ``--start`` to actually kick off the print.

    With ``--start``: same as above plus issue the start-print command.
    """
    config = load_config()

    ams_mapping = _parse_ams_mapping(ams_mapping_str) if ams_mapping_str else None

    try:
        validation = dispatch_validate_file(
            file_path,
            plate=plate,
            allow_printer_mismatch=allow_printer_mismatch,
        )
    except DispatchError as e:
        click.echo(f"send: {e}", err=True)
        sys.exit(2)
    for w in validation.warnings:
        click.echo(f"validate: warning: {w}")
    if not validation.ok:
        click.echo("validate: REFUSING to upload", err=True)
        for err in validation.errors:
            click.echo(f"  - {err}", err=True)
        sys.exit(3)
    if validation.detected_model_id:
        click.echo(
            f"validate: ok (sliced for '{validation.detected_model_id}', "
            f"plate {plate} has gcode)"
        )
    else:
        click.echo(
            f"validate: ok (plate {plate} has gcode; printer_model_id absent, "
            "skipped model check)"
        )

    try:
        remote_name = dispatch_upload(file_path, config=config)
    except DispatchError as e:
        click.echo(f"send: {e}", err=True)
        sys.exit(2)
    click.echo(f"uploaded: {remote_name}")

    if skip_preflight:
        click.echo("preflight: SKIPPED (--skip-preflight passed)")
        if not do_start:
            click.echo("upload complete; re-run with --start to begin the print")
            return
        # Default mapping when preflight is skipped: 0-indexed natural mapping.
        # If --ams-mapping was passed, honor it; else [0] for single-filament.
        final_mapping = ams_mapping if ams_mapping is not None else [0]
    else:
        # The A1's MQTT broker needs a moment to release the client slot after
        # upload's mqtt_stop() before preflight's fresh MQTT query can succeed.
        # 15s empirically sufficient; 3s wasn't.
        import time as _time
        _time.sleep(15.0)

        try:
            report = dispatch_preflight(file_path, config=config, ams_mapping=ams_mapping)
        except DispatchError as e:
            click.echo(f"send: {e}", err=True)
            sys.exit(2)
        click.echo(report.human_summary())

        if not do_start:
            if not report.ok:
                click.echo("preflight: NOT ok; resolve issues before passing --start", err=True)
                sys.exit(3)
            click.echo("preflight: ok; re-run with --start to begin the print")
            return

        if not report.ok:
            click.echo("send: preflight failed; refusing to start", err=True)
            sys.exit(3)
        final_mapping = report.ams_mapping

    try:
        dispatch_start(
            remote_name,
            config=config,
            plate=plate,
            ams_mapping=final_mapping,
            use_ams=not no_ams,
        )
    except DispatchError as e:
        click.echo(f"send: {e}", err=True)
        sys.exit(2)
    click.echo(f"started: {remote_name} plate {plate}")


@main.command()
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output path. Default: rewrite in place.",
)
@click.option(
    "--machine",
    "target_machine",
    default="Bambu Lab A1 0.4 nozzle",
    show_default=True,
    help="Target machine profile.",
)
@click.option(
    "--process",
    "target_process",
    default="0.20mm Standard @BBL A1",
    show_default=True,
    help="Target process profile (only the identifier is flipped; the maker's "
    "actual layer-height/infill/supports tuning is preserved).",
)
@click.option(
    "--filament",
    "target_filament",
    default="Bambu PLA Basic @BBL A1",
    show_default=True,
    help="Target filament profile (applied to every AMS slot).",
)
@click.option(
    "--model-id",
    "target_model_id",
    default="N2S",
    show_default=True,
    help="Target printer_model_id written into slice_info.config (A1 = N2S).",
)
def retarget(
    file_path: Path,
    output_path: Path | None,
    target_machine: str,
    target_process: str,
    target_filament: str,
    target_model_id: str,
) -> None:
    """Rewrite a .3mf to unconditionally target the A1 (or another machine).

    Patches Metadata/project_settings.config so the printer_model, every
    machine-bound setting (start_gcode, bed_exclude_area, max speeds, etc.),
    and every filament_settings_id slot all reference the target A1 profile.
    Process-bound settings (layer_height, infill, supports, etc.) are
    preserved from the input.

    Use this when receiving a .3mf from outside the fetch path (e.g. a file
    a friend sent you, or one you re-downloaded manually). Files coming
    through `python -m bambu_prep fetch` are retargeted automatically.
    """
    config = load_config()
    try:
        result = retarget_3mf(
            file_path,
            config=config,
            output_path=output_path,
            target_machine_profile=target_machine,
            target_process_profile=target_process,
            target_filament_profile=target_filament,
            target_machine_model_id=target_model_id,
        )
    except RetargetError as e:
        click.echo(f"retarget: {e}", err=True)
        sys.exit(2)

    if result.was_already_target:
        click.echo(f"retarget: {file_path.name} already targets {target_machine}; no changes.")
        return
    click.echo(
        f"retarget: rewrote {result.fields_changed} field(s) in {result.output_path.name} "
        f"to target '{result.target_machine}' (model_id={result.target_machine_model_id})."
    )


@main.command(name="slice")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output sliced .3mf path. Default: <input>.gcode.3mf alongside the input.",
)
@click.option(
    "--plate",
    type=int,
    default=0,
    show_default=True,
    help="Plate to slice. 0 = all plates, N = plate N only (1-indexed).",
)
@click.option(
    "--model-id",
    "target_model_id",
    default="N2S",
    show_default=True,
    help="Printer model_id to write into slice_info.config so send preflight "
    "has a definitive match (OrcaSlicer leaves this empty by default).",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=float,
    default=600.0,
    show_default=True,
    help="OrcaSlicer wall-clock timeout in seconds.",
)
def slice_cmd(
    file_path: Path,
    output_path: Path | None,
    plate: int,
    target_model_id: str,
    timeout_seconds: float,
) -> None:
    """Slice a (retargeted) .3mf using OrcaSlicer's CLI.

    OrcaSlicer succeeds on the A1 vendor profile where Bambu Studio
    02.05.00.66's `--slice 0` SIGSEGVs at 71% (upstream bug
    bambulab/BambuStudio#9636). Run `retarget` on the input first so
    the file is A1-flavored; slicing a non-A1-targeted file would
    emit gcode for the wrong machine.

    Output is a sliced .3mf with `Metadata/plate_<N>.gcode` embedded.
    Pass that file straight to `send`.
    """
    config = load_config()
    try:
        result = slice_3mf(
            file_path,
            config=config,
            output_path=output_path,
            plate=plate,
            target_printer_model_id=target_model_id,
            timeout_seconds=timeout_seconds,
        )
    except SliceError as e:
        click.echo(f"slice: {e}", err=True)
        sys.exit(2)

    click.echo(
        f"slice: ok ({result.duration_seconds:.1f}s) -> {result.output_path}"
    )
    if result.model_id_patched:
        click.echo(f"slice: patched printer_model_id to '{target_model_id}'")


@main.command(name="list-profiles")
@click.argument("kind", type=click.Choice(["machine", "process", "filament"]))
@click.option("--filter", "filter_str", default="", help="Substring filter (case-insensitive).")
def list_profiles_cmd(kind: str, filter_str: str) -> None:
    """List installed Bambu Studio profiles of the given kind."""
    config = load_config()
    profiles = list_profiles(config, kind)
    if filter_str:
        needle = filter_str.lower()
        profiles = [p for p in profiles if needle in p.name.lower()]
    for p in profiles:
        click.echo(f"  [{p.source}] {p.name}")


def _items_from_flags(
    stl_path: Path, scales_str: str, slot: int, count: int
) -> list[PlateItem]:
    if not scales_str.strip():
        return [PlateItem(stl_path=stl_path, scale=1.0, ams_slot=slot) for _ in range(count)]
    scales = [_parse_scale(s) for s in scales_str.split(",")]
    items: list[PlateItem] = []
    for scale in scales:
        for _ in range(count):
            items.append(PlateItem(stl_path=stl_path, scale=scale, ams_slot=slot))
    return items


def _parse_scale(s: str) -> ScaleFactor:
    s = s.strip()
    if not s:
        raise click.BadParameter("empty scale value")
    if "x" in s:
        parts = s.split("x")
        if len(parts) != 3:
            raise click.BadParameter(
                f"anisotropic scale must be 3 axis-values, got {s!r}"
            )
        try:
            return tuple(float(p) for p in parts)  # type: ignore[return-value]
        except ValueError as e:
            raise click.BadParameter(f"bad anisotropic scale {s!r}: {e}") from e
    try:
        return float(s)
    except ValueError as e:
        raise click.BadParameter(f"bad scale {s!r}: {e}") from e


def _items_from_manifest(
    manifest_path: Path,
) -> tuple[list[PlateItem], str | None, str | None, Path | None]:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if "items" not in raw or not isinstance(raw["items"], list):
        raise click.UsageError(f"{manifest_path}: manifest must have an 'items' array")
    items: list[PlateItem] = []
    for i, entry in enumerate(raw["items"]):
        if not isinstance(entry, dict) or "stl" not in entry:
            raise click.UsageError(
                f"{manifest_path} item #{i}: each item needs an 'stl' path"
            )
        stl = Path(entry["stl"])
        scale_raw = entry.get("scale", 1.0)
        if isinstance(scale_raw, list):
            if len(scale_raw) != 3:
                raise click.UsageError(
                    f"{manifest_path} item #{i}: anisotropic scale must be 3 values"
                )
            scale: ScaleFactor = tuple(float(x) for x in scale_raw)  # type: ignore[assignment]
        else:
            scale = float(scale_raw)
        slot = int(entry.get("slot", 1))
        n = int(entry.get("count", 1))
        if n < 1:
            raise click.UsageError(f"{manifest_path} item #{i}: count must be >= 1")
        for _ in range(n):
            items.append(PlateItem(stl_path=stl, scale=scale, ams_slot=slot))

    machine = raw.get("machine_profile")
    process = raw.get("process_profile")
    output = Path(raw["output_path"]) if "output_path" in raw else None
    return items, machine, process, output


def _default_output_path(
    items: list[PlateItem], config: Config, subfolder: str
) -> Path:
    base = config.defaults.output_dir or WINDOWS_DROPBOX_DEFAULT
    if subfolder:
        base = base / subfolder
    today = date.today().isoformat()
    distinct_sources = {it.stl_path.stem for it in items}
    if len(distinct_sources) == 1:
        stem = next(iter(distinct_sources))
    else:
        stem = "plate"
    return base / f"{stem}_{len(items)}copies_{today}.3mf"


def _live_ams_or_fail(
    config: Config,
    machine: str,
    slots_needed: set[int],
    *,
    allow_missing_slots: bool = False,
) -> dict[int, str]:
    """Wrap get_ams_state so unreachable printer aborts with a clear message
    instead of falling through to interactive prompt."""
    if not config.printer.ip:
        raise AMSError(
            "no printer ip in config; populate [printer] in bambu_prep_config.toml "
            "or set BAMBU_A1_IP via [secret_refs]"
        )

    captured: dict[str, object] = {}

    def fail_loud(slots, detected, suggestions):
        captured["slots"] = sorted(slots)
        captured["detected_present"] = detected is not None
        return {}  # signals "couldn't resolve"

    if allow_missing_slots:
        state = get_ams_state(
            slots_needed,
            config=config,
            machine_profile_name=machine,
            interactive=lambda *_: {},
        )
        return state

    state = get_ams_state(
        slots_needed,
        config=config,
        machine_profile_name=machine,
        interactive=fail_loud,
    )
    missing = slots_needed - state.keys()
    if missing:
        if captured.get("detected_present"):
            raise AMSError(
                f"printer reached but slots {sorted(missing)} could not be matched "
                "to an installed filament preset. Check the loaded filaments or "
                "use --manifest with an explicit slot/filament map."
            )
        raise AMSError(
            f"printer unreachable at {config.printer.ip} (slots needed: "
            f"{sorted(slots_needed)}). Power on the A1, verify it's on the "
            "network, and that LAN Only Mode + Developer Mode are still enabled."
        )
    return state


def _sanitize_filename(name: str) -> str:
    """Strip path separators and Windows-illegal characters from a filename."""
    import re

    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "makerworld"


def _parse_ams_mapping(s: str) -> list[int]:
    """Parse a comma-separated 0-indexed AMS-slot mapping like '2,0,1'."""
    out: list[int] = []
    for piece in s.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            v = int(piece)
        except ValueError as e:
            raise click.BadParameter(f"ams-mapping must be ints, got {piece!r}") from e
        if v < 0:
            raise click.BadParameter(f"ams-mapping values must be >= 0, got {v}")
        out.append(v)
    return out


def _launch_studio(exe: Path, threempf_path: Path) -> None:
    if not exe.is_file():
        click.echo(f"--open: bambu-studio.exe not at {exe}; skipping launch", err=True)
        return
    try:
        subprocess.Popen([str(exe), str(threempf_path)])
    except OSError as e:
        click.echo(f"--open: failed to launch Studio: {e}", err=True)
