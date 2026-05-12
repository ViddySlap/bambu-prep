import pytest

from bambu_prep.layout import (
    Bbox,
    Placement,
    compute_grid_layout,
    compute_layout,
    compute_row_layout,
    transform_matrix,
)


# ----------------------------------------------------------------------- row layout


def test_row_layout_three_phone_cases_fits_a1() -> None:
    """3 cases at ~85x170mm fit in a row on 256x256 with 1mm gaps (the
    iPhone case workflow Ben tried in the GUI)."""
    cases = [Bbox(dx=83.0, dy=170.0, dz=11.0)] * 3
    placements = compute_row_layout(cases)
    assert placements is not None
    assert len(placements) == 3
    # Three cases at 83mm + two 1mm gaps = 251mm, centered: leftmost cx = 2.5 + 83/2 = 44
    assert pytest.approx(placements[0].center_x, abs=0.1) == 44.0
    assert pytest.approx(placements[2].center_x, abs=0.1) == 212.0
    # All centered on Y
    assert all(p.center_y == 128.0 for p in placements)
    # base_z is half the object height
    assert all(p.base_z == 5.5 for p in placements)


def test_row_layout_overflow_returns_none() -> None:
    """Four 100mm cubes need 403mm + gaps > 256mm. No fit."""
    cubes = [Bbox(dx=100.0, dy=100.0, dz=100.0)] * 4
    assert compute_row_layout(cubes) is None


def test_row_layout_empty_input() -> None:
    assert compute_row_layout([]) == []


def test_row_layout_single_object() -> None:
    placements = compute_row_layout([Bbox(dx=50.0, dy=50.0, dz=50.0)])
    assert placements is not None
    assert placements[0].center_x == 128.0  # centered on plate
    assert placements[0].center_y == 128.0


def test_row_layout_object_too_deep_for_plate() -> None:
    """Object's Y dimension exceeds plate depth -> no fit."""
    huge = [Bbox(dx=50.0, dy=300.0, dz=10.0)]
    assert compute_row_layout(huge) is None


# ----------------------------------------------------------------------- grid layout


def test_grid_layout_four_cubes_two_by_two() -> None:
    cubes = [Bbox(dx=100.0, dy=100.0, dz=50.0)] * 4
    placements = compute_grid_layout(cubes)
    assert placements is not None
    assert len(placements) == 4
    # 4 cubes in a 2x2 grid centered on 256x256 plate
    # cell width = 101mm (100 + 1 gap); grid width = 2 * 101 - 1 = 201mm
    # origin_x = (256 - 201) / 2 = 27.5; centers at 27.5 + 50 = 77.5, then 27.5 + 101 + 50 = 178.5
    assert pytest.approx(placements[0].center_x, abs=0.1) == 77.5
    assert pytest.approx(placements[1].center_x, abs=0.1) == 178.5
    assert pytest.approx(placements[2].center_x, abs=0.1) == 77.5
    assert pytest.approx(placements[3].center_x, abs=0.1) == 178.5


def test_grid_layout_overflow_returns_none() -> None:
    """Six 100mm cubes need 3 rows of 2; total depth 3*100 + 2 = 302mm > 256."""
    cubes = [Bbox(dx=100.0, dy=100.0, dz=100.0)] * 6
    assert compute_grid_layout(cubes) is None


# ----------------------------------------------------------------------- compute_layout entry point


def test_compute_layout_prefers_row_when_it_fits() -> None:
    """If a single row works, prefer it over a grid (looks better, slices faster)."""
    cases = [Bbox(dx=83.0, dy=170.0, dz=11.0)] * 3
    placements = compute_layout(cases)
    assert placements is not None
    # All three on the same y -> row layout
    assert len({p.center_y for p in placements}) == 1


def test_compute_layout_falls_back_to_grid() -> None:
    """If a row doesn't fit but a grid does, use the grid."""
    cubes = [Bbox(dx=80.0, dy=80.0, dz=40.0)] * 6
    # Row: 6 * 80 + 5 = 485mm > 256, no.
    # Grid: 3 cols * 81 - 1 = 242mm, 2 rows * 81 - 1 = 161mm, fits.
    placements = compute_layout(cubes)
    assert placements is not None
    assert len({p.center_y for p in placements}) == 2  # two rows


def test_compute_layout_returns_none_on_overflow() -> None:
    cubes = [Bbox(dx=100.0, dy=100.0, dz=100.0)] * 6
    assert compute_layout(cubes) is None


# ----------------------------------------------------------------------- transform_matrix


def test_transform_matrix_identity_rotation() -> None:
    p = Placement(center_x=128.0, center_y=128.0, base_z=5.5)
    assert transform_matrix(p) == "1 0 0 0 1 0 0 0 1 128.0 128.0 5.5"
