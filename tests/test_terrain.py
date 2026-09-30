from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from terrain_system.cli import main
from terrain_system.edits import EditOp, next_edit_id
from terrain_system.model import ChunkKey, generate_grid
from terrain_system.parameters import ParameterError, TerrainParameters
from terrain_system.project import create_project, load_project

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def run_cli(args: list[str], cwd: Path) -> dict:
    """Run the CLI in a separate Python process; return the last JSON payload."""
    completed = subprocess.run(
        [sys.executable, "-m", "terrain_system.cli", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    output_lines = completed.stdout.splitlines()
    start = len(output_lines) - 1 - output_lines[::-1].index("{")
    return json.loads("\n".join(output_lines[start:]))


def init_project(path: Path) -> None:
    run_cli(["init", str(path), "--seed", "12345", "--chunk-size", "10",
            "--resolution", "6", "--scale", "24", "--octaves", "3",
            "--persistence", "0.5", "--lacunarity", "2.0",
            "--base-height", "0", "--amplitude", "8"], cwd=path.parent)


def dump_chunk(project: Path, chunk: str, cwd: Path) -> dict:
    return run_cli(["dump", str(project), chunk], cwd=cwd)


def test_generation_is_order_independent_in_process():
    p = params()
    first = generate_grid(p, ChunkKey(1, 0), [])
    second = generate_grid(p, ChunkKey(0, 0), [])
    later = generate_grid(p, ChunkKey(1, 0), [])

    assert first == later
    for row in range(p.resolution):
        assert second["heights"][row][-1] == first["heights"][row][0]
    for column in range(p.resolution):
        assert second["heights"][-1][column] == generate_grid(p, ChunkKey(0, 1), [])["heights"][0][column]


def test_generation_order_and_project_reopen_match_across_processes(tmp_path: Path):
    project_a = tmp_path / "order_a"
    project_b = tmp_path / "order_b"
    init_project(project_a)
    init_project(project_b)
    cwd = tmp_path

    run_cli(["generate", str(project_a), "0,0", "1,0", "1,1", "0,1"], cwd)
    run_cli(["generate", str(project_b), "0,1", "1,1", "1,0"], cwd)
    run_cli(["generate", str(project_b), "0,0"], cwd)

    for chunk in ("0,0", "1,0", "0,1", "1,1"):
        grid_a = dump_chunk(project_a, chunk, cwd)
        grid_b = dump_chunk(project_b, chunk, cwd)
        assert grid_a["heights"] == grid_b["heights"]
        assert grid_a["base_heights"] == grid_b["base_heights"]

    seam_a = run_cli(["stitch", str(project_a)], cwd)
    seam_b = run_cli(["stitch", str(project_b)], cwd)
    assert seam_a["status"] == "ok"
    assert seam_b["status"] == "ok"


def test_road_crossing_boundary_is_continuous_and_survives_processes(tmp_path: Path):
    project = tmp_path / "road"
    init_project(project)
    cwd = tmp_path
    run_cli(["generate", str(project), "0,0", "1,0"], cwd)
    run_cli(["road", str(project), "--x1", "-2", "--y1", "4",
             "--x2", "22", "--y2", "4", "--half-width", "2.5",
             "--shoulder", "1.5"], cwd)
    assert run_cli(["stitch", str(project)], cwd)["status"] == "ok"

    left_grid = dump_chunk(project, "0,0", cwd)
    right_grid = dump_chunk(project, "1,0", cwd)
    left = left_grid["heights"]
    right = right_grid["heights"]
    resolution = len(left)
    for row in range(resolution):
        assert left[row][-1] == right[row][0]

    center_row = next(row for row in range(resolution) if abs(row * 10.0 / (resolution - 1) - 4.0) < 1e-9)
    for column in range(resolution):
        assert left[center_row][column] == pytest.approx(left_grid["base_heights"][center_row][column], abs=1e-9)

    reloaded = load_project(project)
    reloaded.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0)])
    regenerated = dump_chunk(project, "1,0", cwd)["heights"]
    assert regenerated == right


def test_road_intersections_have_order_independent_meaning_across_processes(tmp_path: Path):
    def build(name: Path, reverse: bool) -> None:
        init_project(name)
        run_cli(["generate", str(name), "0,0", "1,0"], tmp_path)
        first = ["road", str(name), "--x1", "-2", "--y1", "4", "--x2", "22",
                 "--y2", "4", "--half-width", "2", "--shoulder", "1"]
        second = ["road", str(name), "--x1", "5", "--y1", "-2", "--x2", "5",
                  "--y2", "22", "--half-width", "1.5", "--shoulder", "1"]
        run_cli(second if reverse else first, tmp_path)
        run_cli(first if reverse else second, tmp_path)

    project_a = tmp_path / "roads_a"
    project_b = tmp_path / "roads_b"
    build(project_a, False)
    build(project_b, True)

    for chunk in ("0,0", "1,0"):
        assert dump_chunk(project_a, chunk, tmp_path)["heights"] == dump_chunk(project_b, chunk, tmp_path)["heights"]


def test_undo_one_edit_recomputes_identically_in_new_process(tmp_path: Path):
    undone = tmp_path / "undo"
    reference = tmp_path / "reference"
    init_project(undone)
    init_project(reference)
    run_cli(["generate", str(undone), "0,0", "1,0"], tmp_path)
    run_cli(["generate", str(reference), "0,0", "1,0"], tmp_path)

    run_cli(["road", str(undone), "--x1", "-2", "--y1", "5", "--x2", "22",
             "--y2", "5", "--half-width", "2", "--shoulder", "2"], tmp_path)
    run_cli(["edit", str(undone), "--x", "3", "--y", "3", "--radius", "3", "--delta", "2"], tmp_path)
    run_cli(["edit", str(reference), "--x", "3", "--y", "3", "--radius", "3", "--delta", "2"], tmp_path)

    payload = run_cli(["undo", str(undone), "--id", "1"], tmp_path)
    assert payload["recomputed_chunks"] == [[0, 0], [1, 0]]
    assert run_cli(["stitch", str(undone)], tmp_path)["status"] == "ok"

    for chunk in ("0,0", "1,0"):
        assert dump_chunk(undone, chunk, tmp_path)["heights"] == dump_chunk(reference, chunk, tmp_path)["heights"]

    manifest = json.loads((undone / "project.json").read_text(encoding="utf-8"))
    assert [edit["id"] for edit in manifest["edits"]] == [2]


def test_manual_edits_persist_through_local_regeneration_in_new_process(tmp_path: Path):
    project = tmp_path / "edits"
    init_project(project)
    run_cli(["generate", str(project), "0,0"], tmp_path)
    run_cli(["edit", str(project), "--x", "0", "--y", "0", "--radius", "6", "--delta", "4"], tmp_path)
    before = dump_chunk(project, "0,0", tmp_path)

    run_cli(["generate", str(project), "1,0"], tmp_path)
    run_cli(["generate", str(project), "0,0"], tmp_path)
    after = dump_chunk(project, "0,0", tmp_path)
    assert after["heights"] == before["heights"]
    assert after["heights"][0][0] == pytest.approx(after["base_heights"][0][0] + 4.0)


def test_project_chunks_reload_and_stitch(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0), ChunkKey(0, 1), ChunkKey(1, 1)])
    reloaded = load_project(tmp_path)
    assert reloaded.check_loaded_seams() == []


def test_edit_survives_regeneration_and_updates_existing_chunks(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0)])
    before = project.load_chunk(ChunkKey(0, 0))["heights"][0][0]

    edit = EditOp.raise_op(id=next_edit_id(project.edits), x=0.0, y=0.0, radius=6.0, delta=4.0)
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


def test_readme_commands_run_end_to_end(tmp_path: Path):
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "+" not in readme.split("## 使用", 1)[1].split("~~~", 2)[1]
    block = re.search(r"~~~bash\n(.*?)~~~", readme, re.S).group(1)
    commands = [line.rstrip("\\").strip() for line in block.replace("\\\n", " ").splitlines() if line.strip()]
    env = {"PYTHONPATH": str(REPO_ROOT), "PATH": __import__("os").environ["PATH"]}
    for command in commands:
        completed = subprocess.run(command, shell=True, cwd=str(tmp_path), capture_output=True, text=True, env=env)
        assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "terrain_demo" / "project.json").is_file()
