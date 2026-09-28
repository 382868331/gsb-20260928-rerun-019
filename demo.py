"""端到端演示：分块生成（乱序）→ 局部编辑 → 重生成 → 拼接查裂缝。

运行: python demo.py [输出目录]
"""

from __future__ import annotations

import os
import shutil
import sys

from terrain.config import TerrainConfig
from terrain.stitch import export_pgmbinary, stitch
from terrain.world import TerrainWorld


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "demo_out"
    project = os.path.join(out, "terrain_project")
    if os.path.exists(project):
        shutil.rmtree(project)
    os.makedirs(out, exist_ok=True)

    cfg = TerrainConfig(seed=20260929, chunk_size=48, resolution=1.0,
                        noise_scale=20.0, octaves=4, height_scale=55.0,
                        min_height=0.0, max_height=120.0)
    world = TerrainWorld.create(project, cfg)
    print(f"[1] 工程已创建: {project}")

    # 刻意乱序生成，且包含负坐标块。
    order = [(1, 0), (0, -1), (0, 0), (-1, 0), (1, -1), (0, 1),
             (-1, -1), (1, 1), (-1, 1)]
    for c in order:
        world.generate_chunk(*c)
    print(f"[2] 已乱序生成 {len(order)} 块（含负坐标）")

    seams = world.check_seams(use_final=False, atol=0.0)
    print(f"[3] 基础层裂缝检查: {'通过（零误差）' if not seams else seams}")
    assert not seams

    # 局部人工抬高与压低。
    world.edit_brush(10.0, 10.0, radius=6.0, strength=15.0)
    world.edit_brush(-20.0, 30.0, radius=8.0, strength=-20.0)
    print("[4] 已写入两处局部编辑（抬高山丘 / 压低洼地）")

    # 模拟“重启后补块 + 重算未编辑部分”：重新打开工程。
    world = TerrainWorld.open(project)
    world.generate_chunk(0, 2)   # 重启后补的新块
    world.generate_chunk(-1, 1)  # 旧块重算基础层
    seams = world.check_seams(use_final=True, atol=0.0)
    print("[5] 补块/重生成后合成高度裂缝检查:",
          "通过（零误差）" if not seams else f"发现 {seams}")

    # 拼接 3x3 区域导出。
    grid = [(cx, cy) for cx in (-1, 0, 1) for cy in (-1, 0, 1)]
    hm = stitch(world, coords=grid, use_final=True, atol=0.0)
    pgm_path = os.path.join(out, "stitched_3x3.pgm")
    export_pgmbinary(pgm_path, hm, lo=cfg.min_height, hi=cfg.max_height)
    print(f"[6] 拼接网格 {hm.cols}x{hm.rows} 顶点，抽查图: {pgm_path}")
    print("    注：PGM 仅用于人眼抽查，系统真源为分块 base/edits f32 数据。")

    # 重启确定性的最终证据：同参数新工程乱序重算，逐顶点一致。
    mirror = os.path.join(out, "mirror_project")
    if os.path.exists(mirror):
        shutil.rmtree(mirror)
    w2 = TerrainWorld.create(mirror, cfg)
    for c in reversed(order + [(0, 2)]):
        w2.generate_chunk(*c)
    from terrain.chunk import read_layer
    for c in order + [(0, 2)]:
        a = read_layer(world._base_path(*c), cfg.chunk_size).values
        b = read_layer(w2._base_path(*c), cfg.chunk_size).values
        assert a == b, f"块 {c} 在两次独立运行间不一致"
    print("[7] 两个独立工程/不同生成顺序的基础层逐顶点完全一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
