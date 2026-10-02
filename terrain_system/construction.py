from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .edits import (
    EditError,
    JunctionAdjustmentOp,
    LinearWorkOp,
    TemporaryRampOp,
)


@dataclass(frozen=True)
class BuiltSpan:
    work_id: int
    start: float
    end: float
    stage: int
    action: str = "build"

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "start": self.start,
            "end": self.end,
            "stage": self.stage,
            "action": self.action,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "BuiltSpan":
        span = BuiltSpan(
            work_id=data["work_id"],
            start=float(data["start"]),
            end=float(data["end"]),
            stage=data["stage"],
            action=data.get("action", "build"),
        )
        span.validate()
        return span

    def validate(self) -> None:
        if not isinstance(self.work_id, int) or isinstance(self.work_id, bool) or self.work_id < 1:
            raise EditError("built span must reference a positive work id")
        if not 0.0 <= self.start <= self.end <= 1.0:
            raise EditError("built span bounds must satisfy 0 <= start <= end <= 1")
        if not isinstance(self.stage, int) or isinstance(self.stage, bool) or self.stage < 1:
            raise EditError("construction stage must be a positive integer")
        if self.action not in ("build", "demolish"):
            raise EditError("construction action must be build or demolish")


class ConstructionSchedule:
    """Persistent construction state, distinct from design revisions."""

    def __init__(
        self,
        records: list[BuiltSpan] | None = None,
        junction_stages: dict[int, int] | None = None,
        current_stage: int = 0,
    ):
        self.records = list(records or [])
        self.junction_stages = dict(junction_stages or {})
        self.current_stage = current_stage

    @property
    def enabled(self) -> bool:
        return bool(self.records or self.junction_stages or self.current_stage > 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "current_stage": self.current_stage,
            "records": [record.to_dict() for record in self.records],
            "junction_stages": [
                {"junction": junction, "stage": stage}
                for junction, stage in sorted(self.junction_stages.items())
            ],
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> "ConstructionSchedule":
        if not data:
            return ConstructionSchedule()
        records = [BuiltSpan.from_dict(item) for item in data.get("records", [])]
        stages = {
            int(item["junction"]): int(item["stage"])
            for item in data.get("junction_stages", [])
        }
        stage = int(data.get("current_stage", 0))
        schedule = ConstructionSchedule(records, stages, stage)
        schedule.validate()
        return schedule

    def validate(self) -> None:
        if not isinstance(self.current_stage, int) or self.current_stage < 0:
            raise EditError("current_stage must be a non-negative integer")
        for record in self.records:
            record.validate()
        for junction, stage in self.junction_stages.items():
            if junction < 1 or stage < 1:
                raise EditError("junction construction records must use positive ids and stages")

    def add_record(self, record: BuiltSpan) -> None:
        record.validate()
        self.records.append(record)
        self.current_stage = max(self.current_stage, record.stage)

    def active_intervals(self, work_id: int, stage: int) -> list[tuple[float, float]]:
        intervals: list[tuple[float, float]] = []
        for record in self.records:
            if record.work_id != work_id or record.stage > stage:
                continue
            if record.action == "build":
                intervals.append((record.start, record.end))
            else:
                intervals = [
                    part
                    for interval in intervals
                    for part in _subtract_interval(interval, (record.start, record.end))
                ]
        return _merge_intervals(intervals)


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _subtract_interval(
    interval: tuple[float, float], removed: tuple[float, float]
) -> list[tuple[float, float]]:
    start, end = interval
    remove_start, remove_end = removed
    result = []
    if remove_start > start:
        result.append((start, min(end, remove_start)))
    if remove_end < end:
        result.append((max(start, remove_end), end))
    return [(left, right) for left, right in result if left < right]


def revision_roots(works: list[LinearWorkOp]) -> dict[int, int]:
    parents = {work.id: (work.revises or work.id) for work in works}

    def root(work_id: int) -> int:
        seen = set()
        while work_id in parents and parents[work_id] != work_id and work_id not in seen:
            seen.add(work_id)
            work_id = parents[work_id]
        return work_id

    return {work_id: root(work_id) for work_id in parents}


def _point_is_in_intervals(work: LinearWorkOp, x: float, y: float, intervals: list[tuple[float, float]]) -> float:
    projection = min(max(work.projection_at(x, y), 0.0), 1.0)
    if not any(start <= projection <= end for start, end in intervals):
        return 0.0
    return work.core_weight_at(x, y)


def permanent_height(
    current: float,
    x: float,
    y: float,
    works: list[LinearWorkOp],
    junctions: list[JunctionAdjustmentOp],
    schedule: ConstructionSchedule | None,
    stage: int,
) -> tuple[float, dict[int, float]]:
    roots = revision_roots(works)
    by_root: dict[int, list[tuple[LinearWorkOp, float]]] = {}
    coverage: dict[int, float] = {}

    for work in works:
        if schedule is None:
            weight = work.core_weight_at(x, y)
        else:
            intervals = schedule.active_intervals(work.id, stage)
            weight = _point_is_in_intervals(work, x, y, intervals)
        if weight > 0.0:
            root = roots[work.id]
            by_root.setdefault(root, []).append((work, weight))
            coverage[work.id] = weight

    if not by_root:
        return current, coverage

    winners = [max(candidates, key=lambda item: item[0].id) for candidates in by_root.values()]
    best = max(winners, key=lambda item: (item[0].priority, -item[0].id))
    target = best[0].height
    blend = max(
        weight
        for work, weight in winners
        if (work.priority, -work.id) == (best[0].priority, -best[0].id)
    )
    active_ids = set(coverage)

    for junction in junctions:
        if schedule is not None and schedule.junction_stages.get(junction.id, 10**18) > stage:
            continue
        if junction.work_a not in active_ids or junction.work_b not in active_ids:
            continue
        weight = junction.weight_at(x, y)
        if weight >= blend and weight > 0.0:
            target = junction.height
            blend = weight
    return current + blend * (target - current), coverage


def temporary_height(
    current: float,
    x: float,
    y: float,
    ramps: list[TemporaryRampOp],
    stage: int,
) -> tuple[float, list[int]]:
    active = []
    for ramp in sorted(ramps, key=lambda item: item.id):
        if ramp.active_at_stage(stage):
            weight = ramp.core_weight_at(x, y)
            if weight > 0.0:
                active.append(ramp.id)
                current = current + weight * (ramp.height - current)
    return current, active


def construction_basis(
    x: float,
    y: float,
    works: list[LinearWorkOp],
    junctions: list[JunctionAdjustmentOp],
    schedule: ConstructionSchedule,
    stage: int,
) -> dict[str, Any]:
    height, coverage = permanent_height(0.0, x, y, works, junctions, schedule, stage)
    active = [work_id for work_id, weight in coverage.items() if weight > 0.0]
    selected_work = max(
        (work for work in works if work.id in active),
        key=lambda work: (work.priority, -work.id),
        default=None,
    )
    selected = selected_work.id if selected_work is not None else None
    junction = next(
        (
            item.id
            for item in junctions
            if item.work_a in active
            and item.work_b in active
            and schedule.junction_stages.get(item.id, 10**18) <= stage
            and item.weight_at(x, y) > 0.0
        ),
        None,
    )
    return {
        "point": [x, y],
        "stage": stage,
        "height_basis": junction if junction is not None else selected,
        "active_works": sorted(active),
        "junction": junction,
        "permanent_height": height,
    }
