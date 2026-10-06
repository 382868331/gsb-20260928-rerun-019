from pathlib import Path

import pytest

from terrain_system.earthwork import EarthworkLedger, LedgerError
from terrain_system.model import ChunkKey
from terrain_system.project import create_project, load_project

from terrain_system.edits import LinearWorkOp

from test_terrain import _road, params


def _vertical_road(work_id: int, height: float = 6.0):
    return LinearWorkOp(work_id, "road", 0.0, -6.0, 0.0, 16.0, 2.0, height, shoulder=1.0)


def _built_project(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0), ChunkKey(-1, 0)])
    project.add_edit(_road(1, height=6.0))
    project.construct_work(1, 0.0, 1.0, stage=1)
    return project


def test_construct_registers_excavation_batch_from_actual_terrain(tmp_path: Path):
    project = _built_project(tmp_path)
    balances = project.stage_balances()
    assert len(balances) == 1
    assert balances[0]["fill"] > 0.0

    excavation = [batch for batch in project.ledger.batches if batch.source == "excavation"]
    reloaded = load_project(tmp_path)
    assert [batch.to_dict() for batch in reloaded.ledger.batches] == [
        batch.to_dict() for batch in excavation
    ]


def test_demolish_creates_recycled_source_and_survives_reopen(tmp_path: Path):
    project = _built_project(tmp_path)
    result = project.demolish_work(1, 0.0, 0.5)
    batch = result["recovered_batch"]
    assert batch is not None
    assert batch.source == "demolition"
    assert batch.volume > 0.0

    reloaded = load_project(tmp_path)
    recovered = [item for item in reloaded.ledger.batches if item.source == "demolition"]
    assert len(recovered) == 1
    assert recovered[0].volume == pytest.approx(batch.volume)


def test_batch_lifecycle_and_cancel_only_releases_pending_reservation(tmp_path: Path):
    ledger = EarthworkLedger()
    first = ledger.add_source("stockpile", 10.0, 0)
    second = ledger.add_source("external", 5.0, 1)

    ledger.reserve(first.id)
    ledger.cancel(first.id)
    assert ledger.batch(first.id).state == "available"

    ledger.reserve(first.id)
    ledger.deliver(first.id)
    with pytest.raises(LedgerError):
        ledger.cancel(first.id)

    ledger.reserve(second.id)
    ledger.record_loss(second.id, 2.0)
    assert ledger.batch(second.id).lost_volume == pytest.approx(2.0)

    placement = ledger.place(stage=1, volume=4.0)
    assert [item["batch_id"] for item in placement.allocations] == [first.id]
    assert ledger.batch(first.id).remaining == pytest.approx(6.0)

    with pytest.raises(LedgerError):
        ledger.place(stage=1, volume=100.0)


def test_material_cannot_be_reused_and_trace_survives_reopen(tmp_path: Path):
    project = _built_project(tmp_path)
    batch = project.ledger.add_source("external", 8.0, 1)
    project.ledger.reserve(batch.id)
    project.ledger.deliver(batch.id)
    first = project.ledger.place(stage=1, volume=5.0)
    second = project.ledger.place(stage=1, volume=3.0)
    assert project.ledger.batch(batch.id).state == "placed"
    with pytest.raises(LedgerError):
        project.ledger.place(stage=1, volume=0.5, batch_ids=[batch.id])
    project.save_manifest()

    reloaded = load_project(tmp_path)
    traced = reloaded.ledger.trace(first.id)
    assert traced["sources"] == [
        {"batch_id": batch.id, "source": "external", "stage": 1, "volume": 5.0}
    ]
    assert reloaded.ledger.trace(second.id)["volume"] == pytest.approx(3.0)


def test_compare_reports_per_stage_gaps_without_balancing(tmp_path: Path):
    project = _built_project(tmp_path)
    fill = project.stage_balances()[0]["fill"]
    project.ledger.add_source("stockpile", fill / 2.0, 1)
    project.ledger.add_source("external", fill, 1)
    project.save_manifest()

    reloaded = load_project(tmp_path)
    comparison = reloaded.compare_supply_scenarios()
    reuse = comparison["scenarios"]["reuse_stockpile"][0]
    external = comparison["scenarios"]["external_only"][0]
    assert reuse["gap"] == pytest.approx(fill / 2.0)
    assert external["gap"] == pytest.approx(0.0)
    assert len(reloaded.ledger.batches) == 2


def test_junction_volumes_come_from_actual_terrain_not_nominal_sum(tmp_path: Path):
    project = create_project(tmp_path, params())
    project.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0), ChunkKey(-1, 0), ChunkKey(0, 1)])
    project.add_edit(_road(1, height=6.0))
    crossing = _vertical_road(2)
    project.add_edit(crossing)
    project.construct_work(1, 0.0, 1.0, stage=1)
    project.construct_work(2, 0.0, 1.0, stage=2)

    balances = project.stage_balances()
    stage_two_fill = balances[1]["fill"]
    assert stage_two_fill > 0.0
    # Actual-terrain accounting: building the crossing second adds only the
    # remaining delta, so it must be less than building it on untouched ground.
    solo = create_project(tmp_path / "solo", params())
    solo.generate_chunks([ChunkKey(0, 0), ChunkKey(1, 0), ChunkKey(-1, 0), ChunkKey(0, 1)])
    solo.add_edit(_vertical_road(2))
    solo.construct_work(2, 0.0, 1.0, stage=1)
    solo_fill = solo.stage_balances()[0]["fill"]
    assert stage_two_fill < solo_fill
