from __future__ import annotations

from dataclasses import dataclass

from .construction import ConstructionSchedule
from .edits import LinearWorkOp, TemporaryRampOp
from .model import ChunkKey, generate_grid
from .parameters import TerrainParameters


@dataclass(frozen=True)
class SpanVolume:
    cut: float
    fill: float
    cells: int


def _grid_cell_area(params: TerrainParameters) -> float:
    step = params.chunk_size / (params.resolution - 1)
    return step * step


def span_masks(work: LinearWorkOp, params: TerrainParameters, key: ChunkKey, start: float, end: float):
    """Per-node fractional coverage of a longitudinal span and cell area weights.

    Cut and fill are always integrated from realised height differences of the
    terrain itself (base vs constructed surface). Where a junction, river or
    temporary ramp occupies the same space as a road, the node simply carries
    one final height, so nominal volumes of several works can never be added
    twice.
    """
    step = params.chunk_size / (params.resolution - 1)
    resolution = params.resolution
    coverage = [[0.0] * resolution for _ in range(resolution)]
    for row in range(resolution):
        for column in range(resolution):
            x = key.x * params.chunk_size + column * step
            y = key.y * params.chunk_size + row * step
            projection = min(max(work.projection_at(x, y), 0.0), 1.0)
            if start - 1e-12 <= projection <= end + 1e-12:
                coverage[row][column] = work.core_weight_at(x, y)
    return coverage, step * step


def integrate_span(
    params: TerrainParameters,
    key: ChunkKey,
    work: LinearWorkOp,
    edits: list,
    schedule: ConstructionSchedule,
    stage_before: int,
    stage_after: int,
    start: float,
    end: float,
) -> SpanVolume:
    masks, area = span_masks(work, params, key, start, end)
    return integrate_masks(params, key, masks, edits, schedule, stage_before, stage_after)


def ramp_masks(ramp: TemporaryRampOp, params: TerrainParameters, key: ChunkKey):
    step = params.chunk_size / (params.resolution - 1)
    resolution = params.resolution
    coverage = [[0.0] * resolution for _ in range(resolution)]
    for row in range(resolution):
        for column in range(resolution):
            x = key.x * params.chunk_size + column * step
            y = key.y * params.chunk_size + row * step
            coverage[row][column] = ramp.core_weight_at(x, y)
    return coverage, step * step


def integrate_masks(
    params: TerrainParameters,
    key: ChunkKey,
    masks: list[list[float]],
    edits: list,
    schedule: ConstructionSchedule,
    stage_before: int,
    stage_after: int,
) -> SpanVolume:
    before = generate_grid(params, key, edits, schedule, stage_before)
    after = generate_grid(params, key, edits, schedule, stage_after)
    area = _grid_cell_area(params)
    # Each cell volume is the area times the mean of mask-weighted node height
    # differences. Shared nodes make chunk-boundary volumes independent of
    # chunk generation order.
    cut = 0.0
    fill = 0.0
    cells = 0
    rows = params.resolution - 1
    for row in range(rows):
        for column in range(rows):
            corners = ((row, column), (row, column + 1), (row + 1, column), (row + 1, column + 1))
            weighted = []
            for r, c in corners:
                old = before["heights"][r][c]
                new = after["heights"][r][c]
                weighted.append((masks[r][c], old, new))
            if all(item[0] <= 0.0 for item in weighted):
                continue
            delta_cut = sum(max(old - new, 0.0) * mask for mask, old, new in weighted) / 4.0
            delta_fill = sum(max(new - old, 0.0) * mask for mask, old, new in weighted) / 4.0
            cut += delta_cut * area
            fill += delta_fill * area
            cells += 1
    return SpanVolume(cut=cut, fill=fill, cells=cells)


def integrate_ramp(
    *,
    params: TerrainParameters,
    key: ChunkKey,
    ramp: TemporaryRampOp,
    edits: list,
    schedule: ConstructionSchedule,
    active_stage: int,
    recovery_stage: int,
) -> SpanVolume:
    masks, _ = ramp_masks(ramp, params, key)
    return integrate_masks(params, key, masks, edits, schedule, active_stage, recovery_stage)
