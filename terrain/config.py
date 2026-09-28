"""地形生成参数：校验、JSON 持久化与原子更新。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from typing import Any, Mapping


class ConfigError(ValueError):
    """参数不合法。"""


@dataclass(frozen=True)
class TerrainConfig:
    seed: int = 1337
    chunk_size: int = 64          # 每块每边顶点数（>=2，共享边界顶点）
    resolution: float = 1.0       # 顶点间距（米），>0
    height_scale: float = 40.0    # 高度缩放（米），>=0
    noise_scale: float = 32.0     # 基础噪声格点间距（米），>0
    octaves: int = 4
    lacunarity: float = 2.0
    gain: float = 0.5
    min_height: float = 0.0
    max_height: float = 100.0

    _NUMERIC = ("resolution", "noise_scale", "height_scale",
                "lacunarity", "gain", "min_height", "max_height")

    def validate(self) -> None:
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ConfigError("seed 必须为整数")
        if not -(1 << 63) <= self.seed < (1 << 63):
            raise ConfigError("seed 超出 64 位有符号整数范围")
        if not isinstance(self.chunk_size, int) or isinstance(self.chunk_size, bool):
            raise ConfigError("chunk_size 必须为整数")
        if self.chunk_size < 2 or self.chunk_size > 4096:
            raise ConfigError("chunk_size 必须在 [2, 4096]")
        for name in self._NUMERIC:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{name} 必须为数值")
        if self.resolution <= 0:
            raise ConfigError("resolution 必须为正数")
        if self.noise_scale <= 0:
            raise ConfigError("noise_scale 必须为正数")
        if self.height_scale < 0:
            raise ConfigError("height_scale 不能为负")
        if not isinstance(self.octaves, int) or isinstance(self.octaves, bool):
            raise ConfigError("octaves 必须为整数")
        if not 1 <= self.octaves <= 16:
            raise ConfigError("octaves 必须在 [1, 16]")
        if self.lacunarity <= 1.0:
            raise ConfigError("lacunarity 必须大于 1")
        if not 0.0 < self.gain < 1.0:
            raise ConfigError("gain 必须在 (0, 1) 内")
        if self.max_height <= self.min_height:
            raise ConfigError("max_height 必须大于 min_height")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TerrainConfig":
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"未知参数: {sorted(unknown)}")
        cfg = cls(**{k: data[k] for k in data if k in allowed})
        cfg.validate()
        return cfg

    def write_atomic(self, path: str) -> None:
        """原子写入 JSON：先写临时文件再 rename，失败不影响旧文件。"""
        directory = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def read(cls, path: str) -> "TerrainConfig":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ConfigError("参数文件格式错误")
        return cls.from_dict(data)
