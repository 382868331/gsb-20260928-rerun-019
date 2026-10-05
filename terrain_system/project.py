from __future__ import annotations

import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .construction import BuiltSpan, ConstructionSchedule, construction_basis, permanent_height
from .earthwork import surface_volume_change
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
from .materials import MaterialLedger


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
        materials: MaterialLedger | None = None,
    ):
        self.root = root
        self.parameters = parameters
        self.edits = sorted(edits, key=lambda edit: edit.id)
        self.chunks = dict(chunks)
        self.voided_edits = list(voided_edits or [])
        self.schedule = schedule or ConstructionSchedule()
        self.materials = materials or MaterialLedger()

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
            "materials": self.materials.to_dict(),
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

    def _record_stage_earthwork(
        self,
        affected: list[ChunkKey],
        stage: int,
        trigger: str,
        before_chunks: dict[tuple[int, int], dict] | None = None,
    ) -> dict | None:
        if not affected:
            return None
        before = before_chunks or {
            (key.x, key.y): generate_grid(
                self.parameters, key, self.edits, self.schedule, max(0, stage - 1)
            )
            for key in affected
        }
        after = {(key.x, key.y): self.load_chunk(key) for key in affected}
        change = surface_volume_change(
            before,
            after,
            self.parameters.chunk_size / (self.parameters.resolution - 1),
        )
        change = {
            key: (0.0 if value < 1e-12 else value)
            for key, value in change.items()
        }
        event = self.materials.record_earthwork(stage, trigger, change["cut"], change["fill"])
        return {"event": event.to_dict(), **change}

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
        affected = self._affected_stored_chunks(work)
        record = BuiltSpan(work_id, start, end, stage, "build")
        before_chunks = {
            (key.x, key.y): generate_grid(self.parameters, key, self.edits, self.schedule, stage)
            for key in affected
        }
        self.schedule.add_record(record)
        self._refresh_schedule_chunks(affected)
        earthwork = self._record_stage_earthwork(affected, stage, f"construct-work-{work_id}", before_chunks)
        self.save_manifest()
        return {"record": record, "updated_chunks": affected, "earthwork": earthwork}

    def demolish_work(self, work_id: int, start: float, end: float, stage: int | None = None) -> dict:
        work = self._work(work_id)
        if not 0.0 <= start <= end <= 1.0:
            raise EditError("demolition bounds must satisfy 0 <= start <= end <= 1")
        self._schedule_ready()
        stage = self.schedule.current_stage + 1 if stage is None else stage
        if stage < self.schedule.current_stage:
            raise EditError("cannot record demolition in a past stage")
        affected = self._affected_stored_chunks(work)
        record = BuiltSpan(work_id, start, end, stage, "demolish")
        before_chunks = {
            (key.x, key.y): generate_grid(self.parameters, key, self.edits, self.schedule, stage)
            for key in affected
        }
        self.schedule.add_record(record)
        self._refresh_schedule_chunks(affected)
        earthwork = self._record_stage_earthwork(affected, stage, f"demolish-work-{work_id}", before_chunks)
        self.save_manifest()
        return {"record": record, "updated_chunks": affected, "earthwork": earthwork}

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
        before_chunks = {
            (key.x, key.y): generate_grid(self.parameters, key, self.edits, self.schedule, stage)
            for key in affected
        }
        self._refresh_schedule_chunks(affected)
        earthwork = self._record_stage_earthwork(
            affected, stage, f"construct-junction-{junction_id}", before_chunks
        )
        self.save_manifest()
        return {"updated_chunks": affected, "earthwork": earthwork}

    def advance_stage(self) -> None:
        self._schedule_ready()
        self.schedule.current_stage += 1
        affected = [ChunkKey.parse(key) for key in sorted(self.chunks)]
        self._refresh_schedule_chunks(affected)

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


def create_project(root: Path, parameters: TerrainParameters, overwrite: bool = False) -> TerrainProject:
    parameters.validate()
    manifest = root / PROJECT_FILE
    if manifest.exists() and not overwrite:
        raise FileExistsError("project already exists: " + str(manifest))
    project = TerrainProject(root, parameters, [], {})
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
    materials = MaterialLedger.from_dict(data.get("materials"))
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

    return TerrainProject(root, parameters, edits, chunks, raw_voided, schedule, materials)
