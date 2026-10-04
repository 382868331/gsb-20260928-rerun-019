from __future__ import annotations

import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .construction import BuiltSpan, ConstructionSchedule, construction_basis, permanent_height
from .earthwork import EarthworkLedger, stable_dedup_key
from .earthvolumes import integrate_ramp, span_masks
from .edits import (
    EditOp,
    JunctionAdjustmentOp,
    LinearWorkOp,
    TemporaryRampOp,
    edit_from_dict,
    next_edit_id,
)
from .model import ChunkKey, base_height_at, chunk_affected_by_edit, generate_grid, validate_chunk_key
from .parameters import ParameterError, TerrainParameters
from .stitch import check_seams


PROJECT_FILE = "project.json"
CHUNK_DIR = "chunks"


def segment_intersection(
    x1: float, y1: float, x2: float, y2: float,
    x3: float, y3: float, x4: float, y4: float,
) -> tuple[float, float] | None:
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if denominator == 0.0:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / denominator
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / denominator
    if min(x1, x2) - 1e-9 <= px <= max(x1, x2) + 1e-9 and min(y1, y2) - 1e-9 <= py <= max(y1, y2) + 1e-9:
        if min(x3, x4) - 1e-9 <= px <= max(x3, x4) + 1e-9 and min(y3, y4) - 1e-9 <= py <= max(y3, y4) + 1e-9:
            return px, py
    return None


class ProjectError(RuntimeError):
    pass


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as fd:
        temporary_name = fd.name
        try:
            json.dump(payload, fd, ensure_ascii=False, allow_nan=False, sort_keys=True)
            fd.write("\n")
            fd.flush()
            os.fsync(fd.fileno())
        except BaseException:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
            raise
    os.replace(temporary_name, path)


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ProjectError(str(path) + " must contain a JSON object")
    return data


class TerrainProject:
    def __init__(
        self,
        root: Path,
        parameters: TerrainParameters,
        edits: list,
        chunks: dict[str, str],
        voided_edits: list[dict] | None = None,
        schedule: ConstructionSchedule | None = None,
        ledger: EarthworkLedger | None = None,
    ):
        self.root = root
        self.parameters = parameters
        self.edits = sorted(edits, key=lambda edit: edit.id)
        self.chunks = dict(chunks)
        self.voided_edits = list(voided_edits or [])
        self.schedule = schedule or ConstructionSchedule()
        self.ledger = ledger or EarthworkLedger()
        self.last_released_reservations: list = []

    @property
    def chunk_dir(self) -> Path:
        return self.root / CHUNK_DIR

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "parameters": self.parameters.to_dict(),
            "edits": [edit.to_dict() for edit in self.edits],
            "chunks": dict(sorted(self.chunks.items())),
            "voided_edits": list(self.voided_edits),
            "construction": self.schedule.to_dict(),
            "earthwork": self.ledger.to_dict(),
        }

    def linear_works(self) -> list[LinearWorkOp]:
        return [edit for edit in self.edits if isinstance(edit, LinearWorkOp)]

    def save_manifest(self) -> None:
        _atomic_write(self.root / PROJECT_FILE, self.to_dict())

    def save_chunk(self, key: ChunkKey) -> dict:
        validate_chunk_key(key)
        grid = generate_grid(self.parameters, key, self.edits, self.schedule)
        chunk_path = self.chunk_dir / key.filename
        _atomic_write(chunk_path, grid)
        self.chunks[str(key)] = CHUNK_DIR + "/" + key.filename
        self.save_manifest()
        return grid

    def generate_chunks(self, keys: list[ChunkKey]) -> list[dict]:
        unique = {}
        for key in keys:
            validate_chunk_key(key)
            unique[(key.x, key.y)] = key
        return [self.save_chunk(key) for key in unique.values()]

    def chunk_path(self, key: ChunkKey) -> Path:
        stored = self.chunks.get(str(key))
        if stored is None:
            raise ProjectError("chunk " + str(key) + " has not been generated")
        return self.root / stored

    def load_chunk(self, key: ChunkKey) -> dict:
        return _read_json(self.chunk_path(key))

    def load_all_chunks(self) -> dict[tuple[int, int], dict]:
        loaded = {}
        for stored_key in sorted(self.chunks):
            key = ChunkKey.parse(stored_key)
            data = self.load_chunk(key)
            stored_x = data.get("chunk", {}).get("x")
            stored_y = data.get("chunk", {}).get("y")
            if (stored_x, stored_y) != (key.x, key.y):
                raise ProjectError("chunk " + str(key) + " contains mismatched coordinates")
            loaded[(key.x, key.y)] = data
        return loaded

    def _validate_junction_references(self, edit) -> None:
        work_ids = {work.id for work in self.linear_works()}
        missing = [ref for ref in (edit.work_a, edit.work_b) if ref not in work_ids]
        if missing:
            raise ProjectError(
                "junction references missing work(s): " + ", ".join(str(value) for value in missing)
            )

    def add_edit(self, edit) -> list[ChunkKey]:
        edit.validate()
        if edit.id <= 0 or edit.id in {stored.id for stored in self.edits}:
            raise EditError("edit id must be the next sequential id")
        if isinstance(edit, JunctionAdjustmentOp):
            self._validate_junction_references(edit)
        if isinstance(edit, LinearWorkOp) and edit.revises is not None:
            if edit.revises == edit.id or edit.revises not in {work.id for work in self.linear_works()}:
                raise ProjectError(f"revision references missing work: {edit.revises}")
        released = []
        if isinstance(edit, LinearWorkOp) and edit.revises is not None:
            stage = max(self.schedule.current_stage, 1)
            released = self.ledger.release_reservations_for_work(
                edit.revises, stage, facts_through=stage,
                reason=f"design revision {edit.id} replaced unbuilt design of work {edit.revises}",
            )
        self.edits.append(edit)

        affected = []
        for stored_key in sorted(self.chunks):
            key = ChunkKey.parse(stored_key)
            if chunk_affected_by_edit(self.parameters, key, edit):
                affected.append(key)

        for key in affected:
            grid = generate_grid(self.parameters, key, self.edits, self.schedule)
            _atomic_write(self.chunk_dir / key.filename, grid)
        self.save_manifest()
        self.last_released_reservations = released
        return affected

    def _affected_stored_chunks(self, edit) -> list[ChunkKey]:
        return [
            ChunkKey.parse(stored_key)
            for stored_key in sorted(self.chunks)
            if chunk_affected_by_edit(self.parameters, ChunkKey.parse(stored_key), edit)
        ]

    def undo_edit(self, edit_id: int) -> dict:
        removed = next((edit for edit in self.edits if edit.id == edit_id), None)
        if removed is None:
            raise EditError(f"edit not found: {edit_id}")
        if isinstance(removed, LinearWorkOp) and self.schedule.active_intervals(removed.id, self.schedule.current_stage):
            raise ProjectError(
                f"{removed.kind} {removed.id} has completed terrain; demolish it as a new construction decision"
            )
        if isinstance(removed, JunctionAdjustmentOp) and removed.id in self.schedule.junction_stages:
            raise ProjectError("constructed junction cannot be rolled back; add a new revision instead")
        if isinstance(removed, LinearWorkOp) and any(
            isinstance(edit, LinearWorkOp) and edit.revises == removed.id for edit in self.edits
        ):
            raise ProjectError("undo the newer design revision before removing its referenced work")

        affected_set = set(self._affected_stored_chunks(removed))
        voided = []
        if isinstance(removed, LinearWorkOp):
            dependents = [
                edit
                for edit in self.edits
                if isinstance(edit, JunctionAdjustmentOp)
                and removed.id in (edit.work_a, edit.work_b)
            ]
            for junction in dependents:
                voided.append({
                    "id": junction.id,
                    "reason": f"referenced {removed.kind} {removed.id} was removed",
                    "work_a": junction.work_a,
                    "work_b": junction.work_b,
                })
                affected_set.update(self._affected_stored_chunks(junction))
            voided_ids = {item["id"] for item in voided}
            self.voided_edits.extend(voided)
            self.edits = [
                edit
                for edit in self.edits
                if edit.id != edit_id and edit.id not in voided_ids
            ]
        else:
            self.edits = [edit for edit in self.edits if edit.id != edit_id]
        affected = sorted(affected_set, key=lambda key: (key.x, key.y))
        for key in affected:
            grid = generate_grid(self.parameters, key, self.edits, self.schedule)
            _atomic_write(self.chunk_dir / key.filename, grid)
        self.save_manifest()
        return {
            "removed": removed,
            "updated_chunks": affected,
            "voided_edits": voided,
        }

    def _work(self, work_id: int) -> LinearWorkOp:
        work = next((item for item in self.linear_works() if item.id == work_id), None)
        if work is None:
            raise ProjectError(f"work not found: {work_id}")
        return work

    def _schedule_ready(self) -> None:
        if not self.schedule.enabled:
            self.schedule.current_stage = max(self.schedule.current_stage, 1)

    def _refresh_schedule_chunks(self, affected: list[ChunkKey]) -> None:
        for key in affected:
            grid = generate_grid(self.parameters, key, self.edits, self.schedule)
            _atomic_write(self.chunk_dir / key.filename, grid)
        self.save_manifest()

    def construct_work(
        self,
        work_id: int,
        start: float,
        end: float,
        stage: int | None = None,
    ) -> dict:
        work = self._work(work_id)
        if not 0.0 <= start <= end <= 1.0:
            raise EditError("construction bounds must satisfy 0 <= start <= end <= 1")
        self._schedule_ready()
        stage = self.schedule.current_stage if stage is None else stage
        if stage < 1 or stage < self.schedule.current_stage:
            raise EditError("cannot insert a construction stage in the past")
        before_stage = self.schedule.current_stage
        record = BuiltSpan(work_id, start, end, stage, "build")
        trial_schedule = ConstructionSchedule(
            records=[*self.schedule.records, record],
            junction_stages=dict(self.schedule.junction_stages),
            current_stage=max(self.schedule.current_stage, stage),
        )
        volumes = self._measure_span(work, start, end, before_stage, trial_schedule, stage)
        self.schedule.add_record(record)
        earth = self._record_span_earthwork(work, start, end, stage, volumes, action="build")
        affected = self._affected_stored_chunks(work)
        self._refresh_schedule_chunks(affected)
        return {"record": record, "updated_chunks": affected,
                "cut_volume": volumes.cut, "fill_volume": volumes.fill,
                "earthwork": earth}

    def demolish_work(self, work_id: int, start: float, end: float, stage: int | None = None) -> dict:
        work = self._work(work_id)
        if not 0.0 <= start <= end <= 1.0:
            raise EditError("demolition bounds must satisfy 0 <= start <= end <= 1")
        self._schedule_ready()
        stage = self.schedule.current_stage + 1 if stage is None else stage
        if stage < self.schedule.current_stage:
            raise EditError("cannot record demolition in a past stage")
        before_stage = self.schedule.current_stage
        record = BuiltSpan(work_id, start, end, stage, "demolish")
        trial_schedule = ConstructionSchedule(
            records=[*self.schedule.records, record],
            junction_stages=dict(self.schedule.junction_stages),
            current_stage=max(self.schedule.current_stage, stage),
        )
        volumes = self._measure_span(work, start, end, before_stage, trial_schedule, stage)
        self.schedule.add_record(record)
        earth = self._record_span_earthwork(work, start, end, stage, volumes, action="demolish")
        affected = self._affected_stored_chunks(work)
        self._refresh_schedule_chunks(affected)
        return {"record": record, "updated_chunks": affected,
                "cut_volume": volumes.cut, "fill_volume": volumes.fill,
                "earthwork": earth}

    def _measure_span(self, work, start, end, stage_before, after_schedule, stage_after):
        from .earthvolumes import SpanVolume, _grid_cell_area
        total_cut = 0.0
        total_fill = 0.0
        cells = 0
        area = _grid_cell_area(self.parameters)
        for key in self._affected_stored_chunks(work):
            before = generate_grid(self.parameters, key, self.edits, self.schedule, stage_before)
            after = generate_grid(self.parameters, key, self.edits, after_schedule, stage_after)
            masks, _ = span_masks(work, self.parameters, key, start, end)
            for cell_row in range(self.parameters.resolution - 1):
                for cell_col in range(self.parameters.resolution - 1):
                    corners = ((cell_row, cell_col), (cell_row, cell_col + 1), (cell_row + 1, cell_col), (cell_row + 1, cell_col + 1))
                    weighted = []
                    for r, c in corners:
                        weighted.append((masks[r][c], before["heights"][r][c], after["heights"][r][c]))
                    if all(item[0] <= 0.0 for item in weighted):
                        continue
                    total_cut += sum(max(old - new, 0.0) * m for m, old, new in weighted) / 4.0 * area
                    total_fill += sum(max(new - old, 0.0) * m for m, old, new in weighted) / 4.0 * area
                    cells += 1
        return SpanVolume(total_cut, total_fill, cells)

    def _record_span_earthwork(self, work, start, end, stage, volumes, action: str) -> dict:
        target = f"work{work.id}:{start:g}-{end:g}@{stage}"
        result = {"cut_batch": None, "recovery_batch": None, "requirement": None,
                  "cut": volumes.cut, "fill": volumes.fill}
        dedup = stable_dedup_key(["span", work.id, start, end, stage, action])
        if action == "build":
            if volumes.cut > 0.0:
                _, batch_id = self.ledger.record_cut(
                    volumes.cut, stage, work_id=work.id, start=start, end=end,
                    note=f"excavation constructing {work.kind} {work.id}", dedup_key=dedup + "c",
                )
                result["cut_batch"] = batch_id
            if volumes.fill > 0.0:
                _, requirement = self.ledger.add_demand(
                    target, volumes.fill, stage, work.id, start, end,
                    note=f"fill required for {work.kind} {work.id}", dedup_key=dedup + "f",
                )
                result["requirement"] = requirement.id
        else:
            if volumes.cut > 0.0:
                _, batch_id = self.ledger.record_recovery(
                    volumes.cut, stage, origin=target,
                    work_id=work.id, start=start, end=end,
                    note=f"recovered fill from demolishing {work.kind} {work.id}",
                    dedup_key=dedup + "r",
                )
                result["recovery_batch"] = batch_id
            if volumes.fill > 0.0:
                _, batch_id = self.ledger.record_cut(
                    volumes.fill, stage, work_id=work.id, start=start, end=end,
                    note=f"excavation while demolishing {work.kind} {work.id}",
                    dedup_key=dedup + "c",
                )
                result["cut_batch"] = batch_id
        return result

    def add_temporary_ramp(self, ramp: TemporaryRampOp, stage: int | None = None) -> list[ChunkKey]:
        ramp.validate()
        if ramp.id <= 0 or ramp.id in {stored.id for stored in self.edits}:
            raise EditError("ramp id must be the next sequential id")
        self._schedule_ready()
        if ramp.active_from < self.schedule.current_stage:
            raise EditError("temporary ramp cannot begin in a past stage")
        self.edits.append(ramp)
        self.schedule.current_stage = max(self.schedule.current_stage, ramp.active_from)
        affected = self._affected_stored_chunks(ramp)
        self._refresh_schedule_chunks(affected)
        return affected

    def construct_junction(self, junction_id: int, stage: int | None = None) -> list[ChunkKey]:
        junction = next((item for item in self.edits if isinstance(item, JunctionAdjustmentOp) and item.id == junction_id), None)
        if junction is None:
            raise ProjectError(f"junction not found: {junction_id}")
        self._schedule_ready()
        stage = self.schedule.current_stage if stage is None else stage
        if stage < self.schedule.current_stage:
            raise EditError("cannot construct a junction in a past stage")
        self.schedule.junction_stages[junction.id] = stage
        self.schedule.current_stage = max(self.schedule.current_stage, stage)
        affected = self._affected_stored_chunks(junction)
        self._refresh_schedule_chunks(affected)
        return affected

    def advance_stage(self) -> None:
        self._schedule_ready()
        old_stage = self.schedule.current_stage
        self.schedule.current_stage += 1
        self._recover_expired_ramps(old_stage, self.schedule.current_stage)
        affected = [ChunkKey.parse(key) for key in sorted(self.chunks)]
        self._refresh_schedule_chunks(affected)

    def _recover_expired_ramps(self, old_stage: int, new_stage: int) -> list[dict]:
        recovered = []
        for ramp in [edit for edit in self.edits if isinstance(edit, TemporaryRampOp)]:
            if ramp.active_until != old_stage:
                continue
            dedup = stable_dedup_key(["ramp-recovery", ramp.id, old_stage])
            if any(event.dedup_key == dedup for event in self.ledger.events):
                continue
            total_fill = 0.0
            for key in self._affected_stored_chunks(ramp):
                volume = integrate_ramp(
                    params=self.parameters, key=key, ramp=ramp, edits=self.edits,
                    schedule=self.schedule, active_stage=old_stage, recovery_stage=new_stage,
                )
                total_fill += volume.cut
            if total_fill <= 0.0:
                continue
            origin = f"temporary_ramp{ramp.id}:stage{old_stage}"
            _, batch_id = self.ledger.record_recovery(
                total_fill, new_stage, origin=origin,
                note=f"material recovered after temporary ramp {ramp.id} expired",
                dedup_key=dedup,
            )
            recovered.append({"ramp": ramp.id, "batch": batch_id, "quantity": total_fill,
                              "from_stage": old_stage, "stage": new_stage})
        return recovered

    def surface_at_stage(self, key: ChunkKey, stage: int) -> dict:
        validate_chunk_key(key)
        if stage < 0:
            raise EditError("stage must be non-negative")
        grid = generate_grid(self.parameters, key, self.edits, self.schedule, stage)
        return {
            "chunk": [key.x, key.y],
            "stage": stage,
            "base_heights": grid["base_heights"],
            "permanent_heights": grid["permanent_heights"],
            "temporary_deltas": grid["temporary_deltas"],
            "heights": grid["heights"],
            "construction_records": grid["construction_records"],
            "active_temporary_ramps": grid["active_temporary_ramps"],
            "earthwork": {
                "events": self.ledger.events_for_view(stage),
                "requirements": self.ledger.requirements_view(stage),
                "balances": self.ledger.balances(stage),
            },
        }

    def design_connection_preview(self, candidate: LinearWorkOp, t: float = 0.0) -> dict:
        candidate.validate()
        x, y = candidate.point_at(t)
        works = [*self.linear_works(), candidate]
        stage = self.schedule.current_stage
        current_height = self._non_linear_height(x, y)
        current_height, _ = permanent_height(
            current_height,
            x,
            y,
            self.linear_works(),
            [item for item in self.edits if isinstance(item, JunctionAdjustmentOp)],
            self.schedule,
            stage,
        )
        return {
            "point": [x, y],
            "stage": stage,
            "current_permanent_height": current_height,
            "new_design_height": candidate.height,
            "height_difference": candidate.height - current_height,
            "revises": candidate.revises,
        }

    def junction_height_basis(self, junction_id: int, x: float, y: float) -> dict:
        junction = next((item for item in self.edits if isinstance(item, JunctionAdjustmentOp) and item.id == junction_id), None)
        if junction is None:
            raise ProjectError(f"junction not found: {junction_id}")
        works = self.linear_works()
        junctions = [item for item in self.edits if isinstance(item, JunctionAdjustmentOp)]
        stage = self.schedule.junction_stages.get(junction.id, self.schedule.current_stage)
        basis = construction_basis(x, y, works, junctions, self.schedule, stage)
        height, _ = permanent_height(
            self._non_linear_height(x, y), x, y, works, junctions, self.schedule, stage
        )
        return {**basis, "permanent_height": height}

    def _non_linear_height(self, x: float, y: float) -> float:
        height = base_height_at(self.parameters, x, y)
        for edit in self.edits:
            if isinstance(edit, EditOp):
                height += edit.displacement_at(x, y)
        return height

    def revision_summary(self, edit: LinearWorkOp) -> dict:
        affected = [
            ChunkKey.parse(stored_key)
            for stored_key in sorted(self.chunks)
            if chunk_affected_by_edit(self.parameters, ChunkKey.parse(stored_key), edit)
        ]
        relations = []
        for other in self.linear_works():
            point = segment_intersection(
                edit.x1, edit.y1, edit.x2, edit.y2,
                other.x1, other.y1, other.x2, other.y2,
            )
            if point is None:
                continue
            x, y = point
            if edit.distance_to_segment(x, y) <= edit.influence_radius() and other.distance_to_segment(
                x, y
            ) <= other.influence_radius():
                edit_rank = (edit.priority, -edit.id)
                other_rank = (other.priority, -other.id)
                winner, loser = (edit, other) if edit_rank >= other_rank else (other, edit)
                relations.append({
                    "other_work": other.id,
                    "other_kind": other.kind,
                    "point": [x, y],
                    "retained_work": winner.id,
                    "overridden_work": loser.id,
                })
        return {
            "edit_id": edit.id,
            "kind": edit.kind,
            "priority": edit.priority,
            "affected_chunks": [[key.x, key.y] for key in affected],
            "intersections": relations,
        }

    def check_loaded_seams(self, tolerance: float = 0.0):
        return check_seams(self.load_all_chunks(), tolerance=tolerance)

    # -- earthwork ledger surface --------------------------------------

    def earth_balances(self, stage: int | None = None) -> dict:
        return self.ledger.balances(stage)

    def earth_trace(self, target: str, stage: int | None = None) -> dict:
        return self.ledger.trace_target(target, stage)

    def earth_supply(self, quantity: float, stage: int | None = None, note: str = "") -> dict:
        stage = self.schedule.current_stage if stage is None else stage
        event, batch_id = self.ledger.record_supply(quantity, stage, note=note)
        self.save_manifest()
        return {"event": event.to_dict(), "batch": batch_id}

    def earth_loss(self, batch: str, quantity: float, stage: int | None = None, note: str = "") -> dict:
        stage = self.schedule.current_stage if stage is None else stage
        event = self.ledger.record_loss(batch, quantity, stage, note=note)
        self.save_manifest()
        return {"event": event.to_dict()}

    def earth_reserve(self, batch: str, target: str, quantity: float, stage: int | None = None,
                      work_id: int | None = None, start: float | None = None, end: float | None = None) -> dict:
        stage = self.schedule.current_stage if stage is None else stage
        event, reservation = self.ledger.reserve(
            batch, target, quantity, stage, work_id=work_id, start=start, end=end,
        )
        self.save_manifest()
        return {"event": event.to_dict(), "reservation": reservation.to_dict()}

    def earth_arrive(self, reservation_id: str, stage: int | None = None, quantity: float | None = None) -> dict:
        stage = self.schedule.current_stage if stage is None else stage
        event = self.ledger.mark_arrived(reservation_id, stage, quantity)
        self.save_manifest()
        return {"event": event.to_dict()}

    def earth_place(self, target: str, quantity: float, stage: int | None = None,
                    reservation_id: str | None = None, batch: str | None = None,
                    work_id: int | None = None, start: float | None = None, end: float | None = None) -> dict:
        stage = self.schedule.current_stage if stage is None else stage
        event = self.ledger.place(
            reservation_id, target, quantity, stage, batch=batch,
            work_id=work_id, start=start, end=end,
        )
        self.save_manifest()
        return {"event": event.to_dict()}

    def earth_cancel_reservation(self, reservation_id: str, reason: str, stage: int | None = None) -> dict:
        stage = self.schedule.current_stage if stage is None else stage
        event = self.ledger.cancel_reservation(reservation_id, stage, reason)
        self.save_manifest()
        return {"event": event.to_dict()}

    def compare_completion_strategies(self, work_id: int, plan: list[dict]) -> dict:
        """Measure remaining design per planned stage; compare stockpile vs supply.

        ``plan`` is a list of {"stage": int, "start": float, "end": float}.
        Volumes are measured as real terrain deltas under trial schedules, so
        overlapping junction/ramp space is never counted twice. Nothing is
        saved: the result reports per-stage gaps and spans that cannot start
        without external make-up material.
        """
        work = self._work(work_id)
        ordered = sorted(plan, key=lambda item: item["stage"])
        trial_records = list(self.schedule.records)
        measured = []
        for item in ordered:
            stage = int(item["stage"])
            start = float(item["start"])
            end = float(item["end"])
            record = BuiltSpan(work_id, start, end, stage, "build")
            trial_schedule = ConstructionSchedule(
                records=[*trial_records, record],
                junction_stages=dict(self.schedule.junction_stages),
                current_stage=max(self.schedule.current_stage, stage),
            )
            volume = self._measure_span(work, start, end, stage - 1, trial_schedule, stage)
            trial_records.append(record)
            measured.append({
                "stage": stage, "start": start, "end": end,
                "cut": volume.cut, "fill": volume.fill,
            })
        stockpile_rows = []
        simulated_free = self.ledger.balances(self.schedule.current_stage)["totals"]["free"]
        for row in measured:
            available = simulated_free + row["cut"]
            stockpile_gap = max(row["fill"] - available, 0.0)
            used = min(row["fill"], available)
            simulated_free = available - used
            stockpile_rows.append({
                **row,
                "available_soil": available,
                "gap": stockpile_gap,
                "cannot_start_quantity": stockpile_gap,
                "cannot_start_span": None if stockpile_gap <= 1e-9 else {
                    "work_id": work_id,
                    "hint": "span needs external make-up material before it can be built",
                },
            })
        supply_rows = [
            {**row, "external_supply_required": row["fill"], "gap": 0.0}
            for row in measured
        ]
        return {
            "work_id": work_id,
            "stages": measured,
            "use_existing_stockpile": {
                "rows": stockpile_rows,
                "total_gap": sum(row["gap"] for row in stockpile_rows),
            },
            "external_supply": {
                "rows": supply_rows,
                "total_supply_required": sum(row["external_supply_required"] for row in supply_rows),
            },
        }


def create_project(root: Path, parameters: TerrainParameters, overwrite: bool = False) -> TerrainProject:
    parameters.validate()
    manifest = root / PROJECT_FILE
    if manifest.exists() and not overwrite:
        raise FileExistsError("project already exists: " + str(manifest))
    project = TerrainProject(root, parameters, [], {}, [], ConstructionSchedule(), EarthworkLedger())
    _atomic_write(manifest, project.to_dict())
    (root / CHUNK_DIR).mkdir(parents=True, exist_ok=True)
    return project


def load_project(root: Path) -> TerrainProject:
    manifest = root / PROJECT_FILE
    if not manifest.exists():
        raise FileNotFoundError("project not found: " + str(manifest))
    data = _read_json(manifest)
    if data.get("version") != 1:
        raise ProjectError("unsupported project version")
    raw_edits = data.get("edits", [])
    raw_chunks = data.get("chunks", {})
    raw_voided = data.get("voided_edits", [])
    schedule = ConstructionSchedule.from_dict(data.get("construction"))
    ledger = EarthworkLedger.from_dict(data.get("earthwork"))
    try:
        parameters = TerrainParameters.from_mapping(data["parameters"])
    except (ParameterError, KeyError, TypeError) as exc:
        raise ProjectError("invalid project manifest: " + str(exc)) from exc

    if not isinstance(raw_edits, list) or not isinstance(raw_chunks, dict) or not isinstance(raw_voided, list):
        raise ProjectError("invalid project manifest")
    edits = [edit_from_dict(value) for value in raw_edits]
    edit_ids = [edit.id for edit in edits]
    if len(edit_ids) != len(set(edit_ids)) or any(edit_id < 1 for edit_id in edit_ids):
        raise ProjectError("edit ids must be unique positive integers")

    work_ids = {edit.id for edit in edits if isinstance(edit, LinearWorkOp)}
    dangling = []
    surviving = []
    for edit in edits:
        if isinstance(edit, JunctionAdjustmentOp) and (
            edit.work_a not in work_ids or edit.work_b not in work_ids
        ):
            dangling.append({
                "id": edit.id,
                "reason": "junction referenced a missing work when project was reopened",
                "work_a": edit.work_a,
                "work_b": edit.work_b,
            })
        else:
            surviving.append(edit)
    edits = surviving
    raw_voided = [*raw_voided, *dangling]

    chunks = {}
    for key, path in raw_chunks.items():
        parsed = ChunkKey.parse(key)
        validate_chunk_key(parsed)
        if not isinstance(path, str) or not path:
            raise ProjectError("invalid chunk path for " + key)
        if not (root / path).is_file():
            raise ProjectError("missing chunk file: " + path)
        chunks[key] = path

    if not all(isinstance(item, dict) for item in raw_voided):
        raise ProjectError("invalid voided edit records")
    work_ids = {edit.id for edit in edits if isinstance(edit, LinearWorkOp)}
    missing_records = sorted({record.work_id for record in schedule.records if record.work_id not in work_ids})
    if missing_records:
        raise ProjectError(
            "construction records reference missing work(s): " + ", ".join(str(value) for value in missing_records)
        )
    junction_ids = {edit.id for edit in edits if isinstance(edit, JunctionAdjustmentOp)}
    missing_junctions = sorted(set(schedule.junction_stages) - junction_ids)
    if missing_junctions:
        raise ProjectError(
            "construction records reference missing junction(s): "
            + ", ".join(str(value) for value in missing_junctions)
        )

    return TerrainProject(root, parameters, edits, chunks, raw_voided, schedule, ledger)
