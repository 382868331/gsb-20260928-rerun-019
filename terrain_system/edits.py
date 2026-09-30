from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable


class EditError(ValueError):
    pass


@dataclass(frozen=True)
class EditOp:
    id: int
    kind: str
    params: dict[str, float]

    KINDS = ("raise", "road")

    @classmethod
    def raise_op(cls, id: int, x: float, y: float, radius: float, delta: float,
                falloff: float = 2.0) -> "EditOp":
        return cls(id, "raise", {"x": float(x), "y": float(y), "radius": float(radius),
                                 "delta": float(delta), "falloff": float(falloff)})

    @classmethod
    def road_op(cls, id: int, x1: float, y1: float, x2: float, y2: float,
                half_width: float, shoulder: float = 0.0) -> "EditOp":
        return cls(id, "road", {"x1": float(x1), "y1": float(y1), "x2": float(x2),
                                "y2": float(y2), "half_width": float(half_width),
                                "shoulder": float(shoulder)})

    def validate(self) -> None:
        if not isinstance(self.id, int) or self.id < 1:
            raise EditError("edit id must be a positive integer")
        if self.kind not in self.KINDS:
            raise EditError(f"unknown edit kind: {self.kind}")
        if not isinstance(self.params, dict):
            raise EditError("edit params must be a mapping")
        for name, value in self.params.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EditError(f"{name} must be a number")
            if not math.isfinite(value):
                raise EditError(f"{name} must be finite")
        if self.kind == "raise":
            required = ("x", "y", "radius", "delta", "falloff")
            if set(self.params) != set(required):
                raise EditError("raise edit must contain exactly x, y, radius, delta, falloff")
            if self.params["radius"] <= 0:
                raise EditError("radius must be positive")
            if self.params["falloff"] <= 0:
                raise EditError("falloff must be positive")
        else:
            required = ("x1", "y1", "x2", "y2", "half_width", "shoulder")
            if set(self.params) != set(required):
                raise EditError("road edit must contain exactly x1, y1, x2, y2, half_width, shoulder")
            if self.params["half_width"] <= 0:
                raise EditError("half_width must be positive")
            if self.params["shoulder"] < 0:
                raise EditError("shoulder must be non-negative")
            if self.params["x1"] == self.params["x2"] and self.params["y1"] == self.params["y2"]:
                raise EditError("road endpoints must differ")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "params": dict(self.params),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EditOp":
        try:
            if "kind" in data:
                op = EditOp(id=data["id"], kind=data["kind"], params=dict(data["params"]))
            else:
                op = EditOp.raise_op(
                    id=data["id"],
                    x=data["x"],
                    y=data["y"],
                    radius=data["radius"],
                    delta=data["delta"],
                    falloff=data.get("falloff", 2.0),
                )
        except KeyError as exc:
            raise EditError(f"missing edit field: {exc.args[0]}") from exc
        except TypeError as exc:
            raise EditError("invalid edit payload") from exc
        op.validate()
        return op

    @staticmethod
    def _smoothstep(value: float) -> float:
        return value * value * (3.0 - 2.0 * value)

    def _raise_displacement(self, x: float, y: float) -> float:
        p = self.params
        distance = math.hypot(x - p["x"], y - p["y"])
        if distance >= p["radius"]:
            return 0.0
        strength = 1.0 - distance / p["radius"]
        return p["delta"] * (strength ** p["falloff"])

    def _road_geometry(self, x: float, y: float) -> tuple[float, float, float]:
        p = self.params
        dx = p["x2"] - p["x1"]
        dy = p["y2"] - p["y1"]
        length = math.hypot(dx, dy)
        ux, uy = dx / length, dy / length
        px, py = x - p["x1"], y - p["y1"]
        along = px * ux + py * uy
        across = abs(px * uy - py * ux)
        return along, across, length

    def road_terms(self, base_height_fn: Callable[[float, float], float],
                   x: float, y: float) -> tuple[float, float]:
        """Return (weighted target contribution, weight) of this road edit."""
        p = self.params
        along, across, length = self._road_geometry(x, y)
        half = p["half_width"]
        total = half + p["shoulder"]
        if across >= total or along < 0.0 or along > length:
            return 0.0, 0.0
        if across <= half:
            weight = 1.0
        elif p["shoulder"] > 0.0:
            t = (total - across) / p["shoulder"]
            weight = self._smoothstep(min(max(t, 0.0), 1.0))
        else:
            weight = 0.0
        if weight <= 0.0:
            return 0.0, 0.0
        center_x = p["x1"] + along * (p["x2"] - p["x1"]) / length
        center_y = p["y1"] + along * (p["y2"] - p["y1"]) / length
        return weight * base_height_fn(center_x, center_y), weight

    def influence_bounds(self) -> tuple[float, float, float, float]:
        """Axis-aligned bounding box (min_x, min_y, max_x, max_y) of the edit's reach."""
        p = self.params
        if self.kind == "raise":
            radius = p["radius"]
            return p["x"] - radius, p["y"] - radius, p["x"] + radius, p["y"] + radius
        margin = p["half_width"] + p["shoulder"]
        return (
            min(p["x1"], p["x2"]) - margin,
            min(p["y1"], p["y2"]) - margin,
            max(p["x1"], p["x2"]) + margin,
            max(p["y1"], p["y2"]) + margin,
        )


def compose_edits(base_height_fn: Callable[[float, float], float], x: float, y: float,
                  edits: list[EditOp]) -> float:
    """Deterministic world-space composition of all edits.

    Raise/lower edits add displacement. Road edits flatten toward the unedited
    base centerline height; overlapping roads merge by a weighted average that
    is independent of edit order. Raises always stack on top of road flattening.
    """
    weighted = 0.0
    weight_sum = 0.0
    displacement = 0.0
    for edit in sorted(edits, key=lambda item: item.id):
        if edit.kind == "road":
            contribution, weight = edit.road_terms(base_height_fn, x, y)
            weighted += contribution
            weight_sum += weight
        else:
            displacement += edit._raise_displacement(x, y)
    if weight_sum > 0.0:
        return weighted / weight_sum + displacement
    return base_height_fn(x, y) + displacement


def next_edit_id(edits: list[EditOp]) -> int:
    return max((edit.id for edit in edits), default=0) + 1

