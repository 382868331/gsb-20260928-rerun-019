# 程序化分块高度地形

这是一个纯 Python 的地形工程原型。基础地形只由世界坐标、种子和生成参数决定，不依赖分块生成顺序；局部抬升和道路压平保存在独立编辑层，重新生成已有或缺失分块时都会按编辑 ID 重新应用。

## 工程结构

- project.json：保存生成参数、局部编辑操作和已生成块清单。
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
python -m terrain_system.cli road ./terrain_demo --x1 -50 --y1 50 --x2 250 --y2 50 --width 12 --height 8 --shoulder 8
python -m terrain_system.cli generate ./terrain_demo 2,0
python -m terrain_system.cli stitch ./terrain_demo
python -m terrain_system.cli undo ./terrain_demo --id 2
python -m terrain_system.cli river ./terrain_demo --x1 -50 --y1 -50 --x2 250 --y2 50 --width 8 --height -6 --priority 2
python -m terrain_system.cli crossing ./terrain_demo --work-a 2 --work-b 3 --height 2 --radius 6
python -m terrain_system.cli road ./terrain_demo --x1 0 --y1 0 --x2 200 --y2 0 --width 12 --height 8 --preview
~~~

edit --delta 为正是抬高，为负是压低。road 把道路中心线半宽内压到指定绝对高度，并在 shoulder 宽度内用平滑坡面过渡到原地形；道路可穿过分块边界，函数只依赖世界坐标，因此两侧边界高度一致。编辑只记录一次，覆盖到的已生成块会立即更新；以后补生成的块也会自动应用同一编辑，不需要烘焙成随机地形的一部分。

## 道路、河道与交汇

道路（road）和河道（river）都是独立的线状工程修订，各有自己的宽度、过渡肩和高度影响范围。交汇时不再由“最后一次编辑”无条件覆盖：priority 较大的工程保留自己的设计；priority 相同时保留较早（ID 较小）的修订。添加工程时返回 crossings 列表，逐项给出交点、保留方 kept、被覆盖方 overridden 和判定规则，重开工程或改变生成顺序都得到同一结果。

需要时可以用 crossing 为一小段交汇区单独给出绝对高度调整，它通过 --work-a/--work-b 引用两条工程，只在交点周围 --radius 范围内平滑生效，优先级高于自动判定的保留方。引用的工程不存在或几何上不相交会被拒绝，工程不会落盘。

road、river、crossing 都支持 --preview：只返回受影响分块 affected_chunks 和交汇关系，不写入任何文件。

undo --id 只撤回这次修订并重放其余编辑，其他工程的修改仍在；省略 --id 时撤回最近一次编辑。撤回后再添加操作会使用新的最大 ID，旧 ID 不会被复用。若移除某条道路或河道导致 crossing 失去引用，该交汇调整不会被静默删除：它保留在工程中并标记为 state=invalidated，undo 输出 invalidated_crossings 说明原因（如 work 1 was removed），地形回到剩余工程的设计；被失效的 crossing 也可以再用 undo 明确移除。

## 持久化和非法参数

所有 JSON 都通过临时文件加原子替换写入。初始化时会先完整校验参数，参数非法不会覆盖旧工程。编辑操作使用唯一正数 ID，撤回可在序列中留下空隙；重新打开工程后仍按 ID 从小到大固定重放。

## 测试

~~~bash
python -m pytest
~~~

测试覆盖不同分块生成次序、独立子进程和重开工程后的逐文件一致性（含负坐标分块）、局部重算后的编辑保留、跨分块道路边界零裂缝、道路/河道交汇的优先级与同优先级保留规则、交汇段人工调整、预览不落盘、撤回单次修订、交汇调整随工程移除而失效并给出说明、非法参数保留旧工程，以及完整 CLI 流程。
