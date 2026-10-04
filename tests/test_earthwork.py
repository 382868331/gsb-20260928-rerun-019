from pathlib import Path

import pytest

from terrain_system.construction import BuiltSpan, ConstructionSchedule
from terrain_system.earthwork import (
    EarthworkError,
    EarthworkLedger,
    EarthworkEvent,
    FillRequirement,
    Reservation,
    stable_dedup_key,
)
from terrain_system.edits import LinearWorkOp, TemporaryRampOp
from terrain_system.model import ChunkKey
from terrain_system.parameters import TerrainParameters
from terrain_system.project import create_project, load_project


def params():
    return TerrainParameters(
        seed=7,
        chunk_size=10.0,
        resolution=5,
        scale=20.0,
        octaves=3,
        persistence=0.5,
        lacunarity=2.0,
        base_height=0.0,
        amplitude=10.0,
    )


def _fill_road(project, height=6.0, work_id=1):
    work = LinearWorkOp(work_id, "road", -6.0, 0.0, 16.0, 0.0, 2.0, height, shoulder=1.0)
    project.add_edit(work)
    return work


def _cut_river(project, height=-6.0, work_id=2, priority=2):
    work = LinearWorkOp(work_id, "river", 0.0, -6.0, 0.0, 16.0, 2.0, height,
                        shoulder=1.0, priority=priority)
    project.add_edit(work)
    return work


def _project_with_chunk(tmp_path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0)])
    return project


def test_construction_records_actual_cut_and_fill_batches(tmp_path: Path):
    project = _project_with_chunk(tmp_path)
    fill_work = _fill_road(project, 6.0)
    cut_work = _cut_river(project, -6.0)

    fill_result = project.construct_work(fill_work.id, 0.0, 0.5, stage=1)
    cut_result = project.construct_work(cut_work.id, 0.0, 0.5, stage=2)

    assert fill_result["fill_volume"] > 0.0
    assert fill_result["earthwork"]["requirement"] == "F1"
    assert cut_result["cut_volume"] > 0.0
    cut_batch = cut_result["earthwork"]["cut_batch"]
    assert cut_batch == "C1"
    balances = project.ledger.balances(2)
    assert balances["batches"][cut_batch]["free"] == pytest.approx(cut_result["cut_volume"])
    requirements = project.ledger.requirements_view(2)
    assert {item["id"] for item in requirements} == {"F1"}


def test_reservation_holds_soil_and_two_targets_cannot_share_it(tmp_path: Path):
    ledger = EarthworkLedger()
    ledger.record_cut(10.0, 1, batch="C1")
    ledger.reserve("C1", "fill-a", 6.0, 2)
    with pytest.raises(EarthworkError):
        ledger.reserve("C1", "fill-b", 6.0, 2)
    _, remaining = ledger.reserve("C1", "fill-b", 4.0, 2)
    assert ledger.free_quantity("C1", 2) == pytest.approx(0.0)
    assert set(ledger.open_reservations(2)) == {"P1", "P2"}
    assert remaining.quantity == 4.0


def test_reservation_must_arrive_before_placement_and_cancel_releases_pending_only():
    ledger = EarthworkLedger()
    ledger.record_cut(10.0, 1, batch="C1")
    ledger.reserve("C1", "fill-a", 4.0, 2, reservation_id="P1")
    with pytest.raises(EarthworkError):
        ledger.place("P1", "fill-a", 4.0, 2)
    ledger.mark_arrived("P1", 2, quantity=4.0)
    with pytest.raises(EarthworkError):
        ledger.cancel_reservation("P1", 2, "arrived material cannot be un-stocked")
    ledger.place("P1", "fill-a", 4.0, 2)

    ledger.reserve("C1", "fill-b", 3.0, 3, reservation_id="P2")
    ledger.cancel_reservation("P2", 3, "transport cancelled before departure")
    assert ledger.free_quantity("C1", 3) == pytest.approx(6.0)
    assert "P2" not in ledger.open_reservations(3)


def test_design_revision_releases_future_reservations_but_not_arrived(tmp_path: Path):
    project = _project_with_chunk(tmp_path)
    cut_work = _cut_river(project, -8.0, work_id=1, priority=0)
    project.construct_work(cut_work.id, 0.0, 1.0, stage=1)
    cut_batch = "C1"
    _fill_road(project, 6.0, work_id=2)
    project.earth_reserve(cut_batch, "work2:future", 3.0, stage=2, work_id=2, start=0.5, end=1.0)
    project.earth_reserve(cut_batch, "work2:arrived", 2.0, stage=1, work_id=2, start=0.0, end=0.5)
    project.earth_arrive("P2", stage=1, quantity=2.0)

    revised = LinearWorkOp(3, "road", -6.0, 0.0, 16.0, 0.0, 2.0, 9.0, shoulder=1.0, revises=2)
    project.add_edit(revised)

    released = project.last_released_reservations
    assert [event.reservation for event in released] == ["P1"]
    assert "P2" in project.ledger.open_reservations(2)
    assert "replaced unbuilt design" in released[0].note


def test_explicit_supply_and_loss_never_auto_close_gap(tmp_path: Path):
    project = _project_with_chunk(tmp_path)
    fill_work = _fill_road(project)
    result = project.construct_work(fill_work.id, 0.0, 0.5, stage=1)
    requirement = project.ledger.requirements_view(1)[0]
    initial_gap = requirement["gap"]
    assert initial_gap == pytest.approx(result["fill_volume"])

    project.earth_supply(result["fill_volume"] / 2, stage=1, note="partial delivery only")
    project.earth_loss("S1", result["fill_volume"] / 4, stage=1)

    requirement = project.ledger.requirements_view(1)[0]
    assert requirement["gap"] == pytest.approx(initial_gap)
    assert requirement["sources"] == []


def test_overlapping_works_are_measured_from_real_terrain_once(tmp_path: Path):
    project = _project_with_chunk(tmp_path)
    road = _fill_road(project, 6.0, work_id=1)
    river = _cut_river(project, -6.0, work_id=2, priority=5)
    road_result = project.construct_work(road.id, 0.0, 1.0, stage=1)
    river_result = project.construct_work(river.id, 0.0, 1.0, stage=2)

    # The second excavation removes the actual road surface it intersects;
    # the accounting is a terrain difference, never road+river nominal sums.
    assert river_result["cut_volume"] > 0.0
    view = project.surface_at_stage(ChunkKey(0, 0), 2)
    assert len(view["earthwork"]["events"]) == len(project.ledger.events)
    assert view["heights"][2][0] == -6.0


def test_completion_strategy_compares_stockpile_gap_and_external_supply(tmp_path: Path):
    project = _project_with_chunk(tmp_path)
    cut_work = _cut_river(project, -10.0, work_id=1, priority=0)
    project.construct_work(cut_work.id, 0.0, 1.0, stage=1)
    fill_work = LinearWorkOp(2, "road", -6.0, 8.0, 16.0, 8.0, 2.0, 9.0, shoulder=1.0)
    project.add_edit(fill_work)

    comparison = project.compare_completion_strategies(2, [
        {"stage": 2, "start": 0.0, "end": 0.5},
        {"stage": 3, "start": 0.5, "end": 1.0},
    ])
    stockpile = comparison["use_existing_stockpile"]["rows"]
    assert all("gap" in row for row in stockpile)
    blocked = [row for row in stockpile if row["cannot_start_span"] is not None]
    assert comparison["external_supply"]["total_supply_required"] >= 0.0
    if blocked:
        assert blocked[0]["cannot_start_span"]["work_id"] == 2


def test_demolition_creates_new_recovered_batch_and_ramp_recovery_is_traceable(tmp_path: Path):
    project = _project_with_chunk(tmp_path)
    road = _fill_road(project, 6.0)
    built = project.construct_work(road.id, 0.0, 0.5, stage=1)
    target = next(iter(project.ledger.requirements))
    target = project.ledger.requirements[target].target
    project.earth_supply(built["fill_volume"], stage=1)
    project.earth_place(target, built["fill_volume"], stage=1, batch="S1", work_id=1, start=0.0, end=0.5)

    ramp = TemporaryRampOp(2, 4.0, 5.0, 10.0, 5.0, 2.0, 8.0, 1.0, 1, 1)
    project.add_temporary_ramp(ramp, stage=1)
    project.advance_stage()

    recovered = [event for event in project.ledger.events if event.kind == "recover"]
    assert recovered and recovered[0].batch.startswith("R")
    trace = project.ledger.trace_batch(recovered[0].batch)
    assert trace["root"]["kind"] == "recover"
    assert "temporary ramp" in trace["root"].get("note", "")

    demolition = project.demolish_work(1, 0.25, 0.5, stage=3)
    assert demolition["earthwork"]["recovery_batch"].startswith("R")
    trace = project.ledger.trace_target(target, 3)
    roots = {item["origin"]["root"]["kind"] for item in trace["chain"]}
    assert roots == {"supply"}


def test_reopen_preserves_ledger_balances_requirements_and_trace(tmp_path: Path):
    project = _project_with_chunk(tmp_path)
    cut_work = _cut_river(project, -8.0, work_id=1, priority=0)
    project.construct_work(cut_work.id, 0.0, 1.0, stage=1)
    _fill_road(project, 6.0, work_id=2)
    project.earth_reserve("C1", "work2:0-0.5@2", 2.0, stage=2, work_id=2, start=0.0, end=0.5)
    built = project.construct_work(2, 0.0, 0.5, stage=2)
    target = f"work2:0-0.5@2"
    project.earth_arrive("P1", stage=2, quantity=2.0)
    project.earth_place(target, 2.0, stage=2, reservation_id="P1")

    reloaded = load_project(tmp_path)
    assert reloaded.ledger.balances(2) == project.ledger.balances(2)
    trace = reloaded.earth_trace(target, 2)
    assert trace["quantity"] == pytest.approx(2.0)
    assert trace["chain"][0]["origin"]["root"]["kind"] == "cut"
    assert reloaded.ledger.requirements_view(2)[0]["gap"] == pytest.approx(
        project.ledger.requirements_view(2)[0]["gap"]
    )


def test_ledger_replay_and_dedup_are_load_order_independent():
    ledger = EarthworkLedger()
    key = stable_dedup_key(["cut", 1, 5.0])
    ledger.record_cut(5.0, 1, batch="C1", dedup_key=key)
    payload = ledger.to_dict()
    events = list(reversed(payload["events"]))
    reservations = payload["reservations"]
    requirements = payload["requirements"]
    reordered = EarthworkLedger.from_dict(
        {**payload, "events": events, "reservations": reservations, "requirements": requirements}
    )
    assert reordered.balances(1) == ledger.balances(1)
    with pytest.raises(EarthworkError):
        reordered.record_cut(5.0, 1, batch="C2", dedup_key=key)


def test_ledger_rejects_invalid_persistence_shapes():
    with pytest.raises(Exception):
        EarthworkLedger.from_dict({"version": 99, "events": []})
    bad = EarthworkLedger()
    bad.events.append(EarthworkEvent(1, "cut", 1, 1.0, batch="C1"))
    bad.events.append(EarthworkEvent(1, "reserve", 1, 1.0, batch="C1", reservation="P1"))
    with pytest.raises(EarthworkError):
        bad._reindex()


def test_balances_and_volumes_are_stable_across_reopen_and_regeneration_order(tmp_path: Path):
    project = _project_with_chunk(tmp_path)
    river = _cut_river(project, -9.0, work_id=1, priority=0)
    result = project.construct_work(river.id, 0.0, 1.0, stage=1)
    expected_balances = project.ledger.balances(1)
    expected_chunk = project.load_chunk(ChunkKey(0, 0))["heights"]

    reloaded = load_project(tmp_path)
    reloaded.generate_chunks([ChunkKey(0, 0)])
    reloaded.generate_chunks([ChunkKey(1, 0)])
    again = load_project(tmp_path)

    assert again.ledger.balances(1) == expected_balances
    assert again.load_chunk(ChunkKey(0, 0))["heights"] == expected_chunk
    view = again.surface_at_stage(ChunkKey(0, 0), 1)
    assert view["earthwork"]["balances"] == expected_balances
    assert view["earthwork"]["requirements"] == []
    assert result["cut_volume"] == pytest.approx(
        expected_balances["totals"]["produced"]
    )
