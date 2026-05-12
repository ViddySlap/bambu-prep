"""Direct printer transport: upload a sliced ``.3mf`` to the A1 and start it.

Three operations on top of ``bambulabs-api``:

- :func:`upload`  - FTPS the file to the printer's root.
- :func:`preflight`  - parse the .3mf's filament requirements, compare to
    live AMS state, return a report (slot mismatches, wrong material types).
- :func:`start`  - issue the start-print MQTT command.

The skill calls :func:`upload` + :func:`preflight` first (no destructive
action), shows the report to the user, then calls :func:`start` only after
explicit confirmation. The CLI ``send`` subcommand wires this up: default
stops after preflight; ``--start`` proceeds.

All ``bambulabs-api`` calls are isolated behind injectable functions so the
unit tests don't need a real printer. :func:`extract_filament_requirements`
parses the .3mf zip directly and is fully unit-testable on disk-based
fixtures.
"""

from __future__ import annotations

import re
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bambu_prep.ams import TrayInfo, _default_live_query
from bambu_prep.config import Config


SLICE_INFO_MEMBER = "Metadata/slice_info.config"
MODEL_SETTINGS_MEMBER = "Metadata/model_settings.config"


@dataclass(frozen=True)
class FilamentRequirement:
    """One filament the sliced print needs."""

    filament_index: int  # 1-based, matches the gcode T# index
    material_type: str  # "PLA", "PETG", "ABS", "" if unknown
    color: str  # "#RRGGBBAA" or "" if unknown
    used_grams: float  # 0.0 if unknown


@dataclass(frozen=True)
class SlotCompatibility:
    """Per-slot compatibility check."""

    filament_index: int
    target_slot: int  # 1-based, the AMS slot this filament will use
    expected_type: str
    loaded_type: str
    loaded_color: str
    ok: bool
    reason: str


@dataclass(frozen=True)
class PreflightReport:
    """Output of :func:`preflight`."""

    filaments: list[FilamentRequirement]
    compatibility: list[SlotCompatibility]
    ams_mapping: list[int]  # ready to pass as start(ams_mapping=...)
    source: str  # "slice_info" | "model_settings" | "unknown"
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.compatibility)

    def human_summary(self) -> str:
        """Multi-line human-readable summary of the preflight result."""
        lines: list[str] = []
        if self.source == "unknown":
            lines.append(
                "preflight: could not determine filament requirements from .3mf "
                "(neither slice_info.config nor model_settings.config found)."
            )
        elif self.source == "model_settings":
            lines.append(
                "preflight: .3mf is unsliced; only AMS slot assignments are known "
                "(material types unknown until slice)."
            )
        else:
            lines.append(f"preflight: {len(self.filaments)} filament(s) needed:")
        for c in self.compatibility:
            mark = "ok" if c.ok else "FAIL"
            lines.append(
                f"  [{mark}] slot {c.target_slot}: "
                f"want {c.expected_type or '?'}, "
                f"loaded {c.loaded_type or '<empty>'}"
                f"{' - ' + c.reason if not c.ok else ''}"
            )
        for w in self.warnings:
            lines.append(f"  ! {w}")
        return "\n".join(lines)


class DispatchError(RuntimeError):
    """Raised when upload, preflight, or start fails."""


UploadFn = Callable[[Path, str, Config], str]
StartFn = Callable[[str, int, list[int], bool, Config], bool]
AMSQuery = Callable[[Config], "dict[int, TrayInfo] | None"]


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def upload(
    file_path: Path,
    *,
    config: Config,
    remote_name: str | None = None,
    upload_fn: UploadFn | None = None,
) -> str:
    """Upload a sliced ``.3mf`` to the A1 over FTPS. Returns the remote filename.

    Raises ``DispatchError`` if printer credentials are missing or the upload
    fails (no ``226`` in the FTP response).
    """
    if not file_path.is_file():
        raise DispatchError(f"upload: file not found: {file_path}")
    _require_printer_creds(config)
    remote_name = remote_name or file_path.name
    upload_fn = upload_fn or _default_upload
    result = upload_fn(file_path, remote_name, config)
    if "226" not in result:
        raise DispatchError(
            f"upload: FTPS did not report success (no '226' in response): {result!r}"
        )
    return remote_name


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def preflight(
    file_path: Path,
    *,
    config: Config,
    ams_mapping: list[int] | None = None,
    ams_query: AMSQuery | None = None,
) -> PreflightReport:
    """Compare the .3mf's filament requirements to live AMS state.

    ``ams_mapping`` defaults to the natural mapping ``[0, 1, ..., N-1]``
    (filament i goes to AMS slot i). Pass an override to compare against a
    custom mapping.
    """
    if not file_path.is_file():
        raise DispatchError(f"preflight: file not found: {file_path}")
    ams_query = ams_query or _default_live_query

    filaments, source = extract_filament_requirements(file_path)
    warnings: list[str] = []

    if ams_mapping is None:
        mapping = list(range(len(filaments) or 1))
    else:
        mapping = list(ams_mapping)
        if len(mapping) < len(filaments):
            warnings.append(
                f"ams_mapping has {len(mapping)} entries but .3mf needs "
                f"{len(filaments)} filaments; missing slots default to 0"
            )
            mapping = mapping + list(range(len(mapping), len(filaments)))

    trays = ams_query(config)
    if trays is None:
        warnings.append(
            "AMS unreachable; printer powered off or LAN/Developer mode disabled. "
            "Compatibility checks below assume loaded slots are empty."
        )
        trays = {}

    compat: list[SlotCompatibility] = []
    for fil in filaments:
        slot_zero_idx = mapping[fil.filament_index - 1]
        target_slot = slot_zero_idx + 1  # display as 1-based
        tray = trays.get(target_slot)
        loaded_type = (tray.type if tray else "") or ""
        loaded_color = (tray.color if tray else "") or ""
        ok, reason = _check_compat(fil, loaded_type, tray is not None)
        compat.append(
            SlotCompatibility(
                filament_index=fil.filament_index,
                target_slot=target_slot,
                expected_type=fil.material_type,
                loaded_type=loaded_type,
                loaded_color=loaded_color,
                ok=ok,
                reason=reason,
            )
        )

    return PreflightReport(
        filaments=filaments,
        compatibility=compat,
        ams_mapping=mapping,
        source=source,
        warnings=warnings,
    )


def _check_compat(
    fil: FilamentRequirement, loaded_type: str, slot_populated: bool
) -> tuple[bool, str]:
    if not slot_populated:
        return False, f"slot is empty; needs {fil.material_type or 'a filament'}"
    if not fil.material_type:
        # We don't know what the print expects (unsliced or unknown). Defer.
        return True, "expected type unknown; skipping type check"
    if not loaded_type:
        return False, "slot loaded but type unknown"
    if _materials_match(fil.material_type, loaded_type):
        return True, ""
    return False, f"material mismatch: print wants {fil.material_type}, slot has {loaded_type}"


def _materials_match(a: str, b: str) -> bool:
    """Normalize and compare two material type strings (PLA/PETG/etc.).

    The .3mf side typically uses the bare type ("PLA"); the AMS side may
    use "PLA" or "PLA Basic" / "PLA Matte" depending on RFID readout.
    Match on the leading material family token.
    """
    return _family(a) == _family(b) and _family(a) != ""


def _family(s: str) -> str:
    s = (s or "").strip().upper()
    for token in ("PLA-CF", "PLA", "PETG-CF", "PETG", "PETG-HF", "PET", "ABS", "ASA", "TPU", "PC", "PA"):
        if s.startswith(token) or s == token:
            return token
    return s


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def start(
    remote_filename: str,
    *,
    config: Config,
    plate: int = 1,
    ams_mapping: list[int] | None = None,
    use_ams: bool = True,
    start_fn: StartFn | None = None,
) -> bool:
    """Issue the start-print MQTT command.

    Returns ``True`` on success. Raises ``DispatchError`` if credentials are
    missing or the printer rejects the start.
    """
    _require_printer_creds(config)
    if plate < 1:
        raise DispatchError(f"plate must be 1-based, got {plate}")
    mapping = list(ams_mapping) if ams_mapping is not None else [0]
    start_fn = start_fn or _default_start
    ok = start_fn(remote_filename, plate, mapping, use_ams, config)
    if not ok:
        raise DispatchError(
            f"printer rejected start_print({remote_filename!r}, plate={plate}, "
            f"ams_mapping={mapping}); check printer state and try again"
        )
    return True


# ---------------------------------------------------------------------------
# .3mf inspection
# ---------------------------------------------------------------------------


_FILAMENT_TAG_RE = re.compile(
    r'<filament\b([^/>]*)/?>',
    re.IGNORECASE,
)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def extract_filament_requirements(
    file_path: Path,
) -> tuple[list[FilamentRequirement], str]:
    """Parse a Bambu ``.3mf`` for the filaments the print needs.

    Returns ``(requirements, source)`` where source identifies which file
    inside the .3mf we read:

    - ``"slice_info"``: the .3mf is sliced; we have type + color + usage.
    - ``"model_settings"``: the .3mf is unsliced; we have slot assignments
        only (type/color unknown).
    - ``"unknown"``: neither file is present (raw geometry .3mf, etc.).
    """
    with zipfile.ZipFile(file_path, "r") as zf:
        names = set(zf.namelist())
        if SLICE_INFO_MEMBER in names:
            blob = zf.read(SLICE_INFO_MEMBER).decode("utf-8", errors="replace")
            return _parse_slice_info(blob), "slice_info"
        if MODEL_SETTINGS_MEMBER in names:
            blob = zf.read(MODEL_SETTINGS_MEMBER).decode("utf-8", errors="replace")
            return _parse_model_settings(blob), "model_settings"
    return [], "unknown"


def _parse_slice_info(xml: str) -> list[FilamentRequirement]:
    """Pull ``<filament id=... type=... color=... used_g=.../>`` tags."""
    seen_ids: set[int] = set()
    out: list[FilamentRequirement] = []
    for m in _FILAMENT_TAG_RE.finditer(xml):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        try:
            fid = int(attrs.get("id", ""))
        except ValueError:
            continue
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        out.append(
            FilamentRequirement(
                filament_index=fid,
                material_type=attrs.get("type", "").strip(),
                color=attrs.get("color", "").strip(),
                used_grams=_safe_float(attrs.get("used_g", "")),
            )
        )
    out.sort(key=lambda r: r.filament_index)
    return out


_EXTRUDER_META_RE = re.compile(
    r'<metadata\s+key="extruder"\s+value="(\d+)"\s*/>'
)


def _parse_model_settings(xml: str) -> list[FilamentRequirement]:
    """Pull per-object ``extruder`` (= AMS slot) values from an unsliced .3mf."""
    slots: list[int] = []
    seen: set[int] = set()
    for m in _EXTRUDER_META_RE.finditer(xml):
        try:
            slot = int(m.group(1))
        except ValueError:
            continue
        if slot in seen:
            continue
        seen.add(slot)
        slots.append(slot)
    slots.sort()
    return [
        FilamentRequirement(
            filament_index=i + 1,
            material_type="",
            color="",
            used_grams=0.0,
        )
        for i, _ in enumerate(slots)
    ]


def _safe_float(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# bambulabs-api default backends
# ---------------------------------------------------------------------------


def _require_printer_creds(config: Config) -> None:
    if not config.printer.ip or not config.printer.access_code or not config.printer.serial:
        raise DispatchError(
            "printer credentials missing: populate [printer] in bambu_prep_config.toml "
            "or set BAMBU_A1_IP / BAMBU_A1_ACCESS_CODE / BAMBU_A1_SERIAL via [secret_refs]"
        )


def _default_upload(file_path: Path, remote_name: str, config: Config) -> str:
    """Connect to the A1 via ``bambulabs-api`` and upload over FTPS."""
    import bambulabs_api as bl  # noqa: PLC0415

    printer = bl.Printer(
        ip_address=config.printer.ip,
        access_code=config.printer.access_code,
        serial=config.printer.serial,
    )
    try:
        printer.mqtt_start()
        _wait_for_mqtt(printer, timeout=5.0)
        with file_path.open("rb") as f:
            return str(printer.upload_file(f, remote_name))
    except Exception as e:  # pragma: no cover - real-printer paths
        raise DispatchError(f"upload failed: {e}") from e
    finally:
        try:
            printer.mqtt_stop()
        except Exception:
            pass


def _default_start(
    filename: str,
    plate: int,
    ams_mapping: list[int],
    use_ams: bool,
    config: Config,
) -> bool:
    """Connect to the A1 via ``bambulabs-api`` and issue the start command."""
    import bambulabs_api as bl  # noqa: PLC0415

    printer = bl.Printer(
        ip_address=config.printer.ip,
        access_code=config.printer.access_code,
        serial=config.printer.serial,
    )
    try:
        printer.connect()
        _wait_for_mqtt(printer, timeout=5.0)
        # MQTT can take a moment after connect() before commands are accepted.
        time.sleep(2.0)
        return bool(
            printer.start_print(
                filename,
                plate_number=plate,
                use_ams=use_ams,
                ams_mapping=ams_mapping,
            )
        )
    except Exception as e:  # pragma: no cover - real-printer paths
        raise DispatchError(f"start failed: {e}") from e
    finally:
        try:
            printer.mqtt_stop()
        except Exception:
            pass


def _wait_for_mqtt(printer, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if printer.mqtt_client_ready():
            return
        time.sleep(0.1)
    raise DispatchError(f"MQTT not ready after {timeout}s")
