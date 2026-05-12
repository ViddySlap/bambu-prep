"""Unconditionally retarget any ``.3mf`` to a chosen printer profile.

Motivation: a fetched MakerWorld ``.3mf`` ships with the maker's printer
profile baked into ``Metadata/project_settings.config``. If that maker
sliced for an X1 Carbon and Ben (or an agent) sends the file to the A1
without switching the printer selector in Studio, the A1 firmware silently
rejects the X1C-specific start macros and the print fails (no bed
leveling, mid-air extrusion). This was the 2026-05-11 clicker fidget
failure mode.

Defense: rewrite every fetched ``.3mf`` so its embedded settings target
the A1, period. No "if the maker already had A1 selected" branch; redundant
retargeting is fine. After retargeting, when Ben opens the file in Studio
the printer selector reads "Bambu Lab A1" automatically and the slice
output gets the full A1 start-gcode template.

Mechanism:

1. Load the target machine profile (e.g. ``Bambu Lab A1 0.4 nozzle``),
   recursively resolving its ``inherits`` chain and ``include`` template
   list. The result is a flat dict of every machine-bound setting key
   the A1 cares about, with A1's correct values.
2. Load the target filament profile (e.g. ``Bambu PLA Basic @BBL A1``)
   the same way for filament-bound fields.
3. Open the .3mf zip, read ``Metadata/project_settings.config``, and
   replace every machine-bound and filament-bound key with the A1
   values. Identity fields (``printer_model``, ``printer_settings_id``,
   ``filament_settings_id``, ``print_compatible_printers``) are set
   explicitly.
4. Update ``Metadata/slice_info.config``'s ``printer_model_id`` to the
   target machine's id (e.g. ``N2S``).
5. Re-zip in place (or to an explicit output path), preserving every
   other zip member (geometry, thumbnails, model_settings.config, etc.).

This is a Python-only operation. No ``bambu-studio.exe`` invocation. The
Studio CLI's incomplete profile resolution (which strips machine-start-
gcode templates to a 3-line stub) is exactly what we want to avoid.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from bambu_prep.config import Config
from bambu_prep.profiles import ProfileError, resolve


PROJECT_SETTINGS_MEMBER = "Metadata/project_settings.config"
SLICE_INFO_MEMBER = "Metadata/slice_info.config"

_META_KEYS = {
    "type",
    "name",
    "inherits",
    "from",
    "instantiation",
    "setting_id",
    "include",
    "version",
    "url",
    "is_custom_defined",
    "filament_id",
}
"""Keys in a Bambu Studio profile JSON that describe the profile itself,
not a slicer setting. These are not flattened into the merged result."""

_IDENTITY_KEYS = {
    "printer_settings_id",
    "filament_settings_id",
    "print_settings_id",
}
"""Keys that name the profile itself rather than carry a setting value.
Bambu profile JSONs sometimes ship these as empty-string placeholders
(meant to be set when the profile is materialized). Bulk-applying the
profile would clobber a real id with the empty placeholder, so we
exclude them from bulk-apply and let the explicit identity setters
own them."""


@dataclass(frozen=True)
class RetargetResult:
    """What :func:`retarget` did, for reporting back to the CLI/skill."""

    input_path: Path
    output_path: Path
    target_machine: str
    target_machine_model_id: str
    target_filament: str
    fields_changed: int
    """Number of fields whose value differed from the target and was
    overwritten. Useful for telling Ben "we changed 27 fields" so he
    knows it actually did something."""

    was_already_target: bool
    """``True`` when zero fields needed changing (e.g. file was already
    A1-targeted). Retargeting is still performed for idempotence."""


def retarget(
    file_path: Path,
    *,
    config: Config,
    output_path: Path | None = None,
    target_machine_profile: str = "Bambu Lab A1 0.4 nozzle",
    target_filament_profile: str = "Bambu PLA Basic @BBL A1",
    target_machine_model_id: str = "N2S",
    target_machine_model: str = "Bambu Lab A1",
) -> RetargetResult:
    """Rewrite ``file_path`` so its embedded settings target the A1.

    ``output_path`` defaults to ``file_path`` (in-place). Pass an explicit
    path to write a new file alongside the original.
    """
    if not file_path.is_file():
        raise FileNotFoundError(f"retarget: file not found: {file_path}")

    out = output_path if output_path is not None else file_path

    try:
        machine_profile = resolve(config, "machine", target_machine_profile)
        filament_profile = resolve(config, "filament", target_filament_profile)
    except ProfileError as e:
        raise RetargetError(f"could not resolve target profile: {e}") from e

    machine_dir = machine_profile.path.parent
    filament_dir = filament_profile.path.parent

    machine_fields = _load_profile_chain(target_machine_profile, machine_dir)
    filament_fields = _load_profile_chain(target_filament_profile, filament_dir)

    with zipfile.ZipFile(file_path, "r") as zin:
        members = zin.namelist()
        if PROJECT_SETTINGS_MEMBER not in members:
            raise RetargetError(
                f"retarget: {file_path.name} has no {PROJECT_SETTINGS_MEMBER}; "
                "is this a Bambu Studio .3mf project file?"
            )
        ps_blob = zin.read(PROJECT_SETTINGS_MEMBER).decode("utf-8")
        original_members: dict[str, bytes] = {
            name: zin.read(name) for name in members
        }

    try:
        ps = json.loads(ps_blob)
    except json.JSONDecodeError as e:
        raise RetargetError(
            f"retarget: {PROJECT_SETTINGS_MEMBER} is not valid JSON: {e}"
        ) from e

    fields_changed = _patch_project_settings(
        ps,
        machine_fields=machine_fields,
        filament_fields=filament_fields,
        target_machine_model=target_machine_model,
        target_printer_settings_id=target_machine_profile,
        target_filament_settings_id=target_filament_profile,
    )

    new_ps_blob = json.dumps(ps, indent=4).encode("utf-8")
    original_members[PROJECT_SETTINGS_MEMBER] = new_ps_blob

    if SLICE_INFO_MEMBER in original_members:
        si_blob = original_members[SLICE_INFO_MEMBER].decode("utf-8")
        new_si_blob, si_changed = _patch_slice_info(si_blob, target_machine_model_id)
        original_members[SLICE_INFO_MEMBER] = new_si_blob.encode("utf-8")
        if si_changed:
            fields_changed += 1

    _write_zip(out, original_members)

    return RetargetResult(
        input_path=file_path,
        output_path=out,
        target_machine=target_machine_profile,
        target_machine_model_id=target_machine_model_id,
        target_filament=target_filament_profile,
        fields_changed=fields_changed,
        was_already_target=fields_changed == 0,
    )


class RetargetError(RuntimeError):
    """Raised when retarget can't proceed (missing profile, malformed .3mf)."""


# ---------------------------------------------------------------------------
# profile loading
# ---------------------------------------------------------------------------


def _load_profile_chain(profile_name: str, profile_dir: Path) -> dict:
    """Recursively load a Bambu profile JSON's ``inherits`` and ``include``
    chain, returning a flat dict of every setting key resolved.

    Parent values are loaded first, then child overrides; includes are
    treated as siblings whose values are merged after the parent's. Meta
    keys (``type``, ``name``, etc.) are stripped from the result.
    """
    out: dict = {}
    visited: set[str] = set()

    def walk(name: str) -> None:
        if name in visited:
            return
        visited.add(name)

        path = profile_dir / f"{name}.json"
        if not path.is_file():
            # Some include names refer to siblings that exist as profiles
            # named differently from the file. Try a directory scan as
            # a last resort.
            candidate = _find_profile_by_name(profile_dir, name)
            if candidate is None:
                return
            path = candidate

        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return

        parent = d.get("inherits")
        if isinstance(parent, str) and parent:
            walk(parent)

        for inc in d.get("include", []) or []:
            if isinstance(inc, str):
                walk(inc)

        for k, v in d.items():
            if k in _META_KEYS:
                continue
            out[k] = v

    walk(profile_name)
    return out


def _find_profile_by_name(profile_dir: Path, name: str) -> Path | None:
    """Scan ``profile_dir`` for a JSON file whose ``"name"`` field equals
    ``name``. Some Bambu profiles' filenames don't match their declared
    name (rare, but it happens with template files)."""
    for path in profile_dir.glob("*.json"):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if d.get("name") == name:
            return path
    return None


# ---------------------------------------------------------------------------
# project_settings.config patching
# ---------------------------------------------------------------------------


def _patch_project_settings(
    ps: dict,
    *,
    machine_fields: dict,
    filament_fields: dict,
    target_machine_model: str,
    target_printer_settings_id: str,
    target_filament_settings_id: str,
) -> int:
    """Mutate ``ps`` in place. Return the number of fields whose value changed."""
    changed = 0

    # Determine the filament slot count from the existing file. Most
    # MakerWorld bundles ship with 4 entries (full AMS placeholder), but
    # single-color prints have fewer. Honor whatever's there; default 1.
    cur_filaments = ps.get("filament_settings_id", [])
    if not isinstance(cur_filaments, list):
        cur_filaments = []
    n_filaments = max(len(cur_filaments), 1)

    for k, target_v in machine_fields.items():
        if k in _IDENTITY_KEYS:
            continue
        if _set_if_different(ps, k, target_v):
            changed += 1

    for k, target_v in filament_fields.items():
        if k in _IDENTITY_KEYS:
            continue
        # Filament fields are usually arrays sized to n_filaments. The
        # profile JSON has them as either scalars (rare) or single-element
        # arrays we need to broadcast.
        if isinstance(target_v, list) and len(target_v) == 1 and n_filaments > 1:
            broadcast = target_v * n_filaments
            if _set_if_different(ps, k, broadcast):
                changed += 1
        else:
            if _set_if_different(ps, k, target_v):
                changed += 1

    # Identity fields. These are what Studio reads to decide "which printer
    # is selected" when the file is opened.
    if _set_if_different(ps, "printer_model", target_machine_model):
        changed += 1
    if _set_if_different(ps, "printer_settings_id", target_printer_settings_id):
        changed += 1

    new_filament_ids = [target_filament_settings_id] * n_filaments
    if _set_if_different(ps, "filament_settings_id", new_filament_ids):
        changed += 1

    # ``print_compatible_printers`` is the maker's "this profile is for
    # printer X/Y/Z" list. After retargeting, the only compatible printer
    # is the target.
    if _set_if_different(ps, "print_compatible_printers", [target_printer_settings_id]):
        changed += 1

    return changed


def _set_if_different(ps: dict, key: str, value) -> bool:
    """Set ``ps[key] = value`` if the current value differs. Return whether
    a change happened. Treats missing keys as "different"."""
    cur = ps.get(key, _SENTINEL)
    if cur == value:
        return False
    ps[key] = value
    return True


_SENTINEL = object()


# ---------------------------------------------------------------------------
# slice_info.config patching
# ---------------------------------------------------------------------------


def _patch_slice_info(xml: str, target_model_id: str) -> tuple[str, bool]:
    """Rewrite the ``printer_model_id`` metadata in ``slice_info.config``.

    Returns ``(new_xml, changed)``. If the file has no ``printer_model_id``
    tag we leave it untouched. (Unsliced files often don't have this tag;
    that's fine, the validator handles its absence.)
    """
    import re

    pattern = re.compile(
        r'(<metadata\s+key="printer_model_id"\s+value=")([^"]*)(")',
        re.IGNORECASE,
    )
    match = pattern.search(xml)
    if match is None:
        return xml, False
    if match.group(2) == target_model_id:
        return xml, False
    return pattern.sub(rf"\g<1>{target_model_id}\g<3>", xml, count=1), True


# ---------------------------------------------------------------------------
# zip rewriting
# ---------------------------------------------------------------------------


def _write_zip(out_path: Path, members: dict[str, bytes]) -> None:
    """Atomically write a new .3mf zip with the given members.

    Writes to a sibling tempfile and renames, so a partially-written zip
    doesn't clobber a good file on disk if the process is interrupted.
    """
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in members.items():
            zout.writestr(name, data)
    tmp.replace(out_path)


# ---------------------------------------------------------------------------
# convenience exports for testing
# ---------------------------------------------------------------------------


__all__ = [
    "RetargetError",
    "RetargetResult",
    "retarget",
    "_load_profile_chain",
    "_patch_project_settings",
    "_patch_slice_info",
]
