# 程序化分块高度地形

这是一个纯 Python 的地形工程原型。基础地形只由世界坐标、种子和生成参数决定，不依赖分块生成顺序；局部抬高和压低保存在独立编辑层，重新生成已有或缺失分块时都会重新应用。

## 工程结构

- project.json：保存生成参数、局部编辑操作和已生成块清单。
- chunks/：每个分块一个 JSON，包含基础高度和应用编辑后的最终高度。
- 分块键 x,y 是世界坐标中的分块索引，可直接作为后续道路、植被和流式加载的空间分块。
- 同一块的四个角与相邻块共享世界坐标，因此相同输入得到完全相同的边界点。

## 使用

~~~bash
python -m terrain_system.cli init ./terrain_demo +  --seed 12345 +  --chunk-size 100 +  --resolution 33 +  --scale 40 +  --octaves 4 +  --persistence 0.5 +  --lacunarity 2.0 +  --base-height 0 +  --amplitude 12

python -m terrain_system.cli generate ./terrain_demo 0,0 1,0 0,1 1,1
python -m terrain_system.cli edit ./terrain_demo --x 20 --y 15 --radius 12 --delta 3.5
python -m terrain_system.cli generate ./terrain_demo 2,0
python -m terrain_system.cli stitch ./terrain_demo
~~~

edit --delta 为正是抬高，为负是压低。编辑只记录一次，当前半径覆盖到的已生成块会立即更新；以后补生成的块也会自动应用同一编辑，不需要烘焙成随机地形的一部分。

## 持久化和非法参数

所有 JSON 都通过临时文件加原子替换写入。初始化时会先完整校验参数，参数非法不会覆盖旧工程。编辑操作使用从 1 开始的连续 ID，保证操作列表可按固定顺序重放。

## 测试

~~~bash
python -m pytest
~~~

测试覆盖乱序和重启后补块、边界零裂缝、编辑后重生成不丢失、非法参数保留旧工程，以及完整 CLI 流程。
