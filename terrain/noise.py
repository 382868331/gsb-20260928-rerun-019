"""全局坐标确定性噪声。

格点值由 (seed, ix, iy) 经整数哈希得到，与分块、生成顺序、进程均无关；
任意块在边界世界坐标上采样同一函数，因此相邻块边界严格一致。
"""

MASK64 = (1 << 64) - 1


def _splitmix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & MASK64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & MASK64
    return x ^ (x >> 31)


def _mix(seed: int, ix: int, iy: int) -> int:
    h = _splitmix64(seed & MASK64)
    h = _splitmix64(h ^ (ix & MASK64))
    h = _splitmix64(h ^ (iy & MASK64))
    return h


def _smooth(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class TerrainNoise:
    """分形值噪声，输出约在 [-1, 1]。

    scale 为每个噪声格点对应的世界长度（米），必须为正。
    """

    def __init__(self, seed: int, scale: float, octaves: int = 4,
                 lacunarity: float = 2.0, gain: float = 0.5):
        if not isinstance(seed, int):
            raise TypeError("seed 必须为整数")
        if scale <= 0:
            raise ValueError("scale 必须为正数")
        if octaves < 1:
            raise ValueError("octaves 至少为 1")
        if lacunarity <= 1.0:
            raise ValueError("lacunarity 必须大于 1")
        if not 0.0 < gain < 1.0:
            raise ValueError("gain 必须在 (0, 1) 内")
        self.seed = seed
        self.scale = float(scale)
        self.octaves = octaves
        self.lacunarity = float(lacunarity)
        self.gain = float(gain)

    def _lattice(self, ix: int, iy: int, octave: int) -> float:
        # 每个八度使用独立种子域，避免八度间格点相关。
        h = _mix(self.seed + 0x100000001B3 * (octave + 1), ix, iy)
        return h / (1 << 64) * 2.0 - 1.0

    def value_noise(self, x: float, y: float, octave: int) -> float:
        f = 1.0 / (self.scale * (self.lacunarity ** octave))
        fx, fy = x * f, y * f
        ix, iy = int(fx // 1), int(fy // 1)
        tx, ty = fx - ix, fy - iy
        sx, sy = _smooth(tx), _smooth(ty)
        v00 = self._lattice(ix, iy, octave)
        v10 = self._lattice(ix + 1, iy, octave)
        v01 = self._lattice(ix, iy + 1, octave)
        v11 = self._lattice(ix + 1, iy + 1, octave)
        a = _lerp(v00, v10, sx)
        b = _lerp(v01, v11, sx)
        return _lerp(a, b, sy)

    def sample(self, x: float, y: float) -> float:
        total = 0.0
        amplitude = 1.0
        normalizer = 0.0
        for octave in range(self.octaves):
            total += self.value_noise(x, y, octave) * amplitude
            normalizer += amplitude
            amplitude *= self.gain
        return total / normalizer
