from __future__ import annotations

from dataclasses import dataclass

from .edits import EditOp, compose_edits
from .noise import terrain_height
from .parameters import TerrainParameters


@dataclass(frozen=True)
class ChunkKey:
    x: int
    y: int

    def __str__(self) -> str:
        return f"{self.x},{self.y}"

    @staticmethod
    def parse(value: str) -> "ChunkKey":
        parts = value.split(",")
        if len(parts) != 2:
            raise ValueError(f"invalid chunk key: {value}")
        return ChunkKey(int(parts[0]), int(parts[1]))

    @property
    def filename(self) -> str:
        sign_x = "m" if self.x < 0 else "p"
        sign_y = "m" if self.y < 0 else "p"
        return f"{sign_x}{abs(self.x)}_{sign_y}{abs(self.y)}.json"


def validate_chunk_key(key: ChunkKey) -> None:
    for value in (key.x, key.y):
        if not isinstance(value, int) or not -(2**31) <= value < 2**31:
            raise ValueError("chunk coordinates must be signed 32-bit integers")


def world_point(params: TerrainParameters, key: ChunkKey, column: int, row: int) -> tuple[float, float]:
    step = params.chunk_size / (params.resolution - 1)
    x = key.x * params.chunk_size + column * step
    y = key.y * params.chunk_size + row * step
    return x, y


def base_height_at(params: TerrainParameters, x: float, y: float) -> float:
    return terrain_height(
        x,
        y,
        seed=params.seed,
        scale=params.scale,
        octaves=params.octaves,
        persistence=params.persistence,
        lacunarity=params.lacunarity,
        base_height=params.base_height,
        amplitude=params.amplitude,
    )


def generate_grid(params: TerrainParameters, key: ChunkKey, edits: list[EditOp]) -> dict:
    validate_chunk_key(key)
    ordered_edits = sorted(edits, key=lambda edit: edit.id)
    base = []
    heights = []
    min_height = float("inf")
    max_height = float("-inf")

    for row in range(params.resolution):
        base_row = []
        height_row = []
        for column in range(params.resolution):
            x, y = world_point(params, key, column, row)
            generated = base_height_at(params, x, y)
            height = compose_edits(lambda px, py: base_height_at(params, px, py), x, y, ordered_edits)
            base_row.append(generated)
            height_row.append(height)
            min_height = min(min_height, height)
            max_height = max(max_height, height)
        base.append(base_row)
        heights.append(height_row)

    return {
        "version": 1,
        "chunk": {"x": key.x, "y": key.y},
        "origin": {
            "x": key.x * params.chunk_size,
            "y": key.y * params.chunk_size,
        },
        "resolution": params.resolution,
        "cell_size": params.chunk_size / (params.resolution - 1),
        "base_heights": base,
        "heights": heights,
        "min_height": min_height,
        "max_height": max_height,
    }


def chunk_affected_by_edit(params: TerrainParameters, key: ChunkKey, edit: EditOp) -> bool:
    min_x = key.x * params.chunk_size
    min_y = key.y * params.chunk_size
    max_x = min_x + params.chunk_size
    max_y = min_y + params.chunk_size
    edit_min_x, edit_min_y, edit_max_x, edit_max_y = edit.influence_bounds()
    return edit_max_x > min_x and edit_min_x < max_x and edit_max_y > min_y and edit_min_y < max_y
