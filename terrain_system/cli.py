from __future__ import annotations

import argparse
import json
from pathlib import Path

from .edits import EditError, EditOp, JunctionAdjustmentOp, LinearWorkOp, TransitionOp, next_edit_id
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

    def add_work_parser(name: str, help_text: str) -> None:
        work = subparsers.add_parser(name, help=help_text)
        work.add_argument("project", type=Path)
        work.add_argument("--x1", type=float, required=True)
        work.add_argument("--y1", type=float, required=True)
        work.add_argument("--x2", type=float, required=True)
        work.add_argument("--y2", type=float, required=True)
        work.add_argument("--width", type=_positive_float, required=True)
        work.add_argument("--height", type=float, required=True)
        work.add_argument("--shoulder", type=_non_negative_float, default=0.0)
        work.add_argument("--priority", type=int, default=0, help="higher priority is retained at intersections")

    add_work_parser("road", "flatten and smooth a road segment")
    add_work_parser("river", "cut and smooth a river channel segment")

    junction = subparsers.add_parser("junction", help="add an independent revision for one road/river overlap")
    junction.add_argument("project", type=Path)
    junction.add_argument("--work-a", type=int, required=True)
    junction.add_argument("--work-b", type=int, required=True)
    junction.add_argument("--x", type=float, required=True)
    junction.add_argument("--y", type=float, required=True)
    junction.add_argument("--radius", type=_positive_float, required=True)
    junction.add_argument("--height", type=float, required=True)
    junction.add_argument("--feather", type=_non_negative_float, default=0.0)

    preview = subparsers.add_parser("preview", help="preview a linear work without saving it")
    preview.add_argument("project", type=Path)
    preview.add_argument("--kind", choices=("road", "river"), required=True)
    preview.add_argument("--x1", type=float, required=True)
    preview.add_argument("--y1", type=float, required=True)
    preview.add_argument("--x2", type=float, required=True)
    preview.add_argument("--y2", type=float, required=True)
    preview.add_argument("--width", type=_positive_float, required=True)
    preview.add_argument("--height", type=float, required=True)
    preview.add_argument("--shoulder", type=_non_negative_float, default=0.0)
    preview.add_argument("--priority", type=int, default=0)

    undo = subparsers.add_parser("undo", help="remove one persistent edit and replay the rest")
    undo.add_argument("project", type=Path)
    undo.add_argument("--id", type=int)

    stage = subparsers.add_parser("stage", help="add a construction stage")
    stage.add_argument("project", type=Path)
    stage.add_argument("--name", required=True)

    schedule = subparsers.add_parser("schedule", help="schedule a work (or a portion of it) at a stage")
    schedule.add_argument("project", type=Path)
    schedule.add_argument("--work", type=int, required=True)
    schedule.add_argument("--stage", type=int, required=True)
    schedule.add_argument("--start", type=float, help="portion start as fraction along the line")
    schedule.add_argument("--end", type=float, help="portion end as fraction along the line")

    demolish = subparsers.add_parser("demolish", help="schedule demolition of a built work at a stage")
    demolish.add_argument("project", type=Path)
    demolish.add_argument("--work", type=int, required=True)
    demolish.add_argument("--stage", type=int, required=True)

    transition = subparsers.add_parser("transition", help="add a temporary transition surface")
    transition.add_argument("project", type=Path)
    transition.add_argument("--x1", type=float, required=True)
    transition.add_argument("--y1", type=float, required=True)
    transition.add_argument("--x2", type=float, required=True)
    transition.add_argument("--y2", type=float, required=True)
    transition.add_argument("--width", type=_positive_float, required=True)
    transition.add_argument("--height", type=float, required=True)
    transition.add_argument("--shoulder", type=_non_negative_float, default=0.0)
    transition.add_argument("--priority", type=int, default=0)
    transition.add_argument("--built-stage", type=int, required=True)
    transition.add_argument("--demolish-stage", type=int)

    revise = subparsers.add_parser("revise", help="revise the unbuilt design of a partially built work")
    revise.add_argument("project", type=Path)
    revise.add_argument("--work", type=int, required=True)
    revise.add_argument("--x1", type=float, required=True)
    revise.add_argument("--y1", type=float, required=True)
    revise.add_argument("--x2", type=float, required=True)
    revise.add_argument("--y2", type=float, required=True)
    revise.add_argument("--width", type=_positive_float, required=True)
    revise.add_argument("--height", type=float, required=True)
    revise.add_argument("--shoulder", type=_non_negative_float, default=0.0)
    revise.add_argument("--priority", type=int)

    view = subparsers.add_parser("view", help="show the terrain of one chunk at a construction stage")
    view.add_argument("project", type=Path)
    view.add_argument("--stage", type=int, required=True)
    view.add_argument("--chunk", required=True)

    plan = subparsers.add_parser("plan", help="list stages with permanent works and temporary transitions")
    plan.add_argument("project", type=Path)

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
        elif args.command in ("road", "river"):
            edit = LinearWorkOp(
                id=next_edit_id(project.edits),
                kind=args.command,
                x1=args.x1,
                y1=args.y1,
                x2=args.x2,
                y2=args.y2,
                width=args.width,
                height=args.height,
                shoulder=args.shoulder,
                priority=args.priority,
            )
            affected = project.add_edit(edit)
            _print_json({
                "status": "edited",
                "edit_id": edit.id,
                "kind": edit.kind,
                "priority": edit.priority,
                "updated_chunks": [[key.x, key.y] for key in affected],
            })
        elif args.command == "junction":
            edit = JunctionAdjustmentOp(
                id=next_edit_id(project.edits),
                work_a=args.work_a,
                work_b=args.work_b,
                x=args.x,
                y=args.y,
                radius=args.radius,
                height=args.height,
                feather=args.feather,
            )
            affected = project.add_edit(edit)
            _print_json({
                "status": "edited",
                "edit_id": edit.id,
                "kind": "junction",
                "works": sorted((edit.work_a, edit.work_b)),
                "updated_chunks": [[key.x, key.y] for key in affected],
            })
        elif args.command == "preview":
            candidate = LinearWorkOp(
                id=next_edit_id(project.edits),
                kind=args.kind,
                x1=args.x1,
                y1=args.y1,
                x2=args.x2,
                y2=args.y2,
                width=args.width,
                height=args.height,
                shoulder=args.shoulder,
                priority=args.priority,
            )
            candidate.validate()
            _print_json({"status": "preview", **project.revision_summary(candidate)})
        elif args.command == "undo":
            edit_id = args.id if args.id is not None else max((edit.id for edit in project.edits), default=0)
            if edit_id < 1:
                raise EditError("there are no edits to undo")
            result = project.undo_edit(edit_id)
            _print_json({
                "status": "undone",
                "edit_id": edit_id,
                "kind": result["removed"].type_name,
                "updated_chunks": [[key.x, key.y] for key in result["updated_chunks"]],
                "voided_edits": result["voided_edits"],
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
        elif args.command == "stage":
            stage = project.add_stage(args.name)
            _print_json({"status": "staged", "stage_id": stage.id, "name": stage.name})
        elif args.command == "schedule":
            entry = project.add_schedule_entry(args.work, args.stage, args.start, args.end)
            _print_json({
                "status": "scheduled",
                "work_id": entry.work_id,
                "stage": entry.stage,
                "portion": list(entry.portion()),
            })
        elif args.command == "demolish":
            demolition = project.add_demolition(args.work, args.stage)
            _print_json({
                "status": "demolition_scheduled",
                "work_id": demolition.work_id,
                "stage": demolition.stage,
            })
        elif args.command == "transition":
            edit = TransitionOp(
                id=next_edit_id(project.edits),
                x1=args.x1,
                y1=args.y1,
                x2=args.x2,
                y2=args.y2,
                width=args.width,
                height=args.height,
                shoulder=args.shoulder,
                priority=args.priority,
                built_stage=args.built_stage,
                demolish_stage=args.demolish_stage,
            )
            affected = project.add_edit(edit)
            _print_json({
                "status": "edited",
                "edit_id": edit.id,
                "kind": "transition",
                "built_stage": edit.built_stage,
                "demolish_stage": edit.demolish_stage,
                "updated_chunks": [[key.x, key.y] for key in affected],
            })
        elif args.command == "revise":
            geometry = {
                "x1": args.x1,
                "y1": args.y1,
                "x2": args.x2,
                "y2": args.y2,
                "width": args.width,
                "height": args.height,
                "shoulder": args.shoulder,
            }
            if args.priority is not None:
                geometry["priority"] = args.priority
            result = project.revise_work(args.work, **geometry)
            _print_json({
                "status": "revised",
                "kept_work": result["kept_work"].id,
                "new_edit_id": result["new_edit"].id,
                "built_intervals": result["built_intervals"],
                "connections": result["connections"],
                "suggest_transition": result["suggest_transition"],
            })
        elif args.command == "view":
            key = ChunkKey.parse(args.chunk)
            view = project.view_at_stage(args.stage, key)
            _print_json({
                "status": "view",
                "stage": args.stage,
                "chunk": [key.x, key.y],
                "counts": view["counts"],
                "min_height": view["grid"]["min_height"],
                "max_height": view["grid"]["max_height"],
                "active_works": view["active_works"],
                "active_transitions": view["active_transitions"],
                "demolished_works": view["demolished_works"],
            })
        elif args.command == "plan":
            _print_json({"status": "plan", **project.plan_summary()})
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
