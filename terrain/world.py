"""地形工程：分块坐标、生成、局部编辑与拼接校验。

分块采用共享边界顶点：块 (cx,cy) 的全局顶点原点为
(cx*(chunk_size-1), cy*(chunk_size-1))，因此相邻块边缘顶点
落在同一世界坐标，采样同一确定性噪声函数，高度严格一致。
"""

from __future__ import annotations

import json
import math
import os
import struct
from dataclasses import dataclass

from .chunk import ChunkData, read_layer, write_layer_atomic
from .config import ConfigError, TerrainConfig
from .noise import TerrainNoise

CONFIG_NAME = "terrain.json"
BASE_DIR = "base"
EDIT_DIR = "edits"


@dataclass(frozen=True)
class SeamReport:
    pair: tuple
    max_abs_diff: float
    mismatches: int


class TerrainWorld:
    def __init__(self, root: str, config: TerrainConfig | None = None):
        self.root = root
        self.config_path = os.path.join(root, CONFIG_NAME)
        self.base_dir = os.path.join(root, BASE_DIR)
        self.edit_dir = os.path.join(root, EDIT_DIR)
        if config is None:
            self.config = TerrainConfig.read(self.config_path)
        else:
            self.config = config

    # ---------- 工程生命周期 ----------

    @classmethod
    def create(cls, root: str, config: TerrainConfig) -> "TerrainWorld":
        """创建工程；参数不合法时抛 ConfigError，磁盘不发生任何改动。"""
        config.validate()
        os.makedirs(root, exist_ok=True)
        os.makedirs(os.path.join(root, BASE_DIR), exist_ok=True)
        os.makedirs(os.path.join(root, EDIT_DIR), exist_ok=True)
        world = cls(root, config)
        config.write_atomic(world.config_path)
        return world

    @classmethod
    def open(cls, root: str) -> "TerrainWorld":
        return cls(root)

    def update_config(self, **changes) -> None:
        """校验通过才原子替换参数；失败时旧工程（含旧参数）保持不变。"""
        new_config = TerrainConfig(**{**self.config.to_dict(), **changes})
        new_config.validate()
        new_config.write_atomic(self.config_path)
        self.config = new_config

    # ---------- 坐标 ----------

    def _span(self) -> int:
        return self.config.chunk_size - 1

    def world_xy(self, cx: int, cy: int, row: int, col: int) -> tuple[float, float]:
        s = self._span()
        res = self.config.resolution
        return ((cx * s + col) * res, (cy * s + row) * res)

    def _base_height(self, noise: TerrainNoise, x: float, y: float) -> float:
        n = noise.sample(x, y) * 0.5 + 0.5  # -> [0,1]
        h = n * self.config.height_scale
        return min(max(h, self.config.min_height), self.config.max_height)

    # ---------- 生成（基础层） ----------

    def generate_chunk(self, cx: int, cy: int) -> ChunkData:
        """生成并落盘某块的基础层；与调用顺序、历史块、进程均无关。"""
        cfg = self.config
        noise = TerrainNoise(cfg.seed, cfg.noise_scale, cfg.octaves,
                             cfg.lacunarity, cfg.gain)
        n = cfg.chunk_size
        chunk = ChunkData(n)
        for row in range(n):
            for col in range(n):
                x, y = self.world_xy(cx, cy, row, col)
                chunk.set(row, col, self._base_height(noise, x, y))
        os.makedirs(self.base_dir, exist_ok=True)
        write_layer_atomic(self._base_path(cx, cy), chunk)
        return chunk

    def ensure_chunk(self, cx: int, cy: int) -> ChunkData:
        path = self._base_path(cx, cy)
        if os.path.exists(path):
            return read_layer(path, self.config.chunk_size)
        return self.generate_chunk(cx, cy)

    def generate_many(self, coords) -> list:
        return [self.generate_chunk(cx, cy) for cx, cy in coords]

    # ---------- 编辑层（局部人工抬高/压低） ----------

    def edit_brush(self, world_x: float, world_y: float, radius: float,
                   strength: float, falloff: str = "smooth") -> dict:
        """在世界坐标处施加圆形编辑刷。

        strength>0 抬高，<0 压低；radius 为世界单位。只写编辑层，
        基础层不变；返回实际受影响顶点数（用于日志/测试）。
        """
        if radius <= 0:
            raise ValueError("radius 必须为正数")
        cfg = self.config
        n, s, res = cfg.chunk_size, self._span(), cfg.resolution
        gx = world_x / res
        gy = world_y / res
        r = radius / res
        s = self._span()
        r_min = math.floor(gx - r)
        r_max = math.ceil(gx + r)
        touched_chunks: dict[tuple, list] = {}
        affected = 0
        for gvx in range(r_min, r_max + 1):
            for gvy in range(math.floor(gy - r), math.ceil(gy + r) + 1):
                dist = math.hypot(gx - gvx, gy - gvy)
                if dist > r:
                    continue
                t = 0.0 if r == 0 else dist / r
                if falloff == "constant":
                    weight = 1.0
                else:
                    weight = 1.0 - t * t * (3.0 - 2.0 * t)  # smoothstep
                # 负全局坐标用 floor 除法定块号，保证块覆盖全平面。
                cx, col = divmod(gvx, s)
                cy, row = divmod(gvy, s)
                # 规范归属：共享边界顶点由“较小坐标”的块拥有编辑权。
                # col==0 的顶点属于左边块 (cx-1) 的右边界；
                # row==0 的顶点属于下边块 (cy-1) 的上边界。
                if col == 0 and gvx != 0:
                    cx, col = cx - 1, s
                if row == 0 and gvy != 0:
                    cy, row = cy - 1, s
                touched_chunks.setdefault((cx, cy), []).append(
                    (row, col, strength * weight))
                affected += 1
        os.makedirs(self.edit_dir, exist_ok=True)
        for (cx, cy), deltas in touched_chunks.items():
            self.ensure_chunk(cx, cy)  # 编辑前先有基础层
            path = self._edit_path(cx, cy)
            edit = (read_layer(path, n) if os.path.exists(path) else ChunkData(n))
            for row, col, delta in deltas:
                edit.set(row, col, edit.get(row, col) + delta)
            write_layer_atomic(path, edit)
        return {"affected_vertices": affected, "chunks": sorted(touched_chunks)}

    # ---------- 合成读取 ----------

    def height_at(self, gx: int, gy: int) -> float:
        """全局顶点 (gx,gy) 的最终高度；共享边界顶点按规范归属取编辑。

        顶点网格规则：全局顶点 G 落在块 (G//s) 的局部 G%s 列，
        局部列 0 的顶点与左邻块的右边界是同一物理顶点，其编辑由
        左邻块（较小坐标块）拥有。行方向同理。缺失的规范块会按需生成。
        """
        s = self._span()
        cx, col = divmod(gx, s)
        cy, row = divmod(gy, s)
        if col == 0 and gx != 0:
            cx, col = cx - 1, s
        if row == 0 and gy != 0:
            cy, row = cy - 1, s
        n = self.config.chunk_size
        base = self.ensure_chunk(cx, cy)
        edit_path = self._edit_path(cx, cy)
        edit = read_layer(edit_path, n) if os.path.exists(edit_path) else ChunkData(n)
        lo, hi = self.config.min_height, self.config.max_height
        value = base.get(row, col) + edit.get(row, col)
        value = min(max(value, lo), hi)
        return struct.unpack("<f", struct.pack("<f", value))[0]

    def final_chunk(self, cx: int, cy: int) -> ChunkData:
        """块的合成高度；边界顶点的编辑按规范归属解析。"""
        n = self.config.chunk_size
        merged = ChunkData(n)
        s = self._span()
        for row in range(n):
            for col in range(n):
                merged.set(row, col, self.height_at(cx * s + col, cy * s + row))
        return merged

    def generated_chunks(self) -> list[tuple[int, int]]:
        if not os.path.isdir(self.base_dir):
            return []
        out = []
        for name in os.listdir(self.base_dir):
            if name.endswith(".f32"):
                cx, cy = name[:-4].split("_")
                out.append((int(cx), int(cy)))
        return sorted(out)

    # ---------- 拼接校验 ----------

    def check_seams(self, use_final: bool = True, atol: float = 1e-6) -> list[SeamReport]:
        """对已生成块检查水平/竖直相邻边界，返回所有超差报告。"""
        loader = self.final_chunk if use_final else (
            lambda cx, cy: read_layer(self._base_path(cx, cy),
                                      self.config.chunk_size))
        have = set(self.generated_chunks())
        reports: list[SeamReport] = []
        n = self.config.chunk_size
        for cx, cy in sorted(have):
            for dcx, dcy, edge_a, edge_b in (
                (1, 0, ("col", n - 1), ("col", 0)),
                (0, 1, ("row", n - 1), ("row", 0)),
            ):
                neighbor = (cx + dcx, cy + dcy)
                if neighbor not in have:
                    continue
                a = loader(cx, cy)
                b = loader(*neighbor)
                max_diff = 0.0
                mismatches = 0
                for k in range(n):
                    if edge_a[0] == "col":
                        va = a.get(k, edge_a[1])
                        vb = b.get(k, edge_b[1])
                    else:
                        va = a.get(edge_a[1], k)
                        vb = b.get(edge_b[1], k)
                    d = abs(va - vb)
                    if d > atol:
                        mismatches += 1
                        max_diff = max(max_diff, d)
                if mismatches:
                    reports.append(SeamReport(((cx, cy), neighbor), max_diff, mismatches))
        return reports

    # ---------- 路径 ----------

    @staticmethod
    def _layer_name(cx: int, cy: int) -> str:
        return f"{cx}_{cy}.f32"

    def _base_path(self, cx: int, cy: int) -> str:
        return os.path.join(self.base_dir, self._layer_name(cx, cy))

    def _edit_path(self, cx: int, cy: int) -> str:
        return os.path.join(self.edit_dir, self._layer_name(cx, cy))
