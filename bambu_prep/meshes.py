"""Mesh pre-scaling primitives.

Bambu Studio's CLI exposes ``--scale`` as a *global* uniform-scale per
invocation, not per-input-file, and only as a scalar. To vary scale across
copies on one plate, we pre-scale each copy to its own STL and feed each
as a separate input on the CLI command line.

Scales come in two forms:

- ``float`` (uniform): applied to all three axes.
- ``tuple[float, float, float]`` (anisotropic): one factor per X / Y / Z axis.
  Stage 1.5 feature; needed for cases that stretch along one axis (e.g. a
  phone case that needs Z taller without growing X/Y).

This module provides the primitive: load a mesh, apply a scale, write to a
temp file. Decisions about *when* to call it (consolidation, job lifecycle)
live in :mod:`bambu_prep.plate`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import trimesh


ScaleFactor = float | tuple[float, float, float]
"""A uniform scale (one float) or anisotropic scale (one float per X/Y/Z)."""


class MeshError(ValueError):
    """Raised when a mesh can't be loaded or written."""


@dataclass(frozen=True)
class ScaledMesh:
    source: Path
    scale: ScaleFactor
    output: Path


def is_identity_scale(scale: ScaleFactor) -> bool:
    """True if ``scale`` would leave the mesh unchanged (1.0 or (1, 1, 1))."""
    if isinstance(scale, tuple):
        return all(s == 1.0 for s in scale)
    return scale == 1.0


def _validate_scale(scale: ScaleFactor) -> None:
    if isinstance(scale, tuple):
        if len(scale) != 3:
            raise MeshError(f"anisotropic scale must be a 3-tuple, got {scale!r}")
        if any(s <= 0 for s in scale):
            raise MeshError(f"all scale factors must be positive, got {scale!r}")
    elif scale <= 0:
        raise MeshError(f"scale must be positive, got {scale}")


def _apply_scale(mesh: trimesh.Trimesh, scale: ScaleFactor) -> trimesh.Trimesh:
    if is_identity_scale(scale):
        return mesh
    copy = mesh.copy()
    if isinstance(scale, tuple):
        copy.apply_scale(list(scale))
    else:
        copy.apply_scale(scale)
    return copy


def scale_suffix(scale: ScaleFactor) -> str:
    """Render a filesystem-safe filename suffix for a scale.

    Uniform: ``s1.0500`` (4 decimal places, lexical sort = numeric sort).
    Anisotropic: ``s1.0200x1.0200x1.0400``.
    """
    if isinstance(scale, tuple):
        return "s" + "x".join(f"{s:.4f}" for s in scale)
    return f"s{scale:.4f}"


def verify(source: Path) -> None:
    """Load a mesh to confirm it parses. Raise MeshError if not."""
    _load(source)


def prescale(source: Path, scale: ScaleFactor, output: Path) -> ScaledMesh:
    """Load ``source``, apply ``scale``, write to ``output``.

    ``output`` is overwritten if it already exists. Parent directories are
    created as needed. Returns a ScaledMesh record describing the result.
    """
    _validate_scale(scale)
    mesh = _load(source)
    mesh = _apply_scale(mesh, scale)

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        mesh.export(output)
    except (OSError, ValueError) as e:
        raise MeshError(f"failed to write {output}: {e}") from e

    return ScaledMesh(source=source, scale=scale, output=output)


def prescale_many(
    source: Path, scales: list[ScaleFactor], out_dir: Path
) -> list[ScaledMesh]:
    """Load ``source`` once, write a scaled copy per entry in ``scales``."""
    if not scales:
        return []
    for s in scales:
        _validate_scale(s)

    mesh = _load(source)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[ScaledMesh] = []
    for scale in scales:
        copy = _apply_scale(mesh, scale)
        output = out_dir / f"{source.stem}_{scale_suffix(scale)}.stl"
        try:
            copy.export(output)
        except (OSError, ValueError) as e:
            raise MeshError(f"failed to write {output}: {e}") from e
        results.append(ScaledMesh(source=source, scale=scale, output=output))

    return results


def make_job_dir(temp_scratch_dir: Path, job_id: str | None = None) -> Path:
    """Create and return a unique subdirectory under ``temp_scratch_dir``."""
    job_id = job_id or uuid.uuid4().hex[:12]
    job_dir = temp_scratch_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _load(source: Path) -> trimesh.Trimesh:
    """Load a mesh from disk. Raise MeshError on failure or wrong geometry type."""
    if not source.is_file():
        raise MeshError(f"mesh file not found: {source}")
    try:
        loaded = trimesh.load(source, force="mesh")
    except (OSError, ValueError) as e:
        raise MeshError(f"failed to load {source}: {e}") from e
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshError(f"{source} did not load as a single Trimesh (got {type(loaded).__name__})")
    if loaded.faces is None or len(loaded.faces) == 0:
        raise MeshError(f"{source} has no faces")
    return loaded
