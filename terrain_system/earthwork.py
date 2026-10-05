from __future__ import annotations


def surface_volume_change(
    before_chunks: dict[tuple[int, int], dict],
    after_chunks: dict[tuple[int, int], dict],
    cell_size: float,
) -> dict[str, float]:
    """Integrate raster height changes over the union of generated cells.

    Shared chunk-boundary vertices occur once in the global grid, so an overlap
    contributes only the actual surface delta, never one nominal volume per work.
    """
    if cell_size <= 0.0:
        raise ValueError("cell_size must be positive")
    before = {}
    after = {}
    resolution = None
    span = None
    for key, chunk in before_chunks.items():
        grid = chunk["permanent_heights"]
        rows = len(grid)
        if resolution is None:
            resolution = rows
            span = resolution - 1
        if rows != resolution or any(len(row) != resolution for row in grid):
            raise ValueError("all chunks must use the same grid resolution")
        for row_index, row in enumerate(grid):
            for column_index, height in enumerate(row):
                before[(key[0] * span + column_index, key[1] * span + row_index)] = height
    for key, chunk in after_chunks.items():
        if key not in before_chunks:
            continue
        grid = chunk["permanent_heights"]
        if len(grid) != resolution or any(len(row) != resolution for row in grid):
            raise ValueError("all chunks must use the same grid resolution")
        for row_index, row in enumerate(grid):
            for column_index, height in enumerate(row):
                after[(key[0] * span + column_index, key[1] * span + row_index)] = height

    cut = 0.0
    fill = 0.0
    cell_area = cell_size * cell_size
    x_values = sorted({x for x, _ in before})
    y_values = sorted({y for _, y in before})
    for x_index, next_x in zip(x_values, x_values[1:]):
        if next_x != x_index + 1:
            continue
        for y_index, next_y in zip(y_values, y_values[1:]):
            if next_y != y_index + 1:
                continue
            corners = (
                (x_index, y_index),
                (next_x, y_index),
                (x_index, next_y),
                (next_x, next_y),
            )
            if any(corner not in before for corner in corners):
                continue
            delta = (
                after[x_index, y_index]
                + after[next_x, y_index]
                + after[x_index, next_y]
                + after[next_x, next_y]
                - before[x_index, y_index]
                - before[next_x, y_index]
                - before[x_index, next_y]
                - before[next_x, next_y]
            ) / 4.0
            if delta > 0.0:
                fill += delta * cell_area
            elif delta < 0.0:
                cut -= delta * cell_area
    return {"cut": cut, "fill": fill}
