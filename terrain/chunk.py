"""分块栅格存储：基础层与编辑层分离。

最终高度 = clamp(base + edit)。base 可反复重新生成；
edit 只由局部人工编辑产生，重新生成 base 不会触碰它。
层数据以小端 float32 原始数组落盘，逐块原子替换。
"""

from __future__ import annotations

import os
import struct
import tempfile


class ChunkData:
    __slots__ = ("size", "values")

    def __init__(self, size: int, values=None):
        self.size = size
        if values is None:
            self.values = [0.0] * (size * size)
        else:
            self.values = list(values)
            if len(self.values) != size * size:
                raise ValueError("高度数据长度与 chunk_size 不符")

    def index(self, row: int, col: int) -> int:
        return row * self.size + col

    def get(self, row: int, col: int) -> float:
        return self.values[self.index(row, col)]

    def set(self, row: int, col: int, value: float) -> None:
        # 统一在 float32 域运算，保证任何调用路径逐位一致。
        self.values[self.index(row, col)] = struct.unpack("<f", struct.pack("<f", value))[0]

    def to_bytes(self) -> bytes:
        return struct.pack("<%df" % len(self.values), *self.values)

    @classmethod
    def from_bytes(cls, size: int, data: bytes) -> "ChunkData":
        expected = size * size * 4
        if len(data) != expected:
            raise ValueError(
                f"块数据长度 {len(data)} 与期望 {expected} 不符（chunk_size={size}）")
        return cls(size, struct.unpack("<%df" % (size * size), data))


def write_layer_atomic(path: str, chunk: ChunkData) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".layer-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(chunk.to_bytes())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_layer(path: str, size: int) -> ChunkData:
    with open(path, "rb") as fh:
        return ChunkData.from_bytes(size, fh.read())
