from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .edits import (
    CrossingEditOp,
    EditError,
    EditOp,
    LinearWorkOp,
    choose_winner,
    edit_from_dict,
    next_edit_id,
    segment_intersection,
)
from .model import ChunkKey, chunk_affected_by_edit, generate_grid, validate_chunk_key
from .parameters import ParameterError, TerrainParameters
from .stitch import check_seams


PROJECT_FILE = "project.json"
CHUNK_DIR = "chunks"


class ProjectError(RuntimeError):
    pass


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as fd:
        temporary_name = fd.name
        try:
            json.dump(payload, fd, ensure_ascii=False, allow_nan=False, sort_keys=True)
            fd.write("\n")
            fd.flush()
            os.fsync(fd.fileno())
        except BaseException:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
            raise
    os.replace(temporary_name, path)


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ProjectError(str(path) + " must contain a JSON object")
    return data


class TerrainProject:
    def __init__(self, root: Path, parameters: TerrainParameters, edits: list[EditOp | RoadEditOp], chunks: dict[str, str]):
        self.root = root
        self.parameters = parameters
        self.edits = sorted(edits, key=lambda edit: edit.id)
        self.chunks = dict(chunks)

    @property
    def chunk_dir(self) -> Path:
        return self.root / CHUNK_DIR

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "parameters": self.parameters.to_dict(),
            "edits": [edit.to_dict() for edit in self.edits],
            "chunks": dict(sorted(self.chunks.items())),
        }

    def save_manifest(self) -> None:
        _atomic_write(self.root / PROJECT_FILE, self.to_dict())

    def save_chunk(self, key: ChunkKey) -> dict:
        validate_chunk_key(key)
        grid = generate_grid(self.parameters, key, self.edits)
        chunk_path = self.chunk_dir / key.filename
        _atomic_write(chunk_path, grid)
        self.chunks[str(key)] = CHUNK_DIR + "/" + key.filename
        self.save_manifest()
        return grid

    def generate_chunks(self, keys: list[ChunkKey]) -> list[dict]:
        unique = {}
        for key in keys:
            validate_chunk_key(key)
            unique[(key.x, key.y)] = key
        return [self.save_chunk(key) for key in unique.values()]

    def chunk_path(self, key: ChunkKey) -> Path:
        stored = self.chunks.get(str(key))
        if stored is None:
            raise ProjectError("chunk " + str(key) + " has not been generated")
        return self.root / stored

    def load_chunk(self, key: ChunkKey) -> dict:
        return _read_json(self.chunk_path(key))

    def load_all_chunks(self) -> dict[tuple[int, int], dict]:
        loaded = {}
        for stored_key in sorted(self.chunks):
            key = ChunkKey.parse(stored_key)
            data = self.load_chunk(key)
            stored_x = data.get("chunk", {}).get("x")
            stored_y = data.get("chunk", {}).get("y")
            if (stored_x, stored_y) != (key.x, key.y):
                raise ProjectError("chunk " + str(key) + " contains mismatched coordinates")
            loaded[(key.x, key.y)] = data
        return loaded

    def add_edit(self, edit) -> list[ChunkKey]:
        affected, _ = self.add_edit_report(edit)
        return affected

    def add_edit_report(self, edit) -> tuple[list[ChunkKey], list[dict]]:
        edit.validate()
        if edit.id <= 0 or edit.id in {stored.id for stored in self.edits}:
            raise EditError("edit id must be the next sequential id")
        if isinstance(edit, CrossingEditOp):
            works = self.linear_works()
            for work_id in (edit.work_a, edit.work_b):
                if work_id not in works:
                    raise EditError(f"referenced work not found: {work_id}")
            if segment_intersection(works[edit.work_a], works[edit.work_b]) is None:
                raise EditError("the two works do not intersect")
        self.edits.append(edit)

        works = self.linear_works()
        affected = []
        for stored_key in sorted(self.chunks):
            key = ChunkKey.parse(stored_key)
            if chunk_affected_by_edit(self.parameters, key, edit, works):
                affected.append(key)

        for key in affected:
            grid = generate_grid(self.parameters, key, self.edits)
            _atomic_write(self.chunk_dir / key.filename, grid)
        self.save_manifest()
        return affected, self.crossing_relations()

    def preview_edit(self, edit) -> dict:
        edit.validate()
        if edit.id <= 0 or edit.id in {stored.id for stored in self.edits}:
            raise EditError("edit id must be the next sequential id")
        works = self.linear_works()
        if isinstance(edit, CrossingEditOp):
            for work_id in (edit.work_a, edit.work_b):
                if work_id not in works:
                    raise EditError(f"referenced work not found: {work_id}")
            if segment_intersection(works[edit.work_a], works[edit.work_b]) is None:
                raise EditError("the two works do not intersect")
        preview_works = dict(works)
        if isinstance(edit, LinearWorkOp):
            preview_works[edit.id] = edit
        affected = [
            key
            for stored_key in sorted(self.chunks)
            for key in [ChunkKey.parse(stored_key)]
            if chunk_affected_by_edit(self.parameters, key, edit, preview_works)
        ]
        simulated = TerrainProject(
            self.root, self.parameters, [*self.edits, edit], self.chunks
        )
        return {
            "edit": edit.to_dict(),
            "affected_chunks": [[key.x, key.y] for key in affected],
            "crossings": simulated.crossing_relations(),
        }

    def linear_works(self) -> dict[int, LinearWorkOp]:
        return {edit.id: edit for edit in self.edits if isinstance(edit, LinearWorkOp)}

    def crossing_relations(self) -> list[dict]:
        works = self.linear_works()
        relations = []
        work_ids = sorted(works)
        for index, first_id in enumerate(work_ids):
            for second_id in work_ids[index + 1 :]:
                first = works[first_id]
                second = works[second_id]
                point = segment_intersection(first, second)
                if point is None:
                    continue
                winner = choose_winner(first, second)
                loser = second if winner is first else first
                relations.append(
                    {
                        "work_a": first.id,
                        "work_b": second.id,
                        "point": [point[0], point[1]],
                        "kept": winner.id,
                        "overridden": loser.id,
                        "rule": "higher priority; earlier id breaks ties",
                    }
                )
        return relations

    def undo_edit(self, edit_id: int) -> tuple[list[ChunkKey], list[dict]]:
        removed = next((edit for edit in self.edits if edit.id == edit_id), None)
        if removed is None:
            raise EditError(f"edit not found: {edit_id}")

        works_before = self.linear_works()
        affected_set = {
            key
            for stored_key in self.chunks
            for key in [ChunkKey.parse(stored_key)]
            if chunk_affected_by_edit(self.parameters, key, removed, works_before)
        }

        invalidated: list[dict] = []
        surviving_crossings: list[CrossingEditOp] = []
        for edit in self.edits:
            if not isinstance(edit, CrossingEditOp):
                continue
            if edit.state == "active" and edit_id in edit.work_ids():
                reason = f"work {edit_id} was removed"
                invalidated.append(
                    {
                        "edit_id": edit.id,
                        "state": "invalidated",
                        "retained": True,
                        "reason": reason,
                    }
                )
                edit = CrossingEditOp(
                    id=edit.id,
                    work_a=edit.work_a,
                    work_b=edit.work_b,
                    height=edit.height,
                    radius=edit.radius,
                    state="invalidated",
                    reason=reason,
                )
            surviving_crossings.append(edit)

        self.edits = [edit for edit in self.edits if edit.id != edit_id]
        self.edits = [edit for edit in self.edits if not isinstance(edit, CrossingEditOp)]
        self.edits.extend(
            edit for edit in surviving_crossings if edit.id != edit_id
        )
        self.edits.sort(key=lambda edit: edit.id)

        works_after = self.linear_works()
        for edit in surviving_crossings:
            if edit.id == edit_id:
                continue
            for stored_key in self.chunks:
                key = ChunkKey.parse(stored_key)
                if chunk_affected_by_edit(self.parameters, key, edit, works_after):
                    affected_set.add(key)

        affected = sorted(affected_set, key=lambda key: (key.x, key.y))
        for key in affected:
            grid = generate_grid(self.parameters, key, self.edits)
            _atomic_write(self.chunk_dir / key.filename, grid)
        self.save_manifest()
        return affected, invalidated

    def check_loaded_seams(self, tolerance: float = 0.0):
        return check_seams(self.load_all_chunks(), tolerance=tolerance)


def create_project(root: Path, parameters: TerrainParameters, overwrite: bool = False) -> TerrainProject:
    parameters.validate()
    manifest = root / PROJECT_FILE
    if manifest.exists() and not overwrite:
        raise FileExistsError("project already exists: " + str(manifest))
    project = TerrainProject(root, parameters, [], {})
    _atomic_write(manifest, project.to_dict())
    (root / CHUNK_DIR).mkdir(parents=True, exist_ok=True)
    return project


def load_project(root: Path) -> TerrainProject:
    manifest = root / PROJECT_FILE
    if not manifest.exists():
        raise FileNotFoundError("project not found: " + str(manifest))
    data = _read_json(manifest)
    if data.get("version") != 1:
        raise ProjectError("unsupported project version")
    try:
        parameters = TerrainParameters.from_mapping(data["parameters"])
        raw_edits = data.get("edits", [])
        raw_chunks = data.get("chunks", {})
    except (ParameterError, KeyError, TypeError) as exc:
        raise ProjectError("invalid project manifest: " + str(exc)) from exc

    if not isinstance(raw_edits, list) or not isinstance(raw_chunks, dict):
        raise ProjectError("invalid project manifest")
    edits = [edit_from_dict(value) for value in raw_edits]
    edit_ids = [edit.id for edit in edits]
    if len(edit_ids) != len(set(edit_ids)) or any(edit_id < 1 for edit_id in edit_ids):
        raise ProjectError("edit ids must be unique positive integers")

    chunks = {}
    for key, path in raw_chunks.items():
        parsed = ChunkKey.parse(key)
        validate_chunk_key(parsed)
        if not isinstance(path, str) or not path:
            raise ProjectError("invalid chunk path for " + key)
        if not (root / path).is_file():
            raise ProjectError("missing chunk file: " + path)
        chunks[key] = path

    return TerrainProject(root, parameters, edits, chunks)
