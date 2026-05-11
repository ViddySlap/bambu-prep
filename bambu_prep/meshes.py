"""Mesh pre-scaling primitives.

Bambu Studio's CLI exposes ``--scale`` as a *global* uniform-scale per
invocation, not per-input-file. For the iPhone-case-at-N-scales workflow
(101%, 102%, ..., 110% of the same STL on one plate), the only way to get
varied scales onto one plate is to pre-scale each copy to its own STL and
feed each as a separate input on the CLI command line.

This module provides the primitive: load a mesh, apply a uniform scale,
write to a temp file. Decisions about *when* to call it (consolidation,
job lifecycle) live in :mod:`bambu_prep.plate`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import trimesh


class MeshError(ValueError):
    """Raised when a mesh can't be loaded or written."""


@dataclass(frozen=True)
class ScaledMesh:
    source: Path
    scale: float
    output: Path


def verify(source: Path) -> None:
    """Load a mesh to confirm it parses. Raise MeshError if not."""
    _load(source)


def prescale(source: Path, scale: float, output: Path) -> ScaledMesh:
    """Load ``source``, apply uniform ``scale``, write to ``output``.

    ``output`` is overwritten if it already exists. Parent directories are
    created as needed. Returns a ScaledMesh record describing the result.
    """
    if scale <= 0:
        raise MeshError(f"scale must be positive, got {scale}")

    mesh = _load(source)
    if scale != 1.0:
        mesh = mesh.copy()
        mesh.apply_scale(scale)

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        mesh.export(output)
    except (OSError, ValueError) as e:
        raise MeshError(f"failed to write {output}: {e}") from e

    return ScaledMesh(source=source, scale=scale, output=output)


def prescale_many(
    source: Path, scales: list[float], out_dir: Path
) -> list[ScaledMesh]:
    """Load ``source`` once, write a scaled copy per entry in ``scales``.

    Output filenames follow ``{source.stem}_s{scale:.4f}.stl``. The trailing
    zeros let plate.py sort lexically when assembling the CLI input order.
    """
    if not scales:
        return []
    if any(s <= 0 for s in scales):
        raise MeshError("all scales must be positive")

    mesh = _load(source)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[ScaledMesh] = []
    for scale in scales:
        copy = mesh.copy()
        if scale != 1.0:
            copy.apply_scale(scale)
        output = out_dir / f"{source.stem}_s{scale:.4f}.stl"
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
