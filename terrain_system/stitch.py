from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeamMismatch:
    a: tuple[int, int]
    b: tuple[int, int]
    index: int
    value_a: float
    value_b: float
    difference: float


def _edge_pair(chunks: dict, first: tuple[int, int], second: tuple[int, int], axis: str):
    first_data = chunks[first]["heights"]
    second_data = chunks[second]["heights"]
    resolution = len(first_data)
    if len(second_data) != resolution:
        raise ValueError("chunks being stitched must have the same resolution")

    for index in range(resolution):
        if axis == "x":
            a = first_data[index][resolution - 1]
            b = second_data[index][0]
        else:
            a = first_data[resolution - 1][index]
            b = second_data[0][index]
        difference = abs(a - b)
        if difference != 0.0:
            yield SeamMismatch(first, second, index, a, b, difference)


def check_seams(
    chunks: dict[tuple[int, int], dict],
    *,
    tolerance: float = 0.0,
) -> list[SeamMismatch]:
    mismatches = []
    for x, y in sorted(chunks):
        if (x + 1, y) in chunks:
            for mismatch in _edge_pair(chunks, (x, y), (x + 1, y), "x"):
                if mismatch.difference > tolerance:
                    mismatches.append(mismatch)
        if (x, y + 1) in chunks:
            for mismatch in _edge_pair(chunks, (x, y), (x, y + 1), "y"):
                if mismatch.difference > tolerance:
                    mismatches.append(mismatch)
    return mismatches


def count_adjacencies(chunks: dict[tuple[int, int], dict]) -> int:
    count = 0
    for x, y in chunks:
        if (x + 1, y) in chunks:
            count += 1
        if (x, y + 1) in chunks:
            count += 1
    return count
