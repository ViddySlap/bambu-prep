"""Post-process ``bambu-studio.exe`` CLI output into a GUI-openable ``.3mf``.

Bambu Studio 02.05.00.66 has three CLI bugs that compound to produce a
``.3mf`` its own GUI loader rejects with "loading of a model file failed"
plus "no geometry data". Verified end-to-end on Ben's install 2026-05-11.

1. **Split-file 3MF Production Extension format.** The CLI emits a tiny
   ``3D/3dmodel.model`` with ``<components p:path="/3D/Objects/object_N.model"/>``
   references; actual geometry lives in per-object files. Bambu Studio's GUI
   loader doesn't open this form (it only writes / accepts the single-file form
   where geometry is inline under ``<resources>``).

2. **Stale Production Extension declarations.** After inlining, the ``p:``
   namespace is unused but ``requiredextensions="p"``, ``xmlns:p="..."``, and
   ``p:UUID="..."`` attributes remain - making the document declare a required
   extension it doesn't actually use, which trips Bambu's loader.

3. **Malformed XML in ``Metadata/model_settings.config``.** The CLI dumps
   process settings into the file with unescaped inner quotes, e.g.
   ``value=""Bambu Lab A1 0.4 nozzle""``. The GUI's strict XML parser refuses
   to load it. The GOOD MakerWorld-saved ``model_settings.config`` doesn't
   carry these process-settings keys at all.

:func:`finalize_cli_output` reads the CLI's ``.3mf``, applies all three
fixes plus per-object AMS slot patching, and writes the result back. This
is what :mod:`bambu_prep.plate` calls in the happy path.

:func:`patch_filament_slots` (the original slot-only patcher) is kept as a
public helper for users who want to patch a hand-edited ``.3mf`` from
another source.

All XML handling is regex-based on purpose - strict parsers reject the
CLI's malformed output.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path


CONFIG_MEMBER = "Metadata/model_settings.config"
ROOT_MODEL_MEMBER = "3D/3dmodel.model"
RELS_MEMBER = "3D/_rels/3dmodel.model.rels"
OBJECTS_PREFIX = "3D/Objects/"

_OBJECT_TAG_RE = re.compile(r'<object\s+id="\d+">')
_OBJECT_BLOCK_RE = re.compile(r'(<object\s+id="\d+">)(.*?)(</object>)', re.DOTALL)
_EXTRUDER_RE = re.compile(r'\s*<metadata\s+key="extruder"\s+value="\d+"\s*/>')

_PRODUCTION_EXT_NS_RE = re.compile(r'\s*xmlns:p="[^"]*"')
_REQUIRED_EXT_RE = re.compile(r'\s*requiredextensions="p"')
_P_UUID_RE = re.compile(r'\s+p:UUID="[^"]*"')
_COMPONENTS_RE = re.compile(r"<components>.*?</components>", re.DOTALL)

_CLI_INJECTED_METADATA_KEYS = (
    "compatible_printers",
    "default_acceleration",
    "elefant_foot_compensation",
    "inherits",
    "print_settings_id",
    "travel_speed",
)


class PatchError(ValueError):
    """Raised when the .3mf doesn't have the shape these helpers expect."""


def finalize_cli_output(threempf_path: Path, slot_per_object: list[int]) -> None:
    """Make a ``bambu-studio.exe --export-3mf`` output openable in the Studio GUI.

    Reads the zip once, applies the four-step rewrite (inline geometry,
    strip production-extension cruft, sanitize ``model_settings.config``,
    patch per-object AMS slot metadata), writes once.

    Parameters
    ----------
    threempf_path
        A ``.3mf`` file written by ``bambu-studio.exe`` in CLI plate-prep mode.
    slot_per_object
        One AMS slot number per ``<object>`` in ``model_settings.config``,
        in document order. Length must equal the object count.
    """
    members = _read_zip(threempf_path)

    root_text = members.get(ROOT_MODEL_MEMBER, b"").decode("utf-8")
    root_text = _inline_components(root_text, members)
    root_text = _strip_production_extension(root_text)
    members[ROOT_MODEL_MEMBER] = root_text.encode("utf-8")

    # Drop the per-object model files and the rels that reference them.
    for name in list(members):
        if name.startswith(OBJECTS_PREFIX) or name == RELS_MEMBER:
            del members[name]

    if CONFIG_MEMBER in members:
        cfg = members[CONFIG_MEMBER].decode("utf-8")
        cfg = _sanitize_model_settings(cfg)
        cfg = _inject_extruder_metadata(cfg, slot_per_object)
        members[CONFIG_MEMBER] = cfg.encode("utf-8")

    _write_zip(threempf_path, members)


def patch_filament_slots(threempf_path: Path, slot_per_object: list[int]) -> None:
    """Patch only the per-object AMS slot metadata in a ``.3mf``.

    Use for hand-edited ``.3mf`` files where the rest of the structure is
    already GUI-openable. For ``bambu-studio.exe`` CLI output, use
    :func:`finalize_cli_output` instead.
    """
    members = _read_zip(threempf_path)
    if CONFIG_MEMBER not in members:
        raise PatchError(f"{threempf_path}: missing {CONFIG_MEMBER}")
    cfg = members[CONFIG_MEMBER].decode("utf-8")
    members[CONFIG_MEMBER] = _inject_extruder_metadata(cfg, slot_per_object).encode("utf-8")
    _write_zip(threempf_path, members)


def _read_zip(threempf_path: Path) -> dict[str, bytes]:
    if not threempf_path.is_file():
        raise PatchError(f"no .3mf at {threempf_path}")
    try:
        with zipfile.ZipFile(threempf_path, "r") as zf:
            return {name: zf.read(name) for name in zf.namelist()}
    except zipfile.BadZipFile as e:
        raise PatchError(f"{threempf_path}: not a valid zip: {e}") from e


def _write_zip(threempf_path: Path, members: dict[str, bytes]) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    threempf_path.write_bytes(buffer.getvalue())


def _inline_components(root_text: str, members: dict[str, bytes]) -> str:
    """Replace ``<components p:path=".../object_N.model" objectid="K">`` references
    with the inner ``<mesh>...</mesh>`` content from the per-object files."""
    mesh_for_id: dict[str, str] = {}
    for name, data in members.items():
        if not name.startswith(OBJECTS_PREFIX) or not name.endswith(".model"):
            continue
        text = data.decode("utf-8", errors="replace")
        for m in re.finditer(r'<object\s+id="(\d+)"[^>]*>(.*?)</object>', text, re.DOTALL):
            mesh_for_id[m.group(1)] = m.group(2)

    if not mesh_for_id:
        return root_text  # nothing to inline

    def replace(match: re.Match[str]) -> str:
        body = match.group(0)
        objectid_match = re.search(r'objectid="(\d+)"', body)
        if not objectid_match:
            return body
        inner = mesh_for_id.get(objectid_match.group(1))
        return inner if inner is not None else body

    return _COMPONENTS_RE.sub(replace, root_text)


def _strip_production_extension(text: str) -> str:
    """Remove ``xmlns:p``, ``requiredextensions="p"``, and ``p:UUID="..."`` attrs.

    After ``_inline_components``, the production extension namespace is no
    longer used by any element. Leaving the declarations and ``requiredextensions``
    in place trips Bambu Studio's loader (it tries to honor a required
    extension that has no actual usage).
    """
    text = _PRODUCTION_EXT_NS_RE.sub("", text)
    text = _REQUIRED_EXT_RE.sub("", text)
    text = _P_UUID_RE.sub("", text)
    return text


def _sanitize_model_settings(text: str) -> str:
    """Remove CLI-injected metadata entries the GUI doesn't write.

    The CLI emits ``model_settings.config`` with process-settings metadata
    on each ``<object>`` (``compatible_printers``, ``default_acceleration``,
    etc.), and the ``compatible_printers`` value uses unescaped double quotes
    around the printer name, making the line invalid XML. The GUI's
    strict parser refuses to load the file. The GOOD MakerWorld-saved
    ``model_settings.config`` carries none of these keys; strip them.

    Also strips the empty ``gcode_file`` entry the CLI adds but the GUI
    doesn't.
    """
    for key in _CLI_INJECTED_METADATA_KEYS:
        text = re.sub(rf'\s*<metadata\s+key="{key}"[^/]*/>', "", text)
    text = re.sub(r'\s*<metadata\s+key="gcode_file"\s+value=""\s*/>', "", text)
    return text


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
        insertion = f'\n    <metadata key="extruder" value="{slot}"/>'
        return f"{opening}{insertion}{body_stripped}{closing}" if removed else (
            f"{opening}{insertion}{body}{closing}"
        )

    return _OBJECT_BLOCK_RE.sub(replace_block, config_text)
