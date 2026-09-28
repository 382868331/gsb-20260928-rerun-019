"""命令行入口：工程创建、参数更新、分块生成、局部编辑、拼接检查。"""

from __future__ import annotations

import argparse
import json
import sys

from .config import ConfigError, TerrainConfig
from .world import TerrainWorld


def _coord_pair(text: str) -> tuple[int, int]:
    try:
        x, y = text.split(",", 1)
        return int(x), int(y)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("坐标格式应为 cx,cy（如 0,1）") from exc


def cmd_init(args) -> int:
    cfg = TerrainConfig(
        seed=args.seed, chunk_size=args.chunk_size,
        resolution=args.resolution, height_scale=args.height_scale,
        noise_scale=args.noise_scale, octaves=args.octaves,
        lacunarity=args.lacunarity, gain=args.gain,
        min_height=args.min_height, max_height=args.max_height)
    try:
        TerrainWorld.create(args.project, cfg)
    except (ConfigError, OSError) as exc:
        print(f"工程创建失败，未改动磁盘: {exc}", file=sys.stderr)
        return 2
    print(f"已创建工程 {args.project}（seed={cfg.seed}, chunk_size={cfg.chunk_size}）")
    return 0


def cmd_config(args) -> int:
    world = TerrainWorld.open(args.project)
    changes = {}
    for key in ("seed", "chunk_size", "resolution", "height_scale",
                "noise_scale", "octaves", "lacunarity", "gain",
                "min_height", "max_height"):
        value = getattr(args, key)
        if value is not None:
            changes[key] = value
    if not changes:
        print(json.dumps(world.config.to_dict(), indent=2, sort_keys=True))
        return 0
    old = world.config.to_dict()
    try:
        world.update_config(**changes)
    except ConfigError as exc:
        print(f"参数不合法，旧工程已保留: {exc}", file=sys.stderr)
        return 2
    print(f"参数已更新: {sorted(changes)}")
    print("提示: 已生成块的基础层仍是旧参数结果，可用 regen 重算；编辑层不受影响。")
    return 0


def cmd_generate(args) -> int:
    world = TerrainWorld.open(args.project)
    coords = [_coord_pair(c) for c in args.chunks] if args.chunks else [(0, 0)]
    for cx, cy in coords:
        world.generate_chunk(cx, cy)
        print(f"生成块 ({cx},{cy})")
    return 0


def cmd_regen(args) -> int:
    world = TerrainWorld.open(args.project)
    if args.all:
        coords = world.generated_chunks()
    else:
        coords = [_coord_pair(c) for c in args.chunks]
    for cx, cy in coords:
        world.generate_chunk(cx, cy)
        print(f"重新生成块 ({cx},{cy})（编辑层保留）")
    if not coords:
        print("没有可重新生成的块")
    return 0


def cmd_edit(args) -> int:
    world = TerrainWorld.open(args.project)
    try:
        info = world.edit_brush(args.x, args.y, args.radius, args.strength,
                                falloff=args.falloff)
    except ValueError as exc:
        print(f"编辑失败: {exc}", file=sys.stderr)
        return 2
    print(f"编辑完成: 影响顶点 {info['affected_vertices']} 个，"
          f"涉及块 {info['chunks']}")
    return 0


def cmd_check(args) -> int:
    world = TerrainWorld.open(args.project)
    reports = world.check_seams(use_final=not args.base_only, atol=args.atol)
    if not reports:
        print("拼接检查通过：所有相邻块边界高度一致，无裂缝。")
        return 0
    print(f"发现 {len(reports)} 处边界不一致:", file=sys.stderr)
    for rep in reports:
        print(f"  {rep.pair[0]} <-> {rep.pair[1]}: "
              f"最大差 {rep.max_abs_diff:.6g}，超差点 {rep.mismatches}",
              file=sys.stderr)
    return 1


def cmd_list(args) -> int:
    world = TerrainWorld.open(args.project)
    chunks = world.generated_chunks()
    print(json.dumps({"generated_chunks": chunks, "count": len(chunks)},
                     ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terrain",
                                   description="确定性可分块程序化地形系统")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="创建地形工程")
    p.add_argument("project")
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--chunk-size", type=int, default=64)
    p.add_argument("--resolution", type=float, default=1.0)
    p.add_argument("--height-scale", type=float, default=40.0)
    p.add_argument("--noise-scale", type=float, default=32.0)
    p.add_argument("--octaves", type=int, default=4)
    p.add_argument("--lacunarity", type=float, default=2.0)
    p.add_argument("--gain", type=float, default=0.5)
    p.add_argument("--min-height", type=float, default=0.0)
    p.add_argument("--max-height", type=float, default=100.0)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("config", help="查看/更新参数（非法参数保留旧工程）")
    p.add_argument("project")
    for key, typ in (("seed", int), ("chunk_size", int), ("resolution", float),
                     ("height_scale", float), ("noise_scale", float),
                     ("octaves", int), ("lacunarity", float), ("gain", float),
                     ("min_height", float), ("max_height", float)):
        p.add_argument("--" + key.replace("_", "-"), type=typ, default=None,
                       dest=key)
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("generate", help="生成一块或多块（顺序无关）")
    p.add_argument("project")
    p.add_argument("chunks", nargs="*", help="块坐标，如 0,0 1,0 -1,2")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("regen", help="重算基础层（编辑层保留）")
    p.add_argument("project")
    p.add_argument("chunks", nargs="*", help="块坐标；配合 --all 重算全部")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_regen)

    p = sub.add_parser("edit", help="局部抬高/压低（写编辑层）")
    p.add_argument("project")
    p.add_argument("--x", type=float, required=True, help="世界 X（米）")
    p.add_argument("--y", type=float, required=True, help="世界 Y（米）")
    p.add_argument("--radius", type=float, required=True)
    p.add_argument("--strength", type=float, required=True,
                   help="正数抬高，负数压低")
    p.add_argument("--falloff", choices=("smooth", "constant"),
                   default="smooth")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("check", help="拼接多块检查裂缝")
    p.add_argument("project")
    p.add_argument("--base-only", action="store_true",
                   help="只检查基础层（默认检查 base+edit 合成高度）")
    p.add_argument("--atol", type=float, default=1e-6)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("list", help="列出已生成块")
    p.add_argument("project")
    p.set_defaults(func=cmd_list)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
