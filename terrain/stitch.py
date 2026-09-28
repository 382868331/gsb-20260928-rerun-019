"""把多块拼成一张规则网格并导出（供实际拼接检查/后续道路植被复用）。"""

from __future__ import annotations

import os
import struct

from .chunk import write_layer_atomic  # noqa: F401  (保持存储工具同包)
from .world import TerrainWorld


class StitchedHeightmap:
    """把已生成块合成为一张网格，重复边界顶点只取一份。

    返回的 heights 按行排列；同时逐顶点核对边界，发现不一致即报错，
    避免用平均值掩盖裂缝。
    """

    def __init__(self, heights, rows, cols, origin, resolution):
        self.heights = heights
        self.rows = rows
        self.cols = cols
        self.origin = origin
        self.resolution = resolution


def stitch(world: TerrainWorld, coords=None, use_final: bool = True,
           atol: float = 1e-6) -> StitchedHeightmap:
    chunks = coords or world.generated_chunks()
    if not chunks:
        raise ValueError("没有可拼接的已生成块")
    span = world._span()
    cxs = sorted({coord[0] for coord in chunks})
    cys = sorted({coord[1] for coord in chunks})
    # 要求矩形完整覆盖，避免空洞；如只需局部可显式传 coords。
    if cxs != list(range(cxs[0], cxs[-1] + 1)) or \
       cys != list(range(cys[0], cys[-1] + 1)):
        raise ValueError("块坐标不连续，无法拼成规则矩形；请补齐中间块")
    expected = {(x, y) for x in cxs for y in cys}
    missing = expected - set(chunks)
    if missing:
        raise ValueError(f"拼接区域不完整，缺少块: {sorted(missing)}")
    loader = world.final_chunk if use_final else world.ensure_chunk
    data = {(x, y): loader(x, y) for x in cxs for y in cys}
    min_x, max_x = cxs[0], cxs[-1]
    min_y, max_y = cys[0], cys[-1]
    n = world.config.chunk_size
    cols = (max_x - min_x) * span + n
    rows = (max_y - min_y) * span + n
    heights = [0.0] * (rows * cols)
    for cy in cys:
        for cx in cxs:
            chunk = data[(cx, cy)]
            col0 = (cx - min_x) * span
            row0 = (cy - min_y) * span
            for vr in range(chunk.size):
                for vc in range(chunk.size):
                    gr, gc = row0 + vr, col0 + vc
                    value = chunk.get(vr, vc)
                    idx = gr * cols + gc
                    existing = heights[idx]
                    occupied = (vr == 0 and cy > min_y) or (vc == 0 and cx > min_x)
                    if occupied and abs(existing - value) > atol:
                        raise ValueError(
                            f"块 ({cx},{cy}) 与邻块在全局顶点 (row={gr},col={gc}) "
                            f"高度不一致: {existing} vs {value}")
                    heights[idx] = value
    origin = (min_x * span * world.config.resolution,
              min_y * span * world.config.resolution)
    return StitchedHeightmap(heights, rows, cols, origin, world.config.resolution)


def export_pgmbinary(path: str, hm: StitchedHeightmap,
                     lo: float | None = None, hi: float | None = None) -> None:
    """导出 16 位灰度 PGM（仅用于人眼抽查；系统真源始终是分块 f32 数据）。"""
    lo = min(hm.heights) if lo is None else lo
    hi = max(hm.heights) if hi is None else hi
    span = (hi - lo) or 1.0
    with open(path, "wb") as fh:
        fh.write(f"P5\n{hm.cols} {hm.rows}\n65535\n".encode("ascii"))
        for v in hm.heights:
            q = int(round(min(max((v - lo) / span, 0.0), 1.0) * 65535))
            fh.write(struct.pack(">H", q))
