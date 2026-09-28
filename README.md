# 程序化地形制作系统（Python / 分块高度地形）

纯标准库实现，无第三方依赖。核心目标：**同一组种子与参数下，
无论块的生成顺序、是否重启后补块，相邻块边界严格一致；基础地形
与局部人工修改分层存储，重生成不覆盖编辑。**

## 设计要点

- **全局坐标确定性噪声**（`terrain/noise.py`）：格点值由
  `(seed, 格点ix, 格点iy)` 经 splitmix64 整数哈希得到，噪声是
  世界坐标的纯函数。不使用“每块独立随机”，因此块边界不存在接缝。
- **共享边界顶点的分块规则**：块 `(cx,cy)` 的顶点原点为
  `(cx*(N-1), cy*(N-1))`（`N=chunk_size`），相邻块边缘顶点落在同一
  世界坐标、采样同一函数 → 边界高度逐点相等（浮点精确，非近似混合）。
  块坐标支持任意正/负整数，后续道路、植被可直接复用这套空间分块。
- **两层存储**（工程目录）：
  - `terrain.json`：生成参数（原子写）。
  - `base/{cx}_{cy}.f32`：生成的基础高度层，可随时重算。
  - `edits/{cx}_{cy}.f32`：局部人工抬高/压低增量，只由编辑产生。
  - 合成高度 `final = clamp(base + edit, min_height, max_height)`；
    重新生成基础层绝不触碰编辑层。
- **非法参数保留旧工程**：参数先完整校验，再写临时文件并 `os.replace`
  原子替换；任何一步失败，旧 `terrain.json` 与已生成块均不变。

## 使用

```bash
python -m terrain.cli init proj --seed 42 --chunk-size 64 --noise-scale 20
python -m terrain.cli generate proj 0,0 1,0 0,1 1,1   # 顺序任意
python -m terrain.cli check proj                      # 拼接查裂缝
python -m terrain.cli edit proj --x 10 --y 10 --radius 6 --strength 15
python -m terrain.cli edit proj --x -20 --y 30 --radius 8 --strength -20
python -m terrain.cli regen proj --all                # 重算基础层，编辑保留
python -m terrain.cli config proj --height-scale 60   # 非法值会拒绝并保留旧参数
python -m terrain.cli list proj
```

端到端演示（乱序生成、编辑、重启补块、拼接导出 PGM 抽查图、
双工程逐顶点一致性核对）：

```bash
python demo.py
```

测试：

```bash
python -m pytest tests/ -q
```

## 输出不是随机图片

系统真源是可复用的分块 f32 高度数据与参数/编辑文件；
PGM 仅为可选人眼抽查产物，不参与任何逻辑。
