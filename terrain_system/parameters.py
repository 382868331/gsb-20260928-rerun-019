from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any


class ParameterError(ValueError):
    pass


@dataclass(frozen=True)
class TerrainParameters:
    seed: int
    chunk_size: float
    resolution: int
    scale: float
    octaves: int
    persistence: float
    lacunarity: float
    base_height: float
    amplitude: float

    @staticmethod
    def from_mapping(data: dict[str, Any]) -> "TerrainParameters":
        required = {
            "seed": int,
            "chunk_size": (int, float),
            "resolution": int,
            "scale": (int, float),
            "octaves": int,
            "persistence": (int, float),
            "lacunarity": (int, float),
            "base_height": (int, float),
            "amplitude": (int, float),
        }
        values: dict[str, Any] = {}
        for name, expected in required.items():
            if name not in data:
                raise ParameterError(f"missing parameter: {name}")
            value = data[name]
            if name == "seed" and isinstance(value, bool):
                raise ParameterError("seed must be an integer")
            if not isinstance(value, expected):
                raise ParameterError(f"{name} has an invalid type")
            values[name] = value

        params = TerrainParameters(**values)
        params.validate()
        return params

    def validate(self) -> None:
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ParameterError("seed must be an integer")
        if not -(2**63) <= self.seed < 2**63:
            raise ParameterError("seed must be a signed 64-bit integer")
        if not math.isfinite(self.chunk_size) or self.chunk_size <= 0:
            raise ParameterError("chunk_size must be a positive finite number")
        if self.resolution < 2:
            raise ParameterError("resolution must be at least 2")
        if self.resolution > 4096:
            raise ParameterError("resolution must be at most 4096")
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise ParameterError("scale must be a positive finite number")
        if not 1 <= self.octaves <= 12:
            raise ParameterError("octaves must be between 1 and 12")
        if not math.isfinite(self.persistence) or self.persistence <= 0:
            raise ParameterError("persistence must be a positive finite number")
        if not math.isfinite(self.lacunarity) or self.lacunarity <= 1:
            raise ParameterError("lacunarity must be finite and greater than 1")
        if not math.isfinite(self.base_height):
            raise ParameterError("base_height must be finite")
        if not math.isfinite(self.amplitude) or self.amplitude < 0:
            raise ParameterError("amplitude must be a non-negative finite number")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
