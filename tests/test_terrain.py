from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from terrain_system.cli import main
from terrain_system.edits import EditOp, RoadEditOp, next_edit_id
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


def test_road_intersections_order_and_undo_are_deterministic(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    first = RoadEditOp(1, -2.0, 0.0, 12.0, 0.0, 2.0, 4.0)
    second = RoadEditOp(2, 0.0, -2.0, 0.0, 12.0, 2.0, 7.0)
    project.add_edit(first)
    project.add_edit(second)

    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 7.0
    assert project.undo_edit(1) == [ChunkKey(0, 0)]
    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 7.0

    reloaded = load_project(tmp_path)
    assert reloaded.undo_edit(2) == [ChunkKey(0, 0)]
    final = reloaded.load_chunk(ChunkKey(0, 0))
    assert final["heights"] == final["base_heights"]


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
