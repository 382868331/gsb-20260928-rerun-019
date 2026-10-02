from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

WORK_KINDS = ("road", "river")


class EditError(ValueError):
    pass


def _validate_number(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise EditError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise EditError(f"{name} must be finite")
    return number


def _validate_id(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EditError("edit id must be a positive integer")
    return value


@dataclass(frozen=True)
class EditOp:
    id: int
    x: float
    y: float
    radius: float
    delta: float
    falloff: float = 2.0

    @property
    def type_name(self) -> str:
        return "brush"

    def validate(self) -> None:
        _validate_id(self.id)
        for name in ("x", "y", "radius", "delta", "falloff"):
            value = getattr(self, name)
            _validate_number(value, name)
        if self.radius <= 0:
            raise EditError("radius must be positive")
        if self.falloff <= 0:
            raise EditError("falloff must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "brush",
            "id": self.id,
            "x": self.x,
            "y": self.y,
            "radius": self.radius,
            "delta": self.delta,
            "falloff": self.falloff,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EditOp":
        try:
            op = EditOp(
                id=data["id"],
                x=data["x"],
                y=data["y"],
                radius=data["radius"],
                delta=data["delta"],
                falloff=data.get("falloff", 2.0),
            )
        except KeyError as exc:
            raise EditError(f"missing edit field: {exc.args[0]}") from exc
        op.validate()
        return op

    def displacement_at(self, x: float, y: float) -> float:
        distance = math.hypot(x - self.x, y - self.y)
        if distance >= self.radius:
            return 0.0
        strength = 1.0 - distance / self.radius
        return self.delta * (strength ** self.falloff)


@dataclass(frozen=True)
class LinearWorkOp:
    """A road or river segment with an explicit design influence range.

    priority explicitly records whose design wins inside the overlap of two
    works; equal priority is resolved deterministically by the smaller id so
    the terrain never depends on edit insertion or chunk generation order.
    """

    id: int
    kind: str
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    height: float
    shoulder: float = 0.0
    priority: int = 0
    revises: int | None = None

    @property
    def type_name(self) -> str:
        return self.kind

    def validate(self) -> None:
        _validate_id(self.id)
        if self.kind not in WORK_KINDS:
            raise EditError("kind must be one of: " + ", ".join(WORK_KINDS))
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise EditError("priority must be an integer")
        if self.revises is not None and (
            not isinstance(self.revises, int)
            or isinstance(self.revises, bool)
            or self.revises < 1
        ):
            raise EditError("revises must reference a positive work id")
        for name in ("x1", "y1", "x2", "y2", "width", "height", "shoulder"):
            _validate_number(getattr(self, name), name)
        if self.width <= 0:
            raise EditError("width must be positive")
        if self.shoulder < 0:
            raise EditError("shoulder must be non-negative")
        if math.hypot(self.x2 - self.x1, self.y2 - self.y1) == 0.0:
            raise EditError(f"{self.kind} endpoints must not be identical")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.kind,
            "id": self.id,
            "kind": self.kind,
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "width": self.width,
            "height": self.height,
            "shoulder": self.shoulder,
            "priority": self.priority,
            "revises": self.revises,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "LinearWorkOp":
        kind = data.get("type", "road")
        if kind not in WORK_KINDS:
            raise EditError(f"unknown linear work kind: {kind}")
        try:
            op = LinearWorkOp(
                id=data["id"],
                kind=kind,
                x1=data["x1"],
                y1=data["y1"],
                x2=data["x2"],
                y2=data["y2"],
                width=data["width"],
                height=data["height"],
                shoulder=data.get("shoulder", 0.0),
                priority=data.get("priority", 0),
                revises=data.get("revises"),
            )
        except KeyError as exc:
            raise EditError(f"missing edit field: {exc.args[0]}") from exc
        op.validate()
        return op

    def distance_to_segment(self, x: float, y: float) -> float:
        dx = self.x2 - self.x1
        dy = self.y2 - self.y1
        length_squared = dx * dx + dy * dy
        projection = ((x - self.x1) * dx + (y - self.y1) * dy) / length_squared
        projection = min(max(projection, 0.0), 1.0)
        closest_x = self.x1 + projection * dx
        closest_y = self.y1 + projection * dy
        return math.hypot(x - closest_x, y - closest_y)

    def core_weight_at(self, x: float, y: float) -> float:
        distance = self.distance_to_segment(x, y)
        inner = self.width / 2.0
        outer = inner + self.shoulder
        if distance >= outer:
            return 0.0
        if distance <= inner or self.shoulder == 0.0:
            return 1.0
        t = (distance - inner) / self.shoulder
        smooth = t * t * (3.0 - 2.0 * t)
        return 1.0 - smooth

    def influence_radius(self) -> float:
        return self.width / 2.0 + self.shoulder

    def projection_at(self, x: float, y: float) -> float:
        dx = self.x2 - self.x1
        dy = self.y2 - self.y1
        length_squared = dx * dx + dy * dy
        return ((x - self.x1) * dx + (y - self.y1) * dy) / length_squared

    def longitudinal_weight_at(self, x: float, y: float, start: float, end: float) -> float:
        projection = min(max(self.projection_at(x, y), 0.0), 1.0)
        if projection < start or projection > end:
            return 0.0
        return self.core_weight_at(x, y)

    def point_at(self, t: float) -> tuple[float, float]:
        return self.x1 + t * (self.x2 - self.x1), self.y1 + t * (self.y2 - self.y1)


@dataclass(frozen=True)
class JunctionAdjustmentOp:
    """Independent revision scoped to one small road/river overlap.

    The adjustment is a separate revision: it wins over both referenced works
    only inside its radius. Removing a referenced work invalidates it instead
    of silently letting the last edit take over the whole terrain.
    """

    id: int
    work_a: int
    work_b: int
    x: float
    y: float
    radius: float
    height: float
    feather: float = 0.0

    @property
    def type_name(self) -> str:
        return "junction"

    def validate(self) -> None:
        _validate_id(self.id)
        for name in ("work_a", "work_b"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise EditError(f"{name} must reference a positive edit id")
        if self.work_a == self.work_b:
            raise EditError("junction must reference two distinct works")
        for name in ("x", "y", "radius", "height", "feather"):
            _validate_number(getattr(self, name), name)
        if self.radius <= 0:
            raise EditError("radius must be positive")
        if self.feather < 0:
            raise EditError("feather must be non-negative")
        if self.feather >= self.radius:
            raise EditError("feather must be smaller than radius")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "junction",
            "id": self.id,
            "work_a": self.work_a,
            "work_b": self.work_b,
            "x": self.x,
            "y": self.y,
            "radius": self.radius,
            "height": self.height,
            "feather": self.feather,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "JunctionAdjustmentOp":
        try:
            op = JunctionAdjustmentOp(
                id=data["id"],
                work_a=data["work_a"],
                work_b=data["work_b"],
                x=data["x"],
                y=data["y"],
                radius=data["radius"],
                height=data["height"],
                feather=data.get("feather", 0.0),
            )
        except KeyError as exc:
            raise EditError(f"missing edit field: {exc.args[0]}") from exc
        op.validate()
        return op

    def weight_at(self, x: float, y: float) -> float:
        distance = math.hypot(x - self.x, y - self.y)
        if distance >= self.radius:
            return 0.0
        inner = self.radius - self.feather
        if distance <= inner or self.feather == 0.0:
            return 1.0
        t = (distance - inner) / self.feather
        smooth = t * t * (3.0 - 2.0 * t)
        return 1.0 - smooth

    def influence_radius(self) -> float:
        return self.radius


AnyEdit = "EditOp | LinearWorkOp | JunctionAdjustmentOp"
RevisionOp = "LinearWorkOp | JunctionAdjustmentOp"


@dataclass(frozen=True)
class TemporaryRampOp:
    """A temporary driving surface valid only for an explicit stage range."""

    id: int
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    height: float
    shoulder: float = 0.0
    active_from: int = 1
    active_until: int = 1

    @property
    def type_name(self) -> str:
        return "temporary_ramp"

    def validate(self) -> None:
        _validate_id(self.id)
        for name in ("x1", "y1", "x2", "y2", "width", "height", "shoulder"):
            _validate_number(getattr(self, name), name)
        for name in ("active_from", "active_until"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise EditError(f"{name} must be a positive stage number")
        if self.width <= 0:
            raise EditError("width must be positive")
        if self.shoulder < 0:
            raise EditError("shoulder must be non-negative")
        if self.active_until < self.active_from:
            raise EditError("active_until must not precede active_from")
        if math.hypot(self.x2 - self.x1, self.y2 - self.y1) == 0.0:
            raise EditError("ramp endpoints must not be identical")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "temporary_ramp",
            "id": self.id,
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "width": self.width,
            "height": self.height,
            "shoulder": self.shoulder,
            "active_from": self.active_from,
            "active_until": self.active_until,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TemporaryRampOp":
        try:
            op = TemporaryRampOp(
                id=data["id"],
                x1=data["x1"],
                y1=data["y1"],
                x2=data["x2"],
                y2=data["y2"],
                width=data["width"],
                height=data["height"],
                shoulder=data.get("shoulder", 0.0),
                active_from=data.get("active_from", 1),
                active_until=data.get("active_until", 1),
            )
        except KeyError as exc:
            raise EditError(f"missing edit field: {exc.args[0]}") from exc
        op.validate()
        return op

    def core_weight_at(self, x: float, y: float) -> float:
        distance = self._distance(x, y)
        inner = self.width / 2.0
        outer = inner + self.shoulder
        if distance >= outer:
            return 0.0
        if distance <= inner or self.shoulder == 0.0:
            return 1.0
        t = (distance - inner) / self.shoulder
        smooth = t * t * (3.0 - 2.0 * t)
        return 1.0 - smooth

    def _distance(self, x: float, y: float) -> float:
        dx = self.x2 - self.x1
        dy = self.y2 - self.y1
        length_squared = dx * dx + dy * dy
        projection = ((x - self.x1) * dx + (y - self.y1) * dy) / length_squared
        projection = min(max(projection, 0.0), 1.0)
        return math.hypot(x - (self.x1 + projection * dx), y - (self.y1 + projection * dy))

    def influence_radius(self) -> float:
        return self.width / 2.0 + self.shoulder

    def active_at_stage(self, stage: int) -> bool:
        return self.active_from <= stage <= self.active_until


def split_edits(edits: list[AnyEdit]) -> tuple[list[EditOp], list[LinearWorkOp], list[JunctionAdjustmentOp]]:
    brushes = []
    works = []
    junctions = []
    for edit in edits:
        if isinstance(edit, JunctionAdjustmentOp):
            junctions.append(edit)
        elif isinstance(edit, LinearWorkOp):
            works.append(edit)
        else:
            brushes.append(edit)
    return brushes, works, junctions


def split_all_edits(edits: list) -> tuple[list[EditOp], list[LinearWorkOp], list[JunctionAdjustmentOp], list[TemporaryRampOp]]:
    ramps = [edit for edit in edits if isinstance(edit, TemporaryRampOp)]
    without_ramps = [edit for edit in edits if not isinstance(edit, TemporaryRampOp)]
    brushes, works, junctions = split_edits(without_ramps)
    return brushes, works, junctions, ramps


def linear_work_rank(work: LinearWorkOp) -> tuple[int, int]:
    return (work.priority, -work.id)


def works_at_point(
    works: list[LinearWorkOp], x: float, y: float
) -> list[tuple[LinearWorkOp, float]]:
    present = []
    for work in works:
        weight = work.core_weight_at(x, y)
        if weight > 0.0:
            present.append((work, weight))
    return present


def resolve_linear_height(
    current: float,
    x: float,
    y: float,
    works: list[LinearWorkOp],
    junctions: list[JunctionAdjustmentOp],
) -> float:
    present = works_at_point(works, x, y)
    if not present:
        return current

    best_work = max(present, key=lambda item: linear_work_rank(item[0]))[0]
    target = best_work.height
    blend = max(weight for work, weight in present if linear_work_rank(work) == linear_work_rank(best_work))

    present_ids = {work.id for work, _ in present}
    for junction in junctions:
        if junction.work_a not in present_ids or junction.work_b not in present_ids:
            continue
        weight = junction.weight_at(x, y)
        if weight >= blend and weight > 0.0:
            target = junction.height
            blend = weight
    return current + blend * (target - current)


def apply_edits(base: float, x: float, y: float, edits: list[AnyEdit]) -> float:
    brushes, works, junctions = split_edits(sorted(edits, key=lambda edit: edit.id))
    height = base
    for brush in sorted(brushes, key=lambda edit: edit.id):
        height += brush.displacement_at(x, y)
    return resolve_linear_height(height, x, y, sorted(works, key=lambda edit: edit.id), sorted(junctions, key=lambda edit: edit.id))


def next_edit_id(edits: list[AnyEdit]) -> int:
    return max((edit.id for edit in edits), default=0) + 1


def edit_from_dict(data: dict[str, Any]) -> AnyEdit:
    edit_type = data.get("type", "brush")
    if edit_type == "brush":
        return EditOp.from_dict(data)
    if edit_type in WORK_KINDS:
        return LinearWorkOp.from_dict(data)
    if edit_type == "junction":
        return JunctionAdjustmentOp.from_dict(data)
    if edit_type == "temporary_ramp":
        return TemporaryRampOp.from_dict(data)
    raise EditError(f"unknown edit type: {edit_type}")
