from __future__ import annotations

import argparse
import json
from pathlib import Path

from .edits import EditError, EditOp, JunctionAdjustmentOp, LinearWorkOp, TemporaryRampOp, next_edit_id
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
    _add_earthwork_parsers(subparsers)

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
        work.add_argument("--revises", type=int, help="id of the work whose unbuilt design is being revised")

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

    stitch = subparsers.add_parser("stitch", help="check generated adjacent chunk boundaries")
    stitch.add_argument("project", type=Path)
    stitch.add_argument("--tolerance", type=_finite_float, default=0.0)

    info = subparsers.add_parser("info", help="show project metadata")
    info.add_argument("project", type=Path)

    construct = subparsers.add_parser("construct", help="construct one longitudinal span of a work in the current stage")
    construct.add_argument("project", type=Path)
    construct.add_argument("--work", type=int, required=True)
    construct.add_argument("--start", type=_non_negative_float, default=0.0)
    construct.add_argument("--end", type=_positive_float, default=1.0)
    construct.add_argument("--stage", type=int)

    demolish = subparsers.add_parser("demolish", help="record demolition of completed terrain as a new construction decision")
    demolish.add_argument("project", type=Path)
    demolish.add_argument("--work", type=int, required=True)
    demolish.add_argument("--start", type=_non_negative_float, default=0.0)
    demolish.add_argument("--end", type=_positive_float, default=1.0)
    demolish.add_argument("--stage", type=int)

    ramp = subparsers.add_parser("ramp", help="add a temporary ramp valid for a stage range")
    ramp.add_argument("project", type=Path)
    ramp.add_argument("--x1", type=float, required=True)
    ramp.add_argument("--y1", type=float, required=True)
    ramp.add_argument("--x2", type=float, required=True)
    ramp.add_argument("--y2", type=float, required=True)
    ramp.add_argument("--width", type=_positive_float, required=True)
    ramp.add_argument("--height", type=float, required=True)
    ramp.add_argument("--shoulder", type=_non_negative_float, default=0.0)
    ramp.add_argument("--from-stage", type=int, default=1)
    ramp.add_argument("--until-stage", type=int, default=1)

    construct_junction = subparsers.add_parser("construct-junction", help="activate a junction adjustment at a specific stage")
    construct_junction.add_argument("project", type=Path)
    construct_junction.add_argument("--junction", type=int, required=True)
    construct_junction.add_argument("--stage", type=int)

    stage = subparsers.add_parser("stage", help="advance stage or view permanent/temporary terrain at a stage")
    stage.add_argument("project", type=Path)
    stage.add_argument("--advance", action="store_true")
    stage.add_argument("--number", type=int)
    stage.add_argument("--chunk")

    connect = subparsers.add_parser("connect-preview", help="preview height gap between a revised design and current terrain")
    connect.add_argument("project", type=Path)
    connect.add_argument("--kind", choices=("road", "river"), required=True)
    connect.add_argument("--x1", type=float, required=True)
    connect.add_argument("--y1", type=float, required=True)
    connect.add_argument("--x2", type=float, required=True)
    connect.add_argument("--y2", type=float, required=True)
    connect.add_argument("--width", type=_positive_float, required=True)
    connect.add_argument("--height", type=float, required=True)
    connect.add_argument("--shoulder", type=_non_negative_float, default=0.0)
    connect.add_argument("--priority", type=int, default=0)
    connect.add_argument("--revises", type=int)
    connect.add_argument("--t", type=_non_negative_float, default=0.0)

    basis = subparsers.add_parser("basis", help="report the explicit adopted basis at a junction point")
    basis.add_argument("project", type=Path)
    basis.add_argument("--junction", type=int, required=True)
    basis.add_argument("--x", type=float, required=True)
    basis.add_argument("--y", type=float, required=True)
    return parser


def _add_earthwork_parsers(subparsers) -> None:
    supply = subparsers.add_parser("earth-supply", help="record explicit external make-up material")
    supply.add_argument("project", type=Path)
    supply.add_argument("--quantity", type=float, required=True)
    supply.add_argument("--stage", type=int)
    supply.add_argument("--note", default="")

    loss = subparsers.add_parser("earth-loss", help="record explicit disposal/loss from a batch")
    loss.add_argument("project", type=Path)
    loss.add_argument("--batch", required=True)
    loss.add_argument("--quantity", type=float, required=True)
    loss.add_argument("--stage", type=int)
    loss.add_argument("--note", default="")

    reserve = subparsers.add_parser("earth-reserve", help="reserve batch soil for a future fill target")
    reserve.add_argument("project", type=Path)
    reserve.add_argument("--batch", required=True)
    reserve.add_argument("--target", required=True)
    reserve.add_argument("--quantity", type=float, required=True)
    reserve.add_argument("--stage", type=int)
    reserve.add_argument("--work", type=int)
    reserve.add_argument("--start", type=float)
    reserve.add_argument("--end", type=float)

    arrive = subparsers.add_parser("earth-arrive", help="record arrival of reserved soil")
    arrive.add_argument("project", type=Path)
    arrive.add_argument("--reservation", required=True)
    arrive.add_argument("--stage", type=int)
    arrive.add_argument("--quantity", type=float)

    place = subparsers.add_parser("earth-place", help="place arrived or free material into a fill target")
    place.add_argument("project", type=Path)
    place.add_argument("--target", required=True)
    place.add_argument("--quantity", type=float, required=True)
    place.add_argument("--stage", type=int)
    place.add_argument("--reservation")
    place.add_argument("--batch")
    place.add_argument("--work", type=int)
    place.add_argument("--start", type=float)
    place.add_argument("--end", type=float)

    cancel = subparsers.add_parser("earth-cancel", help="cancel an un-arrived reservation")
    cancel.add_argument("project", type=Path)
    cancel.add_argument("--reservation", required=True)
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--stage", type=int)

    balance = subparsers.add_parser("earth-balance", help="show batch and reservation balances at a stage")
    balance.add_argument("project", type=Path)
    balance.add_argument("--stage", type=int)

    trace = subparsers.add_parser("earth-trace", help="trace final fill back to source cut/recovery/supply")
    trace.add_argument("project", type=Path)
    trace.add_argument("--target", required=True)
    trace.add_argument("--stage", type=int)

    plan = subparsers.add_parser("earth-plan", help="compare stockpile reuse vs external supply")
    plan.add_argument("project", type=Path)
    plan.add_argument("--work", type=int, required=True)
    plan.add_argument("--span", action="append", default=[],
                       help="stage:start:end, may be repeated")


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
                revises=args.revises,
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
        elif args.command == "info":
            _print_json(project.to_dict())
        elif args.command == "construct":
            result = project.construct_work(args.work, args.start, args.end, args.stage)
            _print_json({
                "status": "constructed",
                "record": result["record"].to_dict(),
                "updated_chunks": [[key.x, key.y] for key in result["updated_chunks"]],
                "cut_volume": result["cut_volume"],
                "fill_volume": result["fill_volume"],
                "earthwork": result["earthwork"],
            })
        elif args.command == "demolish":
            result = project.demolish_work(args.work, args.start, args.end, args.stage)
            _print_json({
                "status": "demolished",
                "record": result["record"].to_dict(),
                "updated_chunks": [[key.x, key.y] for key in result["updated_chunks"]],
                "cut_volume": result["cut_volume"],
                "fill_volume": result["fill_volume"],
                "earthwork": result["earthwork"],
            })
        elif args.command == "ramp":
            edit = TemporaryRampOp(
                id=next_edit_id(project.edits),
                x1=args.x1,
                y1=args.y1,
                x2=args.x2,
                y2=args.y2,
                width=args.width,
                height=args.height,
                shoulder=args.shoulder,
                active_from=args.from_stage,
                active_until=args.until_stage,
            )
            affected = project.add_temporary_ramp(edit)
            _print_json({
                "status": "temporary_ramp",
                "edit_id": edit.id,
                "active_from": edit.active_from,
                "active_until": edit.active_until,
                "updated_chunks": [[key.x, key.y] for key in affected],
            })
        elif args.command == "construct-junction":
            affected = project.construct_junction(args.junction, args.stage)
            _print_json({
                "status": "junction_constructed",
                "junction": args.junction,
                "stage": project.schedule.junction_stages[args.junction],
                "updated_chunks": [[key.x, key.y] for key in affected],
            })
        elif args.command == "stage":
            if args.advance:
                project.advance_stage()
                _print_json({"status": "advanced", "stage": project.schedule.current_stage})
            elif args.chunk is not None:
                if args.number is None:
                    raise EditError("--number is required when viewing a chunk")
                _print_json(project.surface_at_stage(ChunkKey.parse(args.chunk), args.number))
            else:
                _print_json({"status": "stage", "schedule": project.schedule.to_dict()})
        elif args.command == "connect-preview":
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
                revises=args.revises,
            )
            candidate.validate()
            _print_json({"status": "connection_preview", **project.design_connection_preview(candidate, args.t)})
        elif args.command == "basis":
            _print_json({"status": "basis", **project.junction_height_basis(args.junction, args.x, args.y)})
        elif args.command == "earth-supply":
            result = project.earth_supply(args.quantity, args.stage, args.note)
            _print_json({"status": "supplied", **result})
        elif args.command == "earth-loss":
            result = project.earth_loss(args.batch, args.quantity, args.stage, args.note)
            _print_json({"status": "loss_recorded", **result})
        elif args.command == "earth-reserve":
            result = project.earth_reserve(
                args.batch, args.target, args.quantity, args.stage,
                work_id=args.work, start=args.start, end=args.end,
            )
            _print_json({"status": "reserved", **result})
        elif args.command == "earth-arrive":
            result = project.earth_arrive(args.reservation, args.stage, args.quantity)
            _print_json({"status": "arrived", **result})
        elif args.command == "earth-place":
            result = project.earth_place(
                args.target, args.quantity, args.stage,
                reservation_id=args.reservation, batch=args.batch,
                work_id=args.work, start=args.start, end=args.end,
            )
            _print_json({"status": "placed", **result})
        elif args.command == "earth-cancel":
            result = project.earth_cancel_reservation(args.reservation, args.reason, args.stage)
            _print_json({"status": "cancelled", **result})
        elif args.command == "earth-balance":
            _print_json({"status": "earth_balance", **project.earth_balances(args.stage)})
        elif args.command == "earth-trace":
            _print_json({"status": "earth_trace", **project.earth_trace(args.target, args.stage)})
        elif args.command == "earth-plan":
            spans = []
            for value in args.span:
                parts = value.split(":")
                if len(parts) != 3:
                    raise EditError("--span must be stage:start:end")
                spans.append({"stage": int(parts[0]), "start": float(parts[1]), "end": float(parts[2])})
            _print_json({"status": "earth_plan",
                         **project.compare_completion_strategies(args.work, spans)})
        else:
            parser.error("unknown command")
    except (ParameterError, EditError, ProjectError, FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
