from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from terrain_system.cli import main
from terrain_system.edits import (
    EditOp,
    JunctionAdjustmentOp,
    LinearWorkOp,
    TemporaryRampOp,
    apply_edits,
    next_edit_id,
)
from terrain_system.model import ChunkKey, generate_grid
from terrain_system.parameters import ParameterError, TerrainParameters
from terrain_system.project import create_project, load_project


def _road(work_id: int, height: float = 4.0, revises=None):
    return LinearWorkOp(
        work_id,
        "road",
        -6.0,
        0.0,
        16.0,
        0.0,
        2.0,
        height,
        shoulder=1.0,
        revises=revises,
    )


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

    road = LinearWorkOp(
        id=1,
        kind="road",
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


def test_road_intersections_priority_and_undo_are_deterministic(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    first = LinearWorkOp(1, "road", -2.0, 0.0, 12.0, 0.0, 2.0, 4.0, priority=0)
    second = LinearWorkOp(2, "road", 0.0, -2.0, 0.0, 12.0, 2.0, 7.0, priority=1)
    project.add_edit(first)
    project.add_edit(second)

    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 7.0
    result = project.undo_edit(1)
    assert result["updated_chunks"] == [ChunkKey(0, 0)]
    assert result["voided_edits"] == []
    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] == 7.0

    reloaded = load_project(tmp_path)
    assert reloaded.undo_edit(2)["updated_chunks"] == [ChunkKey(0, 0)]
    final = reloaded.load_chunk(ChunkKey(0, 0))
    assert final["heights"] == final["base_heights"]


def test_priority_not_last_edit_decides_overlap_and_order_is_stable(tmp_path: Path):
    p = params()
    road = LinearWorkOp(2, "road", -2.0, 0.0, 12.0, 0.0, 2.0, 4.0, priority=1)
    river = LinearWorkOp(1, "river", 0.0, -2.0, 0.0, 12.0, 2.0, 7.0, priority=0)

    heights_a = generate_grid(p, ChunkKey(0, 0), [road, river])["heights"]
    heights_b = generate_grid(p, ChunkKey(0, 0), [river, road])["heights"]
    assert heights_a == heights_b
    assert heights_a[0][0] == 4.0
    assert apply_edits(0.0, 0.0, 0.0, [road, river]) == 4.0


def test_junction_revision_overrides_only_small_overlap_zone(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    road = LinearWorkOp(1, "road", -6.0, 0.0, 16.0, 0.0, 2.0, 4.0)
    river = LinearWorkOp(2, "river", 0.0, -6.0, 0.0, 16.0, 2.0, 7.0, priority=1)
    project.add_edit(road)
    project.add_edit(river)
    junction = JunctionAdjustmentOp(3, 1, 2, 0.0, 0.0, 1.5, 5.5)
    affected = project.add_edit(junction)
    assert affected == [ChunkKey(0, 0)]

    chunk = project.load_chunk(ChunkKey(0, 0))
    assert chunk["heights"][0][0] == 5.5
    assert chunk["heights"][0][-1] == 4.0
    assert chunk["heights"][-1][0] == 7.0

    result = project.undo_edit(3)
    assert result["voided_edits"] == []
    reverted = project.load_chunk(ChunkKey(0, 0))
    assert reverted["heights"][0][0] == 7.0
    assert reverted["heights"][0][-1] == 4.0


def test_removing_work_voids_junction_and_keeps_other_river(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    road = LinearWorkOp(1, "road", -6.0, 0.0, 16.0, 0.0, 2.0, 4.0)
    river = LinearWorkOp(2, "river", 0.0, -6.0, 0.0, 16.0, 2.0, 7.0)
    junction = JunctionAdjustmentOp(3, 1, 2, 0.0, 0.0, 1.5, 5.5)
    project.add_edit(road)
    project.add_edit(river)
    project.add_edit(junction)

    result = project.undo_edit(1)
    assert [item["id"] for item in result["voided_edits"]] == [3]
    assert all("road 1" in item["reason"] for item in result["voided_edits"])

    chunk = project.load_chunk(ChunkKey(0, 0))
    assert chunk["heights"][-1][0] == 7.0
    assert project.load_chunk(ChunkKey(0, 0))["heights"][0][0] != 5.5

    reloaded = load_project(tmp_path)
    assert [edit.id for edit in reloaded.edits] == [2]
    assert reloaded.voided_edits[-1]["id"] == 3


def test_preview_reports_affected_chunks_and_intersection_retention(tmp_path: Path, capsys):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(-1, -1), ChunkKey(0, 0), ChunkKey(1, 0)])
    road = LinearWorkOp(
        next_edit_id(project.edits), "road", -12.0, 0.0, 12.0, 0.0, 2.0, 4.0, priority=1
    )
    project.add_edit(road)

    assert main([
        "preview", str(tmp_path),
        "--kind", "river", "--x1", "0", "--y1", "-12",
        "--x2", "0", "--y2", "12", "--width", "2", "--height", "7",
        "--priority", "0",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "preview"
    assert payload["affected_chunks"] == [[-1, -1], [0, 0]]
    assert payload["intersections"] == [{
        "other_work": 1,
        "other_kind": "road",
        "point": [0.0, 0.0],
        "retained_work": 1,
        "overridden_work": 2,
    }]
    assert [edit.id for edit in load_project(tmp_path).edits] == [1]


def test_negative_coordinate_determinism_across_order_and_reopen(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(-1, -1), ChunkKey(0, 0)])
    road = LinearWorkOp(1, "road", -12.0, -2.0, 8.0, -2.0, 2.0, 4.0, shoulder=2.0)
    river = LinearWorkOp(2, "river", -2.0, -12.0, -2.0, 8.0, 2.0, 2.0, shoulder=2.0, priority=1)
    project.add_edit(road)
    project.add_edit(river)

    direct = generate_grid(project.parameters, ChunkKey(-1, -1), list(reversed(project.edits)))
    stored = project.load_chunk(ChunkKey(-1, -1))
    assert direct["heights"] == stored["heights"]
    assert project.check_loaded_seams() == []

    reloaded = load_project(tmp_path)
    reloaded.generate_chunks([ChunkKey(0, 0), ChunkKey(-1, -1)])
    assert reloaded.check_loaded_seams() == []
    assert reloaded.load_chunk(ChunkKey(-1, -1))["heights"] == stored["heights"]


def test_cli_junction_and_river_undo_report(tmp_path: Path, capsys):
    assert main(["init", str(tmp_path), "--seed", "7", "--chunk-size", "10", "--resolution", "5", "--scale", "20"]) == 0
    assert main(["generate", str(tmp_path), "0,0"]) == 0
    assert main(["road", str(tmp_path), "--x1", "-6", "--y1", "0", "--x2", "16", "--y2", "0", "--width", "2", "--height", "4"]) == 0
    assert main(["river", str(tmp_path), "--x1", "0", "--y1", "-6", "--x2", "0", "--y2", "16", "--width", "2", "--height", "7", "--priority", "1"]) == 0
    assert main(["junction", str(tmp_path), "--work-a", "1", "--work-b", "2", "--x", "0", "--y", "0", "--radius", "1.5", "--height", "5.5"]) == 0
    assert main(["undo", str(tmp_path), "--id", "1"]) == 0

    output_lines = capsys.readouterr().out.splitlines()
    last_object = len(output_lines) - 1 - output_lines[::-1].index("{")
    payload = json.loads("\n".join(output_lines[last_object:]))
    assert payload["kind"] == "road"
    assert payload["voided_edits"] == [{
        "id": 3,
        "reason": "referenced road 1 was removed",
        "work_a": 1,
        "work_b": 2,
    }]


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


def test_partial_construction_revision_keeps_completed_terrain(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    project.add_edit(_road(1, 4.0))
    project.construct_work(1, 0.0, 0.5)

    revised = _road(2, 8.0, revises=1)
    project.add_edit(revised)
    preview = project.design_connection_preview(revised, t=0.5)
    assert preview["height_difference"] == pytest.approx(4.0)

    result = project.undo_edit(2)
    assert result["removed"].id == 2
    still_built = project.load_chunk(ChunkKey(0, 0))
    assert still_built["heights"] == project.surface_at_stage(ChunkKey(0, 0), 1)["heights"]


def test_completed_work_requires_demolition_and_demolition_is_new_stage(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    project.add_edit(_road(1, 4.0))
    project.construct_work(1, 0.0, 1.0)

    with pytest.raises(Exception):
        project.undo_edit(1)

    built = project.surface_at_stage(ChunkKey(0, 0), 1)
    project.demolish_work(1, 0.0, 1.0)
    after = project.surface_at_stage(ChunkKey(0, 0), 2)
    assert after["stage"] == 2
    assert after["heights"][0][0] == built["base_heights"][0][0]


def test_temporary_ramp_is_separate_and_expires_after_stage(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    project.add_edit(_road(1, 4.0))
    project.construct_work(1, 0.0, 0.5)
    ramp = TemporaryRampOp(2, 4.0, 5.0, 10.0, 5.0, 2.0, 7.0, 1.0, 1, 1)
    project.add_temporary_ramp(ramp, stage=1)

    active = project.surface_at_stage(ChunkKey(0, 0), 1)
    assert active["heights"][2][-1] == 7.0
    assert active["temporary_deltas"][2][-1] != 0.0
    assert active["permanent_heights"][2][-1] != 7.0

    expired = project.surface_at_stage(ChunkKey(0, 0), 2)
    assert expired["heights"] == expired["permanent_heights"]
    assert all(all(value == 0.0 for value in row) for row in expired["temporary_deltas"])


def test_construction_records_survive_reopen_and_chunk_order(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(1, 0), ChunkKey(0, 0)])
    project.add_edit(_road(1, 4.0))
    project.construct_work(1, 0.0, 0.5)
    ramp = TemporaryRampOp(2, 4.0, 5.0, 10.0, 5.0, 2.0, 7.0, 1.0, 1, 1)
    project.add_temporary_ramp(ramp, stage=1)
    expected = project.surface_at_stage(ChunkKey(0, 0), 1)

    reloaded = load_project(tmp_path)
    reloaded.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0)])
    actual = reloaded.surface_at_stage(ChunkKey(0, 0), 1)
    assert actual["heights"] == expected["heights"]
    assert reloaded.schedule.records[0].to_dict() == {
        "work_id": 1,
        "start": 0.0,
        "end": 0.5,
        "stage": 1,
        "action": "build",
    }


def test_cross_stage_junction_has_explicit_height_basis(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    project.add_edit(LinearWorkOp(1, "road", -6.0, 0.0, 16.0, 0.0, 2.0, 4.0))
    project.add_edit(LinearWorkOp(2, "river", 0.0, -6.0, 0.0, 16.0, 2.0, 7.0))
    junction = JunctionAdjustmentOp(3, 1, 2, 0.0, 0.0, 1.5, 5.5)
    project.add_edit(junction)
    project.construct_work(1, 0.0, 1.0, stage=1)
    project.construct_work(2, 0.0, 1.0, stage=2)
    project.construct_junction(3, stage=2)

    before = project.surface_at_stage(ChunkKey(0, 0), 1)
    assert before["heights"][0][0] == 4.0
    basis = project.junction_height_basis(3, 0.0, 0.0)
    assert basis["height_basis"] == 3
    assert basis["active_works"] == [1, 2]
    assert basis["permanent_height"] == 5.5
