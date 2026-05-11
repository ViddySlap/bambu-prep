"""Post-patch a generated ``.3mf`` to encode per-object AMS slot assignment.

Bambu Studio 02.05.00.66's CLI silently ignores ``--load-filament-ids`` when
producing an unsliced plate ``.3mf``. The flag parses without error and the
output has the right number of objects, but ``Metadata/model_settings.config``
never gets the per-object ``<metadata key="extruder" value="N"/>`` entries
that encode slot assignment. Verified experimentally 2026-05-11 on Ben's
install (initially documented as working in
``ViddyVault/Projects/3d-printing/wiki/research/slicer-control-options.md``
section 10 finding #5; that finding was incorrect).

This module rewrites the ``.3mf`` zip to inject those entries. Objects in
``Metadata/model_settings.config`` are matched to slots positionally: the
caller supplies a flat ``list[int]`` of slot numbers, one per ``<object>``
in document order. :mod:`bambu_prep.plate` derives that list from the same
``CliInput`` sequence it hands the CLI.

XML parsing is regex-based on purpose: Bambu emits malformed XML
(unescaped double quotes inside attribute values for ``compatible_printers``),
which strict parsers reject.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path


CONFIG_MEMBER = "Metadata/model_settings.config"
_OBJECT_TAG_RE = re.compile(r'<object\s+id="\d+">')
_OBJECT_BLOCK_RE = re.compile(r'(<object\s+id="\d+">)(.*?)(</object>)', re.DOTALL)
_EXTRUDER_RE = re.compile(r'<metadata\s+key="extruder"\s+value="\d+"\s*/>')


class PatchError(ValueError):
    """Raised when the .3mf doesn't have the shape patch_filament_slots expects."""


def patch_filament_slots(threempf_path: Path, slot_per_object: list[int]) -> None:
    """Patch ``threempf_path`` in place to encode per-object AMS slot assignment.

    Parameters
    ----------
    threempf_path
        A ``.3mf`` file written by :mod:`bambu_prep.plate`'s CLI invocation.
    slot_per_object
        One AMS slot number per ``<object>`` in ``model_settings.config``,
        in document order. Length must equal the object count.

    Raises
    ------
    PatchError
        If the zip is malformed, ``model_settings.config`` is missing, or
        the object count doesn't match the slot list length.
    """
    if not threempf_path.is_file():
        raise PatchError(f"no .3mf at {threempf_path}")

    try:
        with zipfile.ZipFile(threempf_path, "r") as zf:
            if CONFIG_MEMBER not in zf.namelist():
                raise PatchError(f"{threempf_path}: missing {CONFIG_MEMBER}")
            members = {name: zf.read(name) for name in zf.namelist()}
    except zipfile.BadZipFile as e:
        raise PatchError(f"{threempf_path}: not a valid zip: {e}") from e

    config_text = members[CONFIG_MEMBER].decode("utf-8")
    patched_text = _inject_extruder_metadata(config_text, slot_per_object)
    members[CONFIG_MEMBER] = patched_text.encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    threempf_path.write_bytes(buffer.getvalue())


def _inject_extruder_metadata(config_text: str, slot_per_object: list[int]) -> str:
    """Insert ``<metadata key="extruder" value="N"/>`` into each <object> block.

    Replaces an existing extruder entry if one is already present, so the
    patch is idempotent.
    """
    objects = list(_OBJECT_TAG_RE.finditer(config_text))
    if len(objects) != len(slot_per_object):
        raise PatchError(
            f"object count mismatch: model_settings.config has {len(objects)} "
            f"<object> entries, slot_per_object has {len(slot_per_object)}"
        )

    counter = {"i": 0}

    def replace_block(m: re.Match[str]) -> str:
        opening, body, closing = m.group(1), m.group(2), m.group(3)
        slot = slot_per_object[counter["i"]]
        counter["i"] += 1

        body_stripped, removed = _EXTRUDER_RE.subn("", body)
        # Match the indentation of existing metadata lines for readability.
        insertion = f'\n    <metadata key="extruder" value="{slot}"/>'
        return f"{opening}{insertion}{body_stripped}{closing}" if removed else (
            f"{opening}{insertion}{body}{closing}"
        )

    return _OBJECT_BLOCK_RE.sub(replace_block, config_text)
