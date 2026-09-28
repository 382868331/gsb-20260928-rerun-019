"""系统级行为测试：确定性、无缝、编辑保留、非法参数回滚、拼接。"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from terrain.chunk import read_layer
from terrain.config import ConfigError, TerrainConfig
from terrain.noise import TerrainNoise
from terrain.stitch import StitchedHeightmap, stitch
from terrain.world import TerrainWorld


@pytest.fixture()
def tmp_root():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture()
def world(tmp_root):
    cfg = TerrainConfig(seed=2026, chunk_size=32, noise_scale=10.0,
                        height_scale=50.0, max_height=80.0)
    return TerrainWorld.create(os.path.join(tmp_root, "proj"), cfg)


def test_noise_is_pure_function_of_world_coords():
    n1 = TerrainNoise(7, 16.0)
    n2 = TerrainNoise(7, 16.0)
    pts = [(0.0, 0.0), (12.3, -4.7), (-64.0, 128.25)]
    assert [n1.sample(x, y) for x, y in pts] == [n2.sample(x, y) for x, y in pts]


def test_different_seed_changes_terrain():
    a = TerrainNoise(1, 16.0).sample(3.0, 5.0)
    b = TerrainNoise(2, 16.0).sample(3.0, 5.0)
    assert a != b


def test_chunk_generation_order_independent(world):
    world.generate_chunk(0, 0)
    world.generate_chunk(1, 0)
    world.generate_chunk(0, 1)
    first = {c: read_layer(world._base_path(*c), world.config.chunk_size).values
             for c in world.generated_chunks()}

    # 用全新“进程”（重新打开工程、逆序生成）结果必须逐字节一致。
    world2 = TerrainWorld.open(world.root)
    for c in ((0, 1), (1, 0), (0, 0)):
        world2.generate_chunk(*c)
    second = {c: read_layer(world2._base_path(*c), world2.config.chunk_size).values
              for c in world2.generated_chunks()}
    assert first.keys() == second.keys()
    for c in first:
        assert first[c] == pytest.approx(second[c], abs=0.0, rel=0.0)


def test_shared_boundary_has_zero_seam(world):
    for c in ((0, 0), (1, 0), (0, 1), (1, 1), (-1, 0), (0, -1)):
        world.generate_chunk(*c)
    reports = world.check_seams(use_final=False, atol=0.0)
    assert reports == []


def test_manual_edit_survives_regen_of_unedited_area(world):
    world.generate_chunk(0, 0)
    world.generate_chunk(1, 0)
    # 在 (0,0) 块内人工抬高。
    world.edit_brush(5.0, 5.0, radius=3.0, strength=10.0)
    edited_before = world.final_chunk(0, 0)
    base_before = read_layer(world._base_path(0, 0), world.config.chunk_size)

    # 重新生成未编辑的邻居块和已编辑块的基础层：编辑层都不应被覆盖。
    world.generate_chunk(1, 0)
    world.generate_chunk(0, 0)
    base_after = read_layer(world._base_path(0, 0), world.config.chunk_size)
    edited_after = world.final_chunk(0, 0)
    assert base_after.values == pytest.approx(base_before.values, abs=0.0)
    assert edited_after.values == pytest.approx(edited_before.values, abs=0.0)

    # 编辑层文件确实独立存在且非零。
    edit = read_layer(world._edit_path(0, 0), world.config.chunk_size)
    assert any(v != 0.0 for v in edit.values)


def test_negative_strength_lowers_and_clamps(world):
    world.generate_chunk(0, 0)
    world.edit_brush(2.0, 2.0, radius=2.0, strength=-1000.0)
    final = world.final_chunk(0, 0)
    assert min(final.values) >= world.config.min_height


def test_edit_across_chunk_boundary_stays_seamless(world):
    """笔刷压在块共享接缝上时，两侧读到的同一物理顶点高度必须一致。"""
    world.generate_chunk(0, 0)
    world.generate_chunk(1, 0)
    s = world._span()
    # 笔刷中心正好在 x=s 的共享边界上。
    world.edit_brush(s * world.config.resolution, 10.0,
                     radius=5.0, strength=12.0)
    assert world.check_seams(use_final=True, atol=0.0) == []
    # 两侧对同一物理顶点给出相同高度。
    left = world.final_chunk(0, 0)
    right = world.final_chunk(1, 0)
    for row in range(world.config.chunk_size):
        assert left.get(row, s) == right.get(row, 0)


def test_edit_on_negative_boundary_stays_seamless(world):
    world.generate_chunk(-1, 0)
    world.generate_chunk(0, 0)
    world.edit_brush(0.0, 5.0, radius=4.0, strength=7.0)
    assert world.check_seams(use_final=True, atol=0.0) == []


def test_invalid_config_keeps_old_project(tmp_root):
    root = os.path.join(tmp_root, "p")
    good = TerrainConfig(seed=1, chunk_size=16)
    TerrainWorld.create(root, good)
    before = open(os.path.join(root, "terrain.json"), encoding="utf-8").read()
    world = TerrainWorld.open(root)
    with pytest.raises(ConfigError):
        world.update_config(chunk_size=1)  # 非法：<2
    with pytest.raises(ConfigError):
        world.update_config(resolution=0.0)
    with pytest.raises(ConfigError):
        world.update_config(max_height=-10.0)  # 小于 min_height
    with pytest.raises(ConfigError):
        world.update_config(noise_scale=-1.0)
    with pytest.raises(ConfigError):
        world.update_config(seed="not-int")
    after = open(os.path.join(root, "terrain.json"), encoding="utf-8").read()
    assert before == after
    # 工程仍可正常打开使用。
    assert TerrainWorld.open(root).config.seed == 1


def test_create_with_invalid_config_writes_nothing(tmp_root):
    root = os.path.join(tmp_root, "never")
    with pytest.raises(ConfigError):
        TerrainWorld.create(root, TerrainConfig(chunk_size=0))
    assert not os.path.exists(root)


def test_unknown_config_field_rejected(world):
    raw = world.config.to_dict()
    raw["hacked"] = True
    with pytest.raises(ConfigError):
        TerrainConfig.from_dict(raw)


def test_stitch_builds_consistent_grid(world):
    for c in ((0, 0), (1, 0), (0, 1), (1, 1)):
        world.generate_chunk(*c)
    hm = stitch(world, use_final=False)
    s = world._span()
    assert hm.cols == 2 * s + 1
    assert hm.rows == 2 * s + 1
    # 角点与各块边界值核对。
    corner_chunk = read_layer(world._base_path(0, 0), world.config.chunk_size)
    assert hm.heights[0] == corner_chunk.get(0, 0)


def test_stitch_detects_holes(world):
    world.generate_chunk(0, 0)
    world.generate_chunk(2, 0)
    with pytest.raises(ValueError):
        stitch(world, use_final=False)


def test_config_roundtrip(world):
    loaded = TerrainConfig.read(world.config_path)
    assert loaded == world.config


def test_layer_size_guard(world):
    from terrain.chunk import ChunkData
    with pytest.raises(ValueError):
        ChunkData.from_bytes(4, b"\0" * 8)
