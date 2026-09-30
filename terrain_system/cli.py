from __future__ import annotations

import argparse
import json
from pathlib import Path

from .edits import EditError, EditOp, RoadEditOp, next_edit_id
from .model import ChunkKey
from .parameters import ParameterError, TerrainParameters
from .project import ProjectError, create_project, load_project


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2))


def _chunk_keys(values: list[str]) -> list[ChunkKey]:
    return [ChunkKey.parse(value) for value in values]


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return number


def _finite_float(value: str) -> float:
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError("value must be finite")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terrain", description="Deterministic chunked height terrain")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a terrain project")
    init.add_argument("path", type=Path)
    init.add_argument("--seed", type=int, required=True)
    init.add_argument("--chunk-size", type=float, required=True)
    init.add_argument("--resolution", type=int, required=True)
    init.add_argument("--scale", type=float, required=True)
    init.add_argument("--octaves", type=int, default=4)
    init.add_argument("--persistence", type=float, default=0.5)
    init.add_argument("--lacunarity", type=float, default=2.0)
    init.add_argument("--base-height", type=float, default=0.0)
    init.add_argument("--amplitude", type=float, default=10.0)
    init.add_argument("--overwrite", action="store_true")

    generate = subparsers.add_parser("generate", help="generate or regenerate chunks")
    generate.add_argument("project", type=Path)
    generate.add_argument("chunks", nargs="+", help="chunk coordinates, for example 0,0 1,0")

    edit = subparsers.add_parser("edit", help="apply a local raise or lower brush")
    edit.add_argument("project", type=Path)
    edit.add_argument("--x", type=float, required=True)
    edit.add_argument("--y", type=float, required=True)
    edit.add_argument("--radius", type=_positive_float, required=True)
    edit.add_argument("--delta", type=float, required=True, help="positive raises; negative lowers")
    edit.add_argument("--falloff", type=_positive_float, default=2.0)

    road = subparsers.add_parser("road", help="flatten and smooth a road segment")
    road.add_argument("project", type=Path)
    road.add_argument("--x1", type=float, required=True)
    road.add_argument("--y1", type=float, required=True)
    road.add_argument("--x2", type=float, required=True)
    road.add_argument("--y2", type=float, required=True)
    road.add_argument("--width", type=_positive_float, required=True)
    road.add_argument("--height", type=float, required=True)
    road.add_argument("--shoulder", type=_non_negative_float, default=0.0)

    undo = subparsers.add_parser("undo", help="remove one persistent edit and replay the rest")
    undo.add_argument("project", type=Path)
    undo.add_argument("--id", type=int)

    stitch = subparsers.add_parser("stitch", help="check generated adjacent chunk boundaries")
    stitch.add_argument("project", type=Path)
    stitch.add_argument("--tolerance", type=_finite_float, default=0.0)

    info = subparsers.add_parser("info", help="show project metadata")
    info.add_argument("project", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            parameters = TerrainParameters(
                seed=args.seed,
                chunk_size=args.chunk_size,
                resolution=args.resolution,
                scale=args.scale,
                octaves=args.octaves,
                persistence=args.persistence,
                lacunarity=args.lacunarity,
                base_height=args.base_height,
                amplitude=args.amplitude,
            )
            project = create_project(args.path, parameters, overwrite=args.overwrite)
            _print_json({"status": "created", "project": str(project.root), "parameters": parameters.to_dict()})
            return 0

        project = load_project(args.project)

        if args.command == "generate":
            keys = _chunk_keys(args.chunks)
            grids = project.generate_chunks(keys)
            _print_json({
                "status": "generated",
                "chunks": [[grid["chunk"]["x"], grid["chunk"]["y"]] for grid in grids],
            })
        elif args.command == "edit":
            edit = EditOp(
                id=next_edit_id(project.edits),
                x=args.x,
                y=args.y,
                radius=args.radius,
                delta=args.delta,
                falloff=args.falloff,
            )
            affected = project.add_edit(edit)
            _print_json({
                "status": "edited",
                "edit_id": edit.id,
                "updated_chunks": [[key.x, key.y] for key in affected],
            })
        elif args.command == "road":
            edit = RoadEditOp(
                id=next_edit_id(project.edits),
                x1=args.x1,
                y1=args.y1,
                x2=args.x2,
                y2=args.y2,
                width=args.width,
                height=args.height,
                shoulder=args.shoulder,
            )
            affected = project.add_edit(edit)
            _print_json({
                "status": "edited",
                "edit_id": edit.id,
                "kind": "road",
                "updated_chunks": [[key.x, key.y] for key in affected],
            })
        elif args.command == "undo":
            edit_id = args.id if args.id is not None else max((edit.id for edit in project.edits), default=0)
            if edit_id < 1:
                raise EditError("there are no edits to undo")
            affected = project.undo_edit(edit_id)
            _print_json({
                "status": "undone",
                "edit_id": edit_id,
                "updated_chunks": [[key.x, key.y] for key in affected],
            })
        elif args.command == "stitch":
            loaded_chunks = project.load_all_chunks()
            from .stitch import check_seams, count_adjacencies

            mismatches = check_seams(loaded_chunks, tolerance=args.tolerance)
            payload = {
                "status": "ok" if not mismatches else "cracked",
                "checked_adjacencies": count_adjacencies(loaded_chunks),
                "mismatch_count": len(mismatches),
                "mismatches": [
                    {
                        "a": list(mismatch.a),
                        "b": list(mismatch.b),
                        "index": mismatch.index,
                        "difference": mismatch.difference,
                    }
                    for mismatch in mismatches[:20]
                ],
            }
            _print_json(payload)
            return 0 if not mismatches else 2
        elif args.command == "info":
            _print_json(project.to_dict())
        else:
            parser.error("unknown command")
    except (ParameterError, EditError, ProjectError, FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
