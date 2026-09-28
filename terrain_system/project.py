from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .edits import EditOp, EditError, next_edit_id
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
    def __init__(self, root: Path, parameters: TerrainParameters, edits: list[EditOp], chunks: dict[str, str]):
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

    def add_edit(self, edit: EditOp) -> list[ChunkKey]:
        edit.validate()
        if edit.id != next_edit_id(self.edits):
            raise EditError("edit id must be the next sequential id")
        self.edits.append(edit)

        affected = []
        for stored_key in sorted(self.chunks):
            key = ChunkKey.parse(stored_key)
            if chunk_affected_by_edit(self.parameters, key, edit):
                affected.append(key)

        for key in affected:
            grid = generate_grid(self.parameters, key, self.edits)
            _atomic_write(self.chunk_dir / key.filename, grid)
        self.save_manifest()
        return affected

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
    edits = [EditOp.from_dict(value) for value in raw_edits]
    expected_ids = list(range(1, len(edits) + 1))
    if [edit.id for edit in edits] != expected_ids:
        raise ProjectError("edit ids must be sequential starting at 1")

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
