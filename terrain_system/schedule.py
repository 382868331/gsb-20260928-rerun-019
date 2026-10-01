from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .edits import (
    EditError,
    JunctionAdjustmentOp,
    LinearWorkOp,
    TransitionOp,
)


@dataclass(frozen=True)
class Stage:
    id: int
    name: str

    def validate(self) -> None:
        if not isinstance(self.id, int) or isinstance(self.id, bool) or self.id < 1:
            raise EditError("stage id must be a positive integer")
        if not isinstance(self.name, str) or not self.name:
            raise EditError("stage name must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Stage":
        try:
            stage = Stage(id=data["id"], name=data["name"])
        except KeyError as exc:
            raise EditError(f"missing stage field: {exc.args[0]}") from exc
        stage.validate()
        return stage


@dataclass(frozen=True)
class ScheduleEntry:
    """Builds the portion [start, end] (fraction along the line) of a work at one stage.

    start/end of None mean the whole work. Junction revisions only support
    whole-work scheduling.
    """

    work_id: int
    stage: int
    start: float | None = None
    end: float | None = None

    def portion(self) -> tuple[float, float]:
        start = 0.0 if self.start is None else self.start
        end = 1.0 if self.end is None else self.end
        return start, end

    def validate(self) -> None:
        for name in ("work_id", "stage"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise EditError(f"{name} must be a positive integer")
        if (self.start is None) != (self.end is None):
            raise EditError("start and end must be given together")
        if self.start is not None:
            for name in ("start", "end"):
                value = getattr(self, name)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise EditError(f"{name} must be a number")
                if not 0.0 <= float(value) <= 1.0:
                    raise EditError(f"{name} must be within [0, 1]")
            if not float(self.start) < float(self.end):
                raise EditError("start must be smaller than end")

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "stage": self.stage,
            "start": self.start,
            "end": self.end,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ScheduleEntry":
        try:
            entry = ScheduleEntry(
                work_id=data["work_id"],
                stage=data["stage"],
                start=data.get("start"),
                end=data.get("end"),
            )
        except KeyError as exc:
            raise EditError(f"missing schedule field: {exc.args[0]}") from exc
        entry.validate()
        return entry


@dataclass(frozen=True)
class Demolition:
    """A separate construction decision that removes a built work at one stage."""

    work_id: int
    stage: int

    def validate(self) -> None:
        for name in ("work_id", "stage"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise EditError(f"{name} must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {"work_id": self.work_id, "stage": self.stage}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Demolition":
        try:
            demolition = Demolition(work_id=data["work_id"], stage=data["stage"])
        except KeyError as exc:
            raise EditError(f"missing demolition field: {exc.args[0]}") from exc
        demolition.validate()
        return demolition


@dataclass(frozen=True)
class PartialWork(LinearWorkOp):
    """A linear work clipped to its already-built parameter intervals."""

    intervals: tuple[tuple[float, float], ...] = ()

    def core_weight_at(self, x: float, y: float) -> float:
        t = self.project_t(x, y)
        for start, end in self.intervals:
            if start - 1e-9 <= t <= end + 1e-9:
                return super().core_weight_at(x, y)
        return 0.0


def merge_intervals(intervals: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    ordered = sorted(intervals)
    merged: list[list[float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((item[0], item[1]) for item in merged)


def built_intervals(work_id: int, schedule: list[ScheduleEntry], stage: int | None) -> tuple[tuple[float, float], ...]:
    portions = [
        entry.portion()
        for entry in schedule
        if entry.work_id == work_id and (stage is None or entry.stage <= stage)
    ]
    return merge_intervals(portions) if portions else ()


def view_edits(
    edits: list,
    schedule: list[ScheduleEntry],
    demolitions: list[Demolition],
    stage: int | None,
) -> list:
    """Resolve the edit list visible at one construction stage.

    stage=None means the final state: every scheduled portion is built and
    every demolition (including a transition's own demolish_stage) has
    happened. Works without any schedule entry are unmanaged by the
    construction plan and apply in full at every stage.
    """
    demolished_ids = {
        demolition.work_id
        for demolition in demolitions
        if stage is None or demolition.stage <= stage
    }
    scheduled_ids = {entry.work_id for entry in schedule}
    result = []
    for edit in edits:
        if edit.id in demolished_ids:
            continue
        if isinstance(edit, TransitionOp):
            if stage is None:
                if edit.demolish_stage is not None:
                    continue
            else:
                if edit.built_stage > stage:
                    continue
                if edit.demolish_stage is not None and edit.demolish_stage <= stage:
                    continue
            result.append(edit)
        elif isinstance(edit, LinearWorkOp):
            if edit.id not in scheduled_ids:
                result.append(edit)
                continue
            intervals = built_intervals(edit.id, schedule, stage)
            if not intervals:
                continue
            if intervals == ((0.0, 1.0),):
                result.append(edit)
            else:
                result.append(PartialWork(
                    id=edit.id,
                    kind=edit.kind,
                    x1=edit.x1,
                    y1=edit.y1,
                    x2=edit.x2,
                    y2=edit.y2,
                    width=edit.width,
                    height=edit.height,
                    shoulder=edit.shoulder,
                    priority=edit.priority,
                    intervals=intervals,
                ))
        elif isinstance(edit, JunctionAdjustmentOp):
            if edit.id in scheduled_ids:
                if any(entry.work_id == edit.id and (stage is None or entry.stage <= stage) for entry in schedule):
                    result.append(edit)
            else:
                result.append(edit)
        else:
            result.append(edit)
    return result
