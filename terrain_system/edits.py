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


def apply_edits(x: float, y: float, edits: list[EditOp]) -> float:
    return sum(edit.displacement_at(x, y) for edit in edits)


def next_edit_id(edits: list[EditOp]) -> int:
    return max((edit.id for edit in edits), default=0) + 1
