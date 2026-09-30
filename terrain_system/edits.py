from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any


class EditError(ValueError):
    pass


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
        if not isinstance(self.id, int) or self.id < 1:
            raise EditError("edit id must be a positive integer")
        for name in ("x", "y", "radius", "delta", "falloff"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise EditError(f"{name} must be a number")
            if not math.isfinite(value):
                raise EditError(f"{name} must be finite")
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
        return self.delta * (strength**self.falloff)

    def applied_height(self, current: float, x: float, y: float) -> float:
        return current + self.displacement_at(x, y)


def apply_edits(base: float, x: float, y: float, edits: list) -> float:
    height = base
    ordered = sorted(edits, key=lambda item: item.id)
    works = {edit.id: edit for edit in ordered if isinstance(edit, LinearWorkOp)}

    for edit in ordered:
        if isinstance(edit, EditOp):
            height = edit.applied_height(height, x, y)

    candidates = [
        (edit.weight_at(x, y), edit)
        for edit in ordered
        if isinstance(edit, LinearWorkOp)
    ]
    candidates = [(weight, edit) for weight, edit in candidates if weight > 0.0]
    if candidates:
        _, winner = min(candidates, key=lambda item: winning_key(item[1]))
        weight = winner.weight_at(x, y)
        height = height + weight * (winner.height - height)

    for edit in ordered:
        if isinstance(edit, CrossingEditOp) and edit.is_active(works):
            height = edit.applied_height(height, x, y, works)
    return height


def winning_key(work: "LinearWorkOp") -> tuple[float, int]:
    # Higher priority keeps its design; equal priority resolves to the
    # earlier revision, so a later edit can never silently overwrite it.
    return (-work.priority, work.id)


def choose_winner(first: "LinearWorkOp", second: "LinearWorkOp") -> "LinearWorkOp":
    return first if winning_key(first) <= winning_key(second) else second


def next_edit_id(edits: list) -> int:
    return max((edit.id for edit in edits), default=0) + 1


@dataclass(frozen=True)
class LinearWorkOp:
    id: int
    kind: str
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    height: float
    shoulder: float = 0.0
    priority: float = 0.0

    @property
    def type_name(self) -> str:
        return self.kind

    def validate(self) -> None:
        if not isinstance(self.id, int) or self.id < 1:
            raise EditError("edit id must be a positive integer")
        if self.kind not in ("road", "river"):
            raise EditError("linear work kind must be road or river")
        for name in ("x1", "y1", "x2", "y2", "width", "height", "shoulder", "priority"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise EditError(f"{name} must be a number")
            if not math.isfinite(value):
                raise EditError(f"{name} must be finite")
        if self.width <= 0:
            raise EditError("width must be positive")
        if self.shoulder < 0:
            raise EditError("shoulder must be non-negative")
        if math.hypot(self.x2 - self.x1, self.y2 - self.y1) == 0.0:
            raise EditError("road endpoints must not be identical")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.kind,
            "id": self.id,
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "width": self.width,
            "height": self.height,
            "shoulder": self.shoulder,
            "priority": self.priority,
        }

    @staticmethod
    def from_dict(data: dict[str, Any], kind: str | None = None) -> "LinearWorkOp":
        try:
            op = LinearWorkOp(
                id=data["id"],
                kind=kind or data.get("type", "road"),
                x1=data["x1"],
                y1=data["y1"],
                x2=data["x2"],
                y2=data["y2"],
                width=data["width"],
                height=data["height"],
                shoulder=data.get("shoulder", 0.0),
                priority=data.get("priority", 0.0),
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

    def weight_at(self, x: float, y: float) -> float:
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

    def applied_height(self, current: float, x: float, y: float) -> float:
        weight = self.weight_at(x, y)
        return current + weight * (self.height - current)


def RoadEditOp(
    id: int,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float,
    height: float,
    shoulder: float = 0.0,
    priority: float = 0.0,
) -> LinearWorkOp:
    return LinearWorkOp(id, "road", x1, y1, x2, y2, width, height, shoulder, priority)


def RiverEditOp(
    id: int,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float,
    height: float,
    shoulder: float = 0.0,
    priority: float = 0.0,
) -> LinearWorkOp:
    return LinearWorkOp(id, "river", x1, y1, x2, y2, width, height, shoulder, priority)


def segment_intersection(
    first: LinearWorkOp,
    second: LinearWorkOp,
) -> tuple[float, float] | None:
    rx = first.x2 - first.x1
    ry = first.y2 - first.y1
    sx = second.x2 - second.x1
    sy = second.y2 - second.y1
    denominator = rx * sy - ry * sx
    if denominator == 0.0:
        return None
    qpx = second.x1 - first.x1
    qpy = second.y1 - first.y1
    t = (qpx * sy - qpy * sx) / denominator
    u = (qpx * ry - qpy * rx) / denominator
    if not (0.0 <= t <= 1.0 and 0.0 <= u <= 1.0):
        return None
    return first.x1 + t * rx, first.y1 + t * ry


@dataclass(frozen=True)
class CrossingEditOp:
    id: int
    work_a: int
    work_b: int
    height: float
    radius: float
    state: str = "active"
    reason: str | None = None

    @property
    def type_name(self) -> str:
        return "crossing"

    def validate(self) -> None:
        if not isinstance(self.id, int) or self.id < 1:
            raise EditError("edit id must be a positive integer")
        if not isinstance(self.work_a, int) or not isinstance(self.work_b, int):
            raise EditError("crossing works must be integer edit ids")
        if self.work_a < 1 or self.work_b < 1 or self.work_a == self.work_b:
            raise EditError("crossing must reference two distinct positive work ids")
        for name in ("height", "radius"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise EditError(f"{name} must be a finite number")
        if self.radius <= 0:
            raise EditError("crossing radius must be positive")
        if self.state not in ("active", "invalidated"):
            raise EditError("crossing state must be active or invalidated")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": "crossing",
            "id": self.id,
            "work_a": self.work_a,
            "work_b": self.work_b,
            "height": self.height,
            "radius": self.radius,
            "state": self.state,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CrossingEditOp":
        try:
            op = CrossingEditOp(
                id=data["id"],
                work_a=data["work_a"],
                work_b=data["work_b"],
                height=data["height"],
                radius=data["radius"],
                state=data.get("state", "active"),
                reason=data.get("reason"),
            )
        except KeyError as exc:
            raise EditError(f"missing edit field: {exc.args[0]}") from exc
        op.validate()
        return op

    def work_ids(self) -> tuple[int, int]:
        return tuple(sorted((self.work_a, self.work_b)))

    def is_active(self, works: dict[int, LinearWorkOp]) -> bool:
        return (
            self.state == "active"
            and self.work_a in works
            and self.work_b in works
        )

    def center(self, works: dict[int, LinearWorkOp]) -> tuple[float, float] | None:
        point = segment_intersection(works[self.work_a], works[self.work_b])
        return point

    def weight_at(self, x: float, y: float, works: dict[int, LinearWorkOp]) -> float:
        center = self.center(works)
        if center is None:
            return 0.0
        distance = math.hypot(x - center[0], y - center[1])
        if distance >= self.radius:
            return 0.0
        t = distance / self.radius
        smooth = t * t * (3.0 - 2.0 * t)
        return 1.0 - smooth

    def applied_height(self, current: float, x: float, y: float, works: dict[int, LinearWorkOp]) -> float:
        weight = self.weight_at(x, y, works)
        return current + weight * (self.height - current)


def edit_from_dict(data: dict[str, Any]):
    edit_type = data.get("type", "brush")
    if edit_type == "brush":
        return EditOp.from_dict(data)
    if edit_type in ("road", "river"):
        return LinearWorkOp.from_dict(data, edit_type)
    if edit_type == "crossing":
        return CrossingEditOp.from_dict(data)
    raise EditError(f"unknown edit type: {edit_type}")
