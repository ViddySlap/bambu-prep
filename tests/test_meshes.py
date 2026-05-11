from pathlib import Path

import numpy as np
import pytest
import trimesh

from bambu_prep.meshes import (
    MeshError,
    ScaledMesh,
    make_job_dir,
    prescale,
    prescale_many,
    verify,
)


def _write_cube(path: Path, side: float = 10.0) -> None:
    """Write a unit cube STL with the given side length, centered on origin."""
    box = trimesh.creation.box(extents=(side, side, side))
    path.parent.mkdir(parents=True, exist_ok=True)
    box.export(path)


def _bbox_extents(path: Path) -> np.ndarray:
    mesh = trimesh.load(path, force="mesh")
    assert isinstance(mesh, trimesh.Trimesh)
    return mesh.bounding_box.extents


def test_verify_accepts_valid_stl(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl, side=20.0)
    verify(stl)


def test_verify_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(MeshError, match="not found"):
        verify(tmp_path / "nope.stl")


def test_verify_rejects_garbage(tmp_path: Path) -> None:
    bad = tmp_path / "bad.stl"
    bad.write_bytes(b"this is not an STL")
    with pytest.raises(MeshError):
        verify(bad)


def test_prescale_identity_writes_same_bbox(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl, side=10.0)
    out = tmp_path / "out.stl"

    result = prescale(stl, 1.0, out)

    assert isinstance(result, ScaledMesh)
    assert result.output == out
    assert result.scale == 1.0
    extents = _bbox_extents(out)
    assert np.allclose(extents, [10.0, 10.0, 10.0])


def test_prescale_uniform_scales_bbox(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl, side=10.0)
    out = tmp_path / "out.stl"

    prescale(stl, 1.05, out)

    extents = _bbox_extents(out)
    assert np.allclose(extents, [10.5, 10.5, 10.5])


def test_prescale_creates_parent_dir(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "nested" / "dir" / "out.stl"

    prescale(stl, 1.0, out)

    assert out.is_file()


def test_prescale_rejects_nonpositive_scale(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.stl"

    with pytest.raises(MeshError, match="positive"):
        prescale(stl, 0.0, out)
    with pytest.raises(MeshError, match="positive"):
        prescale(stl, -1.0, out)


def test_prescale_many_writes_per_scale(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl, side=10.0)
    out_dir = tmp_path / "scaled"

    results = prescale_many(stl, [1.01, 1.05, 1.10], out_dir)

    assert len(results) == 3
    assert [r.scale for r in results] == [1.01, 1.05, 1.10]
    for r in results:
        assert r.output.is_file()
        assert r.source == stl

    # Bounding boxes should reflect each scale
    assert np.allclose(_bbox_extents(results[0].output), [10.1, 10.1, 10.1])
    assert np.allclose(_bbox_extents(results[1].output), [10.5, 10.5, 10.5])
    assert np.allclose(_bbox_extents(results[2].output), [11.0, 11.0, 11.0])


def test_prescale_many_filenames_sort_lexically(tmp_path: Path) -> None:
    """Filenames embed scale with fixed precision so lexical sort = numeric sort."""
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out_dir = tmp_path / "scaled"

    results = prescale_many(stl, [1.10, 1.02, 1.05], out_dir)
    names_in_call_order = [r.output.name for r in results]
    names_lex_sorted = sorted(names_in_call_order)
    expected_numeric_order = ["cube_s1.0200.stl", "cube_s1.0500.stl", "cube_s1.1000.stl"]
    assert names_lex_sorted == expected_numeric_order


def test_prescale_anisotropic_3tuple(tmp_path: Path) -> None:
    """Per-axis scale applies different factors to X, Y, Z."""
    stl = tmp_path / "cube.stl"
    _write_cube(stl, side=10.0)
    out = tmp_path / "out.stl"

    prescale(stl, (1.02, 1.02, 1.04), out)

    extents = _bbox_extents(out)
    assert np.allclose(extents, [10.2, 10.2, 10.4])


def test_prescale_identity_3tuple_writes_unchanged(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl, side=10.0)
    out = tmp_path / "out.stl"

    prescale(stl, (1.0, 1.0, 1.0), out)

    extents = _bbox_extents(out)
    assert np.allclose(extents, [10.0, 10.0, 10.0])


def test_prescale_anisotropic_filename_encodes_all_axes(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out_dir = tmp_path / "scaled"

    results = prescale_many(stl, [(1.02, 1.02, 1.04), (1.025, 1.025, 1.05)], out_dir)
    names = [r.output.name for r in results]
    assert names == [
        "cube_s1.0200x1.0200x1.0400.stl",
        "cube_s1.0250x1.0250x1.0500.stl",
    ]


def test_prescale_rejects_bad_3tuple(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    out = tmp_path / "out.stl"
    with pytest.raises(MeshError, match="3-tuple"):
        prescale(stl, (1.0, 1.0), out)  # type: ignore[arg-type]
    with pytest.raises(MeshError, match="positive"):
        prescale(stl, (1.0, -1.0, 1.0), out)


def test_prescale_many_empty_returns_empty(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    assert prescale_many(stl, [], tmp_path / "scaled") == []


def test_prescale_many_rejects_nonpositive(tmp_path: Path) -> None:
    stl = tmp_path / "cube.stl"
    _write_cube(stl)
    with pytest.raises(MeshError, match="positive"):
        prescale_many(stl, [1.0, 0.0, 1.05], tmp_path / "scaled")


def test_make_job_dir_creates_unique_dirs(tmp_path: Path) -> None:
    a = make_job_dir(tmp_path)
    b = make_job_dir(tmp_path)
    assert a.is_dir()
    assert b.is_dir()
    assert a != b
    assert a.parent == tmp_path


def test_make_job_dir_respects_explicit_id(tmp_path: Path) -> None:
    job = make_job_dir(tmp_path, job_id="custom-job-42")
    assert job.name == "custom-job-42"
    assert job.is_dir()
