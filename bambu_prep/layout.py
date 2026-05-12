"""Plate layout: compute per-object positions for tight packing.

Bambu Studio 02.05.00.66's CLI ``--arrange 1`` packs more conservatively
than the GUI's auto-arrange (e.g. three ~85x170mm iPhone cases don't fit
on the A1's 256x256 plate via CLI, but do fit in the GUI). To work around
this we run the CLI with ``--arrange 0`` (which leaves objects stacked at
the origin), then patch each build item's transform with positions we
compute here.

The Stage 1 layout strategy is the simplest one that handles common
real-world cases: a single row of N objects centered on the plate, equal
slots per object. If one row doesn't fit, fall back to a uniform grid.
If the grid can't fit either, signal overflow so the caller can drop
the lowest-priority item and retry.

Anything fancier (rotation, mixed-size bin packing, irregular arrangement)
is Stage 1.5+ territory.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil


PLATE_A1 = (256.0, 256.0)
"""(width_mm, depth_mm) for the Bambu Lab A1 build plate."""

DEFAULT_GAP_MM = 1.0
"""Minimum gap between adjacent objects' bounding boxes. The GUI's
auto-arrange uses near-zero spacing when objects don't share material;
1mm gives us a small safety margin without losing density."""


@dataclass(frozen=True)
class Bbox:
    """Pre-scaled STL bounding-box extents (width X, depth Y, height Z)."""

    dx: float
    dy: float
    dz: float


@dataclass(frozen=True)
class Placement:
    """One object's placement on the plate.

    ``center_x`` / ``center_y`` are where the object's bbox center goes on
    the plate. ``base_z`` is half the object's height so the object sits
    with its base at z=0 (Bambu's convention: the transform's translation
    is for the bbox center).
    """

    center_x: float
    center_y: float
    base_z: float


class LayoutError(ValueError):
    """Raised when a layout request is structurally invalid (not just overflow)."""


def compute_row_layout(
    bboxes: list[Bbox],
    *,
    plate: tuple[float, float] = PLATE_A1,
    gap: float = DEFAULT_GAP_MM,
) -> list[Placement] | None:
    """Place objects in a single row centered along Y, equal slots along X.

    Returns one Placement per bbox in input order, or ``None`` if the
    objects don't fit in a single row on the plate.
    """
    if not bboxes:
        return []

    plate_w, plate_d = plate
    total_w = sum(b.dx for b in bboxes) + gap * (len(bboxes) - 1)
    if total_w > plate_w:
        return None
    max_dy = max(b.dy for b in bboxes)
    if max_dy > plate_d:
        return None

    start_x = (plate_w - total_w) / 2.0
    center_y = plate_d / 2.0

    placements: list[Placement] = []
    cursor = start_x
    for b in bboxes:
        cx = cursor + b.dx / 2.0
        placements.append(Placement(center_x=cx, center_y=center_y, base_z=b.dz / 2.0))
        cursor += b.dx + gap
    return placements


def compute_grid_layout(
    bboxes: list[Bbox],
    *,
    plate: tuple[float, float] = PLATE_A1,
    gap: float = DEFAULT_GAP_MM,
) -> list[Placement] | None:
    """Place objects in a uniform grid sized to the largest bbox per axis.

    Fallback when a single row doesn't fit. Each cell is sized to fit the
    largest object's width and depth, so objects of varying sizes share
    cell dimensions (some cells have slack). Returns ``None`` if the grid
    can't fit on the plate.
    """
    if not bboxes:
        return []

    plate_w, plate_d = plate
    max_dx = max(b.dx for b in bboxes)
    max_dy = max(b.dy for b in bboxes)
    if max_dx > plate_w or max_dy > plate_d:
        return None

    cols = max(1, int((plate_w + gap) // (max_dx + gap)))
    rows = ceil(len(bboxes) / cols)
    if rows * (max_dy + gap) - gap > plate_d:
        return None

    cell_w = max_dx + gap
    cell_d = max_dy + gap
    grid_w = cols * cell_w - gap
    grid_d = rows * cell_d - gap
    origin_x = (plate_w - grid_w) / 2.0
    origin_y = (plate_d - grid_d) / 2.0

    placements: list[Placement] = []
    for i, b in enumerate(bboxes):
        row, col = divmod(i, cols)
        cx = origin_x + col * cell_w + max_dx / 2.0
        cy = origin_y + row * cell_d + max_dy / 2.0
        placements.append(Placement(center_x=cx, center_y=cy, base_z=b.dz / 2.0))
    return placements


def compute_layout(
    bboxes: list[Bbox],
    *,
    plate: tuple[float, float] = PLATE_A1,
    gap: float = DEFAULT_GAP_MM,
) -> list[Placement] | None:
    """Layout entry point. Try row first, fall back to grid. ``None`` on overflow."""
    row = compute_row_layout(bboxes, plate=plate, gap=gap)
    if row is not None:
        return row
    return compute_grid_layout(bboxes, plate=plate, gap=gap)


def transform_matrix(placement: Placement) -> str:
    """Build a 3MF build-item transform string for ``placement``.

    The 3MF spec uses a 4x3 affine transform serialized as 12 floats:
    rotation 3x3 followed by translation xyz. For axis-aligned placement
    we use the identity rotation and translate by (cx, cy, base_z).
    """
    return f"1 0 0 0 1 0 0 0 1 {placement.center_x} {placement.center_y} {placement.base_z}"
