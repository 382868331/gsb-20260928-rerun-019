from __future__ import annotations

import hashlib
import math
import struct


def _smoothstep(value: float) -> float:
    return value * value * (3.0 - 2.0 * value)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lattice_value(seed: int, x: int, y: int, octave: int) -> float:
    digest = hashlib.blake2b(
        struct.pack("<qiiB", seed, x, y, octave), digest_size=8
    ).digest()
    integer = int.from_bytes(digest, "little")
    return integer / float(1 << 64) * 2.0 - 1.0


def value_noise(seed: int, x: float, y: float, octave: int = 0) -> float:
    x0 = math.floor(x)
    y0 = math.floor(y)
    tx = _smoothstep(x - x0)
    ty = _smoothstep(y - y0)

    v00 = _lattice_value(seed, x0, y0, octave)
    v10 = _lattice_value(seed, x0 + 1, y0, octave)
    v01 = _lattice_value(seed, x0, y0 + 1, octave)
    v11 = _lattice_value(seed, x0 + 1, y0 + 1, octave)

    lower = _lerp(v00, v10, tx)
    upper = _lerp(v01, v11, tx)
    return _lerp(lower, upper, ty)


def terrain_height(
    x: float,
    y: float,
    *,
    seed: int,
    scale: float,
    octaves: int,
    persistence: float,
    lacunarity: float,
    base_height: float,
    amplitude: float,
) -> float:
    total = 0.0
    weight_sum = 0.0
    weight = 1.0
    frequency = scale

    for octave in range(octaves):
        total += weight * value_noise(seed, x / frequency, y / frequency, octave)
        weight_sum += weight
        weight *= persistence
        frequency *= lacunarity

    return base_height + amplitude * (total / weight_sum)
