from __future__ import annotations

from dataclasses import dataclass
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


def apply_edits(base: float, x: float, y: float, edits: list["EditOp | RoadEditOp"]) -> float:
    height = base
    for edit in sorted(edits, key=lambda item: item.id):
        height = edit.applied_height(height, x, y)
    return height


def next_edit_id(edits: list["EditOp | RoadEditOp"]) -> int:
    return max((edit.id for edit in edits), default=0) + 1


@dataclass(frozen=True)
class RoadEditOp:
    id: int
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    height: float
    shoulder: float = 0.0

    @property
    def type_name(self) -> str:
        return "road"

    def validate(self) -> None:
        if not isinstance(self.id, int) or self.id < 1:
            raise EditError("edit id must be a positive integer")
        for name in ("x1", "y1", "x2", "y2", "width", "height", "shoulder"):
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
            "type": "road",
            "id": self.id,
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "width": self.width,
            "height": self.height,
            "shoulder": self.shoulder,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RoadEditOp":
        try:
            op = RoadEditOp(
                id=data["id"],
                x1=data["x1"],
                y1=data["y1"],
                x2=data["x2"],
                y2=data["y2"],
                width=data["width"],
                height=data["height"],
                shoulder=data.get("shoulder", 0.0),
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


def edit_from_dict(data: dict[str, Any]) -> "EditOp | RoadEditOp":
    edit_type = data.get("type", "brush")
    if edit_type == "brush":
        return EditOp.from_dict(data)
    if edit_type == "road":
        return RoadEditOp.from_dict(data)
    raise EditError(f"unknown edit type: {edit_type}")
