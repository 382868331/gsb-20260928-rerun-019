# 程序化分块高度地形

这是一个纯 Python 的地形工程原型。基础地形只由世界坐标、种子和生成参数决定，不依赖分块生成顺序；局部抬高/压低与道路压平保存在独立编辑层，重新生成已有或缺失分块时都会重新应用。

## 工程结构

- project.json：保存生成参数、编辑操作和已生成块清单。
- chunks/：每个分块一个 JSON，包含基础高度和应用编辑后的最终高度。
- 分块键 x,y 是世界坐标中的分块索引，可直接作为后续道路、植被和流式加载的空间分块。
- 同一块的四个角与相邻块共享世界坐标，因此相同输入得到完全相同的边界点。

## 使用

~~~bash
python -m terrain_system.cli init ./terrain_demo \
  --seed 12345 \
  --chunk-size 100 \
  --resolution 33 \
  --scale 40 \
  --octaves 4 \
  --persistence 0.5 \
  --lacunarity 2.0 \
  --base-height 0 \
  --amplitude 12

python -m terrain_system.cli generate ./terrain_demo 0,0 1,0 0,1 1,1
python -m terrain_system.cli edit ./terrain_demo --x 20 --y 15 --radius 12 --delta 3.5
python -m terrain_system.cli road ./terrain_demo --x1 -10 --y1 50 --x2 210 --y2 55 --half-width 6 --shoulder 4
python -m terrain_system.cli generate ./terrain_demo 2,0
python -m terrain_system.cli stitch ./terrain_demo
python -m terrain_system.cli undo ./terrain_demo --id 1
~~~

edit --delta 为正是抬高，为负是压低。编辑只记录一次，当前覆盖到的已生成块会立即更新；以后补生成的块也会自动应用同一编辑，不需要烘焙成随机地形的一部分。

## 跨分块道路

`road` 把中心线两侧 half-width 内的高度压平到中心线处未经编辑的基础地形高度，车辆沿中心线没有横向坡度。`--shoulder` 给出额外的坡面过渡宽度，在该范围内用 smoothstep 权重平滑回到周围地形：

- 道路高度只由世界坐标和底层基础地形决定，因此穿过分块边界时两侧高度逐点相等，接缝天然连续。
- 交叉或重叠的多条道路按权重平均合并，含义只取决于编辑集合，与编辑先后顺序无关；再次打开工程或局部重算结果不变。
- 抬高/压低编辑始终叠加在道路压平结果之上。
- `undo --id` 可以撤回任意一次历史编辑（包括道路），撤回后全部已生成块按剩余编辑确定性重算；编辑 ID 保持唯一，不需要连续。
- 道路中心线取自未编辑的基础地形，所以即使先压平道路、再重生成底层地形，人工修改仍然保留且结果一致。

## 持久化和非法参数

所有 JSON 都通过临时文件加原子替换写入。初始化时会先完整校验参数，参数非法不会覆盖旧工程。编辑操作使用唯一的正整数 ID，操作列表始终按 ID 固定顺序重放。

## 测试

~~~bash
python -m pytest
~~~

测试覆盖乱序生成、在独立 Python 进程中重开工程补块并逐点比对高度、跨进程接缝零裂缝、编辑顺序无关、撤回单次编辑、跨分块道路边界连续与交叉确定性、非法参数保留旧工程，以及按本文档命令逐步执行的完整 CLI 流程。
