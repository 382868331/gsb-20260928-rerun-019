from pathlib import Path

import pytest

from terrain_system.cli import main
from terrain_system.edits import EditError, LinearWorkOp, TransitionOp, next_edit_id
from terrain_system.model import ChunkKey, base_height_at
from terrain_system.project import create_project, load_project

from test_terrain import params


def make_road(project, y=5.0, height=4.0, priority=0):
    road = LinearWorkOp(
        id=next_edit_id(project.edits),
        kind="road",
        x1=0.0,
        y1=y,
        x2=10.0,
        y2=y,
        width=2.0,
        height=height,
        shoulder=0.0,
        priority=priority,
    )
    project.add_edit(road)
    return road


def test_segmented_view_shows_built_part_and_original_terrain(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    road = make_road(project)
    project.add_stage("first half")
    project.add_stage("second half")
    project.add_schedule_entry(road.id, 1, 0.0, 0.5)
    project.add_schedule_entry(road.id, 2, 0.5, 1.0)

    view1 = project.view_at_stage(1, ChunkKey(0, 0))
    grid1 = view1["grid"]
    # point at x=2.5 (t=0.25) is built; x=7.5 (t=0.75) is still original terrain
    assert grid1["heights"][2][1] == pytest.approx(4.0)
    assert grid1["heights"][2][3] == pytest.approx(base_height_at(project.parameters, 7.5, 5.0))
    assert view1["counts"]["completed"] > 0
    assert view1["counts"]["unbuilt"] > 0

    view2 = project.view_at_stage(2, ChunkKey(0, 0))
    assert view2["grid"]["heights"][2][3] == pytest.approx(4.0)

    # final stored chunk has the fully built road
    final = project.load_chunk(ChunkKey(0, 0))
    assert final["heights"][2][1] == pytest.approx(4.0)
    assert final["heights"][2][3] == pytest.approx(4.0)


def test_transition_lifecycle_and_final_terrain(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    project.add_stage("enable")
    project.add_stage("remove")
    transition = TransitionOp(
        id=next_edit_id(project.edits),
        x1=0.0, y1=5.0, x2=10.0, y2=5.0,
        width=2.0, height=9.0, built_stage=1, demolish_stage=2,
    )
    project.add_edit(transition)

    view1 = project.view_at_stage(1, ChunkKey(0, 0))
    assert view1["grid"]["heights"][2][2] == pytest.approx(9.0)
    assert view1["active_transitions"] == [transition.id]
    assert view1["counts"]["temporary"] > 0

    view2 = project.view_at_stage(2, ChunkKey(0, 0))
    assert view2["active_transitions"] == []
    assert view2["grid"]["heights"][2][2] == pytest.approx(
        base_height_at(project.parameters, 5.0, 5.0)
    )

    # demolished transition no longer affects the final stored terrain
    final = project.load_chunk(ChunkKey(0, 0))
    assert final["heights"][2][2] == pytest.approx(base_height_at(project.parameters, 5.0, 5.0))


def test_revise_keeps_built_part_and_reports_connection_delta(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    road = make_road(project, height=4.0)
    project.add_stage("first half")
    project.add_schedule_entry(road.id, 1, 0.0, 0.5)

    result = project.revise_work(
        road.id, x1=5.0, y1=5.0, x2=10.0, y2=5.0, width=2.0, height=7.0,
    )
    assert result["kept_work"].id == road.id
    assert result["suggest_transition"] is True
    front = [item for item in result["connections"] if item["point"] == [5.0, 5.0]]
    assert front and front[0]["existing_height"] == pytest.approx(4.0)
    assert front[0]["new_height"] == pytest.approx(7.0)
    assert front[0]["delta"] == pytest.approx(3.0)

    # built part of the old design is untouched at stage 1
    view1 = project.view_at_stage(1, ChunkKey(0, 0))
    assert view1["grid"]["heights"][2][1] == pytest.approx(4.0)
    # new design is a separate unscheduled edit applied to the final terrain
    final = project.load_chunk(ChunkKey(0, 0))
    assert final["heights"][2][3] == pytest.approx(7.0)

    # a temporary transition can be added across the connection
    project.add_stage("transition")
    transition = TransitionOp(
        id=next_edit_id(project.edits),
        x1=4.0, y1=5.0, x2=6.0, y2=5.0,
        width=2.0, height=5.5, priority=10, built_stage=2, demolish_stage=None,
    )
    project.add_edit(transition)
    view2 = project.view_at_stage(2, ChunkKey(0, 0))
    assert view2["grid"]["heights"][2][2] == pytest.approx(5.5)


def test_undo_rules_for_built_and_unbuilt_works(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    built = make_road(project, y=2.0, height=3.0)
    unbuilt = make_road(project, y=8.0, height=6.0)
    project.add_stage("stage one")
    project.add_schedule_entry(built.id, 1, 0.0, 1.0)

    with pytest.raises(EditError, match="demolition"):
        project.undo_edit(built.id)

    # undoing the unbuilt revision leaves the completed terrain untouched
    before = project.load_chunk(ChunkKey(0, 0))["heights"][1][1]
    project.undo_edit(unbuilt.id)
    after = project.load_chunk(ChunkKey(0, 0))["heights"][1][1]
    assert after == pytest.approx(before)

    # demolishing the built part is a separate construction decision
    project.add_stage("stage two")
    project.add_demolition(built.id, 2)
    view1 = project.view_at_stage(1, ChunkKey(0, 0))
    assert view1["grid"]["heights"][1][1] == pytest.approx(3.0)
    view2 = project.view_at_stage(2, ChunkKey(0, 0))
    assert view2["demolished_works"] == [built.id]
    assert view2["grid"]["heights"][1][1] == pytest.approx(
        base_height_at(project.parameters, 2.5, 2.5)
    )


def test_reopen_and_generation_order_keep_stage_view_consistent(tmp_path: Path):
    project = create_project(tmp_path, params())
    road = make_road(project)
    project.add_stage("half")
    project.add_schedule_entry(road.id, 1, 0.0, 0.5)
    project.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0)])
    first_view = project.view_at_stage(1, ChunkKey(0, 0))["grid"]
    first_chunk = project.load_chunk(ChunkKey(0, 0))

    reloaded = load_project(tmp_path)
    reloaded.generate_chunks([ChunkKey(1, 0), ChunkKey(0, 0)])
    second_view = reloaded.view_at_stage(1, ChunkKey(0, 0))["grid"]
    second_chunk = reloaded.load_chunk(ChunkKey(0, 0))

    assert second_view == first_view
    assert second_chunk == first_chunk
    plan = reloaded.plan_summary()
    assert plan["stages"][0]["permanent"] == [
        {"work_id": road.id, "kind": "road", "portion": [0.0, 0.5]}
    ]


def test_junction_priority_decides_across_stages(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    road = LinearWorkOp(
        id=1, kind="road", x1=0.0, y1=5.0, x2=10.0, y2=5.0,
        width=2.0, height=6.0, priority=10,
    )
    river = LinearWorkOp(
        id=2, kind="river", x1=5.0, y1=0.0, x2=5.0, y2=10.0,
        width=2.0, height=-2.0, priority=5,
    )
    project.add_edit(road)
    project.add_edit(river)
    project.add_stage("road first")
    project.add_stage("river second")
    project.add_schedule_entry(road.id, 1)
    project.add_schedule_entry(river.id, 2)

    # only the road is built at stage 1: the crossing follows the road
    view1 = project.view_at_stage(1, ChunkKey(0, 0))
    assert view1["grid"]["heights"][2][2] == pytest.approx(6.0)
    # at stage 2 both exist and the explicit priority keeps the road design
    view2 = project.view_at_stage(2, ChunkKey(0, 0))
    assert view2["grid"]["heights"][2][2] == pytest.approx(6.0)


def test_plan_separates_permanent_and_temporary(tmp_path: Path):
    project = create_project(tmp_path, params())
    road = make_road(project)
    project.add_stage("build")
    project.add_stage("cleanup")
    project.add_schedule_entry(road.id, 1)
    transition = TransitionOp(
        id=next_edit_id(project.edits),
        x1=0.0, y1=1.0, x2=10.0, y2=1.0,
        width=2.0, height=1.0, built_stage=1, demolish_stage=2,
    )
    project.add_edit(transition)

    plan = project.plan_summary()
    assert plan["stages"][0]["permanent"][0]["work_id"] == road.id
    assert plan["stages"][0]["transitions_built"] == [transition.id]
    assert plan["stages"][1]["transitions_demolished"] == [transition.id]
    assert plan["unscheduled_works"] == []


def test_cli_stage_workflow(tmp_path: Path, capsys):
    project_path = tmp_path / "demo"
    assert main(["init", str(project_path), "--seed", "7", "--chunk-size", "10",
                 "--resolution", "5", "--scale", "20"]) == 0
    assert main(["generate", str(project_path), "0,0"]) == 0
    assert main(["road", str(project_path), "--x1", "0", "--y1", "5",
                 "--x2", "10", "--y2", "5", "--width", "2", "--height", "4"]) == 0
    assert main(["stage", str(project_path), "--name", "half"]) == 0
    assert main(["schedule", str(project_path), "--work", "1", "--stage", "1",
                 "--start", "0", "--end", "0.5"]) == 0
    assert main(["view", str(project_path), "--stage", "1", "--chunk", "0,0"]) == 0
    assert main(["plan", str(project_path)]) == 0
    assert main(["revise", str(project_path), "--work", "1", "--x1", "5", "--y1", "5",
                 "--x2", "10", "--y2", "5", "--width", "2", "--height", "7"]) == 0
    assert main(["undo", str(project_path), "--id", "1"]) == 1

    output = capsys.readouterr().out
    assert '"suggest_transition": true' in output
    assert "demolition" in output
