from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from terrain_system.cli import main
from terrain_system.edits import (
    CrossingEditOp,
    EditError,
    EditOp,
    LinearWorkOp,
    RoadEditOp,
    RiverEditOp,
    next_edit_id,
)
from terrain_system.model import ChunkKey, generate_grid
from terrain_system.parameters import ParameterError, TerrainParameters
from terrain_system.project import create_project, load_project


def params(resolution=5, chunk_size=10.0):
    return TerrainParameters(
        seed=12345,
        chunk_size=chunk_size,
        resolution=resolution,
        scale=24.0,
        octaves=3,
        persistence=0.5,
        lacunarity=2.0,
        base_height=0.0,
        amplitude=8.0,
    )


def test_generation_is_order_and_process_independent():
    p = params()
    first = generate_grid(p, ChunkKey(1, 0), [])
    second = generate_grid(p, ChunkKey(0, 0), [])
    later = generate_grid(p, ChunkKey(1, 0), [])

    assert first == later
    assert len(first["heights"]) == p.resolution

    for row in range(p.resolution):
        assert second["heights"][row][-1] == first["heights"][row][0]
    for column in range(p.resolution):
        assert second["heights"][-1][column] == generate_grid(p, ChunkKey(0, 1), [])["heights"][0][column]


def test_project_chunks_reload_and_stitch(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0), ChunkKey(0, 1), ChunkKey(1, 1)])

    reloaded = load_project(tmp_path)
    assert reloaded.check_loaded_seams() == []


def test_edit_survives_regeneration_and_updates_existing_chunks(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0)])
    before = project.load_chunk(ChunkKey(0, 0))["heights"][0][0]

    edit = EditOp(id=next_edit_id(project.edits), x=0.0, y=0.0, radius=6.0, delta=4.0)
    affected = project.add_edit(edit)
    assert affected == [ChunkKey(0, 0)]

    reloaded = load_project(tmp_path)
    reloaded.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0)])
    after = reloaded.load_chunk(ChunkKey(0, 0))["heights"][0][0]
    untouched = reloaded.load_chunk(ChunkKey(1, 0))["heights"][0][0]

    assert after == pytest.approx(before + 4.0)
    assert untouched == reloaded.load_chunk(ChunkKey(1, 0))["base_heights"][0][0]
    assert reloaded.check_loaded_seams() == []


def test_invalid_parameters_keep_existing_project(tmp_path: Path):
    project = create_project(tmp_path, params(resolution=4))
    project.generate_chunks([ChunkKey(0, 0)])
    original = (tmp_path / "project.json").read_text(encoding="utf-8")

    with pytest.raises(ParameterError):
        create_project(tmp_path, params(resolution=1), overwrite=True)

    reloaded = load_project(tmp_path)
    assert reloaded.parameters.resolution == 4
    assert (tmp_path / "project.json").read_text(encoding="utf-8") == original


def test_cli_workflow_and_invalid_exit_code(tmp_path: Path, capsys):
    assert main(["init", str(tmp_path / "good"), "--seed", "7", "--chunk-size", "12", "--resolution", "4", "--scale", "20"]) == 0
    project_path = tmp_path / "good"
    assert main(["generate", str(project_path), "0,0", "1,0", "0,1", "1,1"]) == 0
    assert main(["edit", str(project_path), "--x", "1", "--y", "2", "--radius", "3", "--delta", "2"]) == 0
    assert main(["stitch", str(project_path)]) == 0

    output_lines = capsys.readouterr().out.splitlines()
    last_object = len(output_lines) - 1 - output_lines[::-1].index("{")
    payload = json.loads("\n".join(output_lines[last_object:]))
    assert payload["status"] == "ok"

    bad = tmp_path / "bad"
    assert main(["init", str(bad), "--seed", "1", "--chunk-size", "0", "--resolution", "4", "--scale", "20"]) == 1
    assert not bad.exists()


def test_road_crosses_chunk_boundary_and_survives_regeneration(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0)])

    road = RoadEditOp(
        id=1,
        x1=-2.0,
        y1=0.0,
        x2=12.0,
        y2=0.0,
        width=2.0,
        height=5.0,
        shoulder=2.0,
    )
    assert set(project.add_edit(road)) == {ChunkKey(0, 0), ChunkKey(1, 0)}
    assert project.check_loaded_seams() == []

    left = project.load_chunk(ChunkKey(0, 0))
    right = project.load_chunk(ChunkKey(1, 0))
    assert left["heights"][0][-1] == 5.0
    assert right["heights"][0][0] == 5.0

    reloaded = load_project(tmp_path)
    reloaded.generate_chunks([ChunkKey(0, 0)])
    assert reloaded.load_chunk(ChunkKey(0, 0))["heights"][0][-1] == 5.0
    assert reloaded.check_loaded_seams() == []


def test_road_intersections_keep_explicit_priority_not_last_edit(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    first = LinearWorkOp(1, "road", -2.0, 0.0, 12.0, 0.0, 2.0, 4.0, priority=1.0)
    second = LinearWorkOp(2, "river", 0.0, -2.0, 0.0, 12.0, 2.0, 7.0, priority=0.0)
    project.add_edit(first)

    _, relations = project.add_edit_report(second)
    assert relations == [
        {
            "work_a": 1,
            "work_b": 2,
            "point": [0.0, 0.0],
            "kept": 1,
            "overridden": 2,
            "rule": "higher priority; earlier id breaks ties",
        }
    ]
    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 4.0

    affected, invalidated = project.undo_edit(1)
    assert affected == [ChunkKey(0, 0)]
    assert invalidated == []
    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 7.0

    reloaded = load_project(tmp_path)
    affected, _ = reloaded.undo_edit(2)
    assert affected == [ChunkKey(0, 0)]
    final = reloaded.load_chunk(ChunkKey(0, 0))
    assert final["heights"] == final["base_heights"]


def test_equal_priority_resolves_to_earlier_revision_independent_of_add_order(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    low = LinearWorkOp(1, "road", 0.0, -2.0, 0.0, 12.0, 2.0, 7.0)
    high = LinearWorkOp(2, "road", -2.0, 0.0, 12.0, 0.0, 2.0, 4.0)
    project.add_edit(low)
    project.add_edit(high)
    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 7.0

    reloaded = load_project(tmp_path)
    assert reloaded.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 7.0


def test_manual_crossing_adjustment_overrides_winner_and_is_reported_on_preview(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    road = LinearWorkOp(1, "road", -2.0, 0.0, 12.0, 0.0, 6.0, 4.0)
    river = LinearWorkOp(2, "river", 0.0, -2.0, 0.0, 12.0, 6.0, 7.0, priority=1.0)
    project.add_edit(road)
    project.add_edit(river)
    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 7.0

    crossing = CrossingEditOp(
        id=next_edit_id(project.edits),
        work_a=1,
        work_b=2,
        height=5.5,
        radius=1.5,
    )
    preview = project.preview_edit(crossing)
    assert preview["affected_chunks"] == [[0, 0]]
    assert preview["crossings"][0]["kept"] == 2
    assert load_project(tmp_path).edits == project.edits

    affected, _ = project.add_edit_report(crossing)
    assert affected == [ChunkKey(0, 0)]
    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 5.5


def test_removing_one_work_invalidates_manual_crossing_with_explanation(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    road = RoadEditOp(1, -2.0, 0.0, 12.0, 0.0, 6.0, 4.0)
    river = RiverEditOp(2, 0.0, -2.0, 0.0, 12.0, 6.0, -3.0)
    project.add_edit(road)
    project.add_edit(river)
    crossing = CrossingEditOp(3, 1, 2, 1.0, 1.5)
    project.add_edit(crossing)
    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 1.0

    affected, invalidated = project.undo_edit(1)
    assert affected == [ChunkKey(0, 0)]
    assert invalidated == [
        {
            "edit_id": 3,
            "state": "invalidated",
            "retained": True,
            "reason": "work 1 was removed",
        }
    ]

    reloaded = load_project(tmp_path)
    stored_crossing = next(edit for edit in reloaded.edits if edit.id == 3)
    assert stored_crossing.state == "invalidated"
    assert stored_crossing.reason == "work 1 was removed"
    assert reloaded.load_chunk(ChunkKey(0, 0))["heights"][0][0] == pytest.approx(-3.0)

    affected, invalidated = reloaded.undo_edit(3)
    assert affected == []
    assert invalidated == []


def test_crossing_requires_existing_intersecting_works(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    project.add_edit(RoadEditOp(1, -2.0, 0.0, 12.0, 0.0, 2.0, 4.0))

    missing = CrossingEditOp(2, 1, 9, 1.0, 1.0)
    with pytest.raises(EditError, match="referenced work not found"):
        project.add_edit(missing)

    parallel = RiverEditOp(2, -2.0, 5.0, 12.0, 5.0, 2.0, -3.0)
    project.add_edit(parallel)
    disjoint = CrossingEditOp(3, 1, 2, 1.0, 1.0)
    with pytest.raises(EditError, match="do not intersect"):
        project.add_edit(disjoint)


def test_negative_coordinate_chunks_are_deterministic_and_seamless(tmp_path: Path):
    keys = [ChunkKey(-1, -1), ChunkKey(0, 0), ChunkKey(-1, 0), ChunkKey(0, -1)]
    project = create_project(tmp_path / "a", params())
    project.generate_chunks(keys)
    road = LinearWorkOp(1, "road", -15.0, -5.0, 5.0, 5.0, 2.0, 6.0)
    river = LinearWorkOp(2, "river", -5.0, 5.0, -5.0, -15.0, 2.0, -2.0, priority=2.0)
    project.add_edit(road)
    project.add_edit(river)
    project.add_edit(CrossingEditOp(3, 1, 2, 1.5, 2.0))
    assert project.check_loaded_seams() == []

    rebuilt = create_project(tmp_path / "b", params())
    rebuilt.edits = list(project.edits)
    rebuilt.generate_chunks(list(reversed(keys)))
    assert rebuilt.check_loaded_seams() == []
    for key in keys:
        assert rebuilt.load_chunk(key)["heights"] == project.load_chunk(key)["heights"]

    reloaded = load_project(tmp_path / "a")
    reloaded.generate_chunks(keys)
    for key in keys:
        assert reloaded.load_chunk(key)["heights"] == project.load_chunk(key)["heights"]


def test_generation_is_consistent_across_processes_order_reopen_and_local_recompute(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root))
    first = tmp_path / "first"
    second = tmp_path / "second"
    common = [
        "--seed", "7",
        "--chunk-size", "10",
        "--resolution", "5",
        "--scale", "20",
        "--octaves", "3",
    ]

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "terrain_system.cli", *args],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    run("init", str(first), *common)
    run("generate", str(first), "0,0", "1,0")
    run("init", str(second), *common)
    run("generate", str(second), "1,0", "0,0")

    for name in ("p0_p0.json", "p1_p0.json"):
        assert (first / "chunks" / name).read_text() == (second / "chunks" / name).read_text()

    before = (first / "chunks" / "p0_p0.json").read_text()
    run("road", str(first), "--x1", "-2", "--y1", "0", "--x2", "12", "--y2", "0", "--width", "2", "--height", "5", "--shoulder", "2")
    edited = (first / "chunks" / "p0_p0.json").read_text()
    assert edited != before
    run("stitch", str(first))

    run("generate", str(first), "0,0")
    assert (first / "chunks" / "p0_p0.json").read_text() == edited
    run("stitch", str(first))
