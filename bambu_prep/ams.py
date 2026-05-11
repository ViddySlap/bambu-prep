"""AMS slot loadout: live MQTT query with interactive fallback.

Public surface is :func:`get_ams_state`. The caller passes the set of slots
the print needs and the machine profile name; the function returns
``{slot: filament_preset_name}`` ready to hand to :func:`bambu_prep.plate.prepare_plate`.

Resolution flow:

1. **Live MQTT query** via ``bambulabs-api``. If the printer is reachable in
   LAN/Developer mode, each AMS slot's RFID-read tray data comes back.
2. **Heuristic match** of each tray's ``tray_id_name`` (e.g. ``"Bambu PLA Matte"``)
   to an installed filament preset whose suffix matches the machine
   (e.g. ``"@BBL A1"`` for the A1 with 0.4 nozzle).
3. **Interactive prompt** for any slot the first two steps couldn't resolve.

Both branches are dependency-injected (``live_query`` / ``interactive``) so
the orchestrator stays unit-testable without a real printer or real stdin.
``bambulabs-api`` is imported lazily inside the default live-query so test
environments without it still work.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from bambu_prep.config import Config
from bambu_prep.profiles import list_profiles


@dataclass(frozen=True)
class TrayInfo:
    """One AMS slot's RFID-reported loadout."""

    slot: int
    type: str
    sub_brand: str
    color: str
    info_idx: str
    name_hint: str


class AMSError(RuntimeError):
    """Raised when AMS state can't be fully resolved (e.g. user provided no value)."""


LiveQuery = Callable[[Config], "dict[int, TrayInfo] | None"]
Interactive = Callable[[set[int], "dict[int, TrayInfo] | None", dict[int, str]], dict[int, str]]
ListFilaments = Callable[[], list[str]]


_PRINTER_ABBREV = {
    "A1 mini": "A1M",
    "X1 Carbon": "X1C",
}


def get_ams_state(
    slots_needed: set[int],
    *,
    config: Config,
    machine_profile_name: str,
    list_filaments: ListFilaments | None = None,
    live_query: LiveQuery | None = None,
    interactive: Interactive | None = None,
) -> dict[int, str]:
    """Resolve ``{slot: filament_preset_name}`` for every slot in ``slots_needed``.

    Tries live MQTT first; falls back per-slot to an interactive prompt when
    the printer is unreachable or the RFID hint doesn't disambiguate to a
    single installed preset.
    """
    if not slots_needed:
        return {}

    list_filaments_fn = list_filaments or (
        lambda: [p.name for p in list_profiles(config, "filament")]
    )
    live = live_query or _default_live_query
    prompt = interactive or _default_interactive

    trays = live(config)

    resolved: dict[int, str] = {}
    missing: set[int] = set()
    suggestions: dict[int, str] = {}

    if trays is None:
        missing = set(slots_needed)
    else:
        suffix = filament_suffix_for(machine_profile_name)
        all_filaments = list_filaments_fn()
        for slot in slots_needed:
            tray = trays.get(slot)
            if tray is None:
                missing.add(slot)
                continue
            match = match_preset(tray, suffix, all_filaments)
            if match is not None:
                resolved[slot] = match
            else:
                missing.add(slot)
                hint = tray.name_hint or tray.sub_brand
                if hint:
                    suggestions[slot] = f"{hint} {suffix}".strip()

    if missing:
        extras = prompt(missing, trays, suggestions)
        for slot in missing:
            if slot not in extras:
                raise AMSError(f"no filament resolved for slot {slot}")
        resolved.update(extras)

    return resolved


def filament_suffix_for(machine_profile_name: str) -> str:
    """Derive a filament-preset suffix from a machine preset name.

    Bambu's filament presets are named like ``"Bambu PLA Matte @BBL A1"``,
    where the ``@BBL <printer>`` segment binds the preset to a specific
    printer. This function maps a machine preset name to the matching
    suffix.

    Examples
    --------
    >>> filament_suffix_for("Bambu Lab A1 0.4 nozzle")
    '@BBL A1'
    >>> filament_suffix_for("Bambu Lab A1 0.2 nozzle")
    '@BBL A1 0.2 nozzle'
    >>> filament_suffix_for("Bambu Lab A1 mini 0.4 nozzle")
    '@BBL A1M'
    """
    m = re.match(r"Bambu Lab (.+?)(?:\s+(\d+\.\d+) nozzle)?$", machine_profile_name)
    if not m:
        return f"@BBL {machine_profile_name}"

    printer = m.group(1).strip()
    nozzle = m.group(2)

    short = _PRINTER_ABBREV.get(printer, printer)
    suffix = f"@BBL {short}"
    if nozzle and nozzle != "0.4":
        suffix += f" {nozzle} nozzle"
    return suffix


def match_preset(tray: TrayInfo, suffix: str, available: list[str]) -> str | None:
    """Find an installed filament preset matching ``tray``'s RFID hint + ``suffix``.

    For Bambu RFID-tagged filaments, ``tray_sub_brands`` carries the human
    family name (``"PLA Basic"``, ``"PETG Translucent"``) and Bambu's preset
    files are named ``"Bambu {sub_brand} {suffix}"``. ``tray_id_name`` is an
    internal SKU code (``"A00-P06"``, ``"G01-P1"``) and is *not* useful for
    matching against preset filenames - verified live on Ben's A1 2026-05-11.

    Returns the preset's name on unambiguous match, or ``None`` otherwise.
    """
    if tray.sub_brand:
        target = f"Bambu {tray.sub_brand} {suffix}"
        if target in available:
            return target
        target = f"{tray.sub_brand} {suffix}"
        if target in available:
            return target

    hint = tray.sub_brand or tray.name_hint
    if not hint:
        return None

    candidates = [
        name
        for name in available
        if name.endswith(suffix) and hint in name and "@base" not in name
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _default_live_query(config: Config) -> dict[int, TrayInfo] | None:
    """Connect to the printer over MQTT and read AMS slot loadout.

    Returns ``None`` when the printer is unreachable, when credentials are
    missing, or when ``bambulabs-api`` isn't installed.
    """
    if not config.printer.ip or not config.printer.access_code or not config.printer.serial:
        return None

    try:
        import bambulabs_api as bl  # noqa: PLC0415  (lazy by design)
    except ImportError:
        return None

    printer = bl.Printer(
        ip_address=config.printer.ip,
        access_code=config.printer.access_code,
        serial=config.printer.serial,
    )
    try:
        printer.mqtt_start()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if printer.mqtt_client_ready():
                break
            time.sleep(0.1)
        else:
            return None

        ams_hub = printer.ams_hub()
        result: dict[int, TrayInfo] = {}
        for ams in ams_hub.ams_hub.values():
            for tray_index in range(4):
                tray = ams.get_filament_tray(tray_index)
                if tray is None:
                    continue
                slot = tray_index + 1
                result[slot] = TrayInfo(
                    slot=slot,
                    type=str(getattr(tray, "tray_type", "") or ""),
                    sub_brand=str(getattr(tray, "tray_sub_brands", "") or ""),
                    color=str(getattr(tray, "tray_color", "") or ""),
                    info_idx=str(getattr(tray, "tray_info_idx", "") or ""),
                    name_hint=str(getattr(tray, "tray_id_name", "") or ""),
                )
        return result or None
    except Exception:
        return None
    finally:
        try:
            printer.mqtt_stop()
        except Exception:
            pass


def _default_interactive(
    slots_needed: set[int],
    detected: dict[int, TrayInfo] | None,
    suggestions: dict[int, str],
) -> dict[int, str]:
    """Prompt the user via stderr/stdin for filament preset names per slot."""
    if detected is None:
        print(
            "[bambu-prep] printer unreachable; please confirm AMS slot loadout.",
            file=sys.stderr,
        )
    else:
        print("[bambu-prep] confirming AMS slot loadout:", file=sys.stderr)
        for slot in sorted(detected):
            t = detected[slot]
            descr = t.name_hint or t.sub_brand or t.type or "(unknown)"
            color = t.color or "?"
            print(f"  slot {slot}: {descr} (color {color})", file=sys.stderr)

    result: dict[int, str] = {}
    for slot in sorted(slots_needed):
        suggestion = suggestions.get(slot, "")
        prompt = f"  filament preset for slot {slot}"
        if suggestion:
            prompt += f" [{suggestion}]"
        prompt += ": "
        print(prompt, file=sys.stderr, end="", flush=True)
        line = input().strip()
        if not line:
            line = suggestion
        if not line:
            raise AMSError(f"no filament name provided for slot {slot}")
        result[slot] = line
    return result
