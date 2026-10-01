# 程序化分块高度地形

这是一个纯 Python 的地形工程原型。基础地形只由世界坐标、种子和生成参数决定，不依赖分块生成顺序；局部抬升、道路、河道压平以及交汇区小范围调整都保存在独立编辑层，重新生成已有或缺失分块时都会按编辑 ID 重新应用。每条道路/河道是一次独立修订，交汇处用显式 priority 决定保留谁的设计，而不是由最后一次编辑无条件覆盖。

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
python -m terrain_system.cli road ./terrain_demo --x1 -50 --y1 50 --x2 250 --y2 50 --width 12 --height 8 --shoulder 8 --priority 10
python -m terrain_system.cli river ./terrain_demo --x1 0 --y1 -50 --x2 0 --y2 250 --width 10 --height -3 --shoulder 6 --priority 5
python -m terrain_system.cli preview ./terrain_demo --kind road --x1 0 --y1 0 --x2 200 --y2 200 --width 12 --height 6 --priority 8
python -m terrain_system.cli junction ./terrain_demo --work-a 2 --work-b 3 --x 0 --y 50 --radius 8 --height 5 --feather 3
python -m terrain_system.cli generate ./terrain_demo 2,0
python -m terrain_system.cli stitch ./terrain_demo
python -m terrain_system.cli undo ./terrain_demo --id 2
~~~

edit --delta 为正是抬高，为负是压低。road 把道路中心线半宽内压到指定绝对高度，并在 shoulder 宽度内用平滑坡面过渡到原地形；道路可穿过分块边界，函数只依赖世界坐标，因此两侧边界高度一致。编辑只记录一次，覆盖到的已生成块会立即更新；以后补生成的块也会自动应用同一编辑，不需要烘焙成随机地形的一部分。

道路（road）和河道（river）都是带显式 priority 的线性工程：交汇处由 priority 较大的工程保留自己的设计高度，priority 相同则由较小的编辑 ID 决定，因此结果与“谁后编辑”无关，也不受分块生成顺序或进程影响；编辑仍按 ID 固定重放。junction 是独立的第三次修订，只在以某点为中心的小半径区域内覆盖两个被引用工程的设计，feather 给出与周边平滑过渡的羽化宽度，半径之外的地形不变。

preview 不写入工程，返回新线性工程会更新的已生成分块以及每个交汇点的坐标和保留/被覆盖的工程 ID。undo --id 只撤回这一次修订并重放其余编辑；省略 --id 时撤回最近一次编辑。撤回道路或河道时，引用它的 junction 修订会一并失效并在 undo 输出和 project.json 的 voided_edits 中给出原因，另一条工程（如河道）的修改完整保留；失效的 junction 不再作用于地形，重新补一条 junction 修订会使用新的最大 ID，旧 ID 不会被复用。

## 施工阶段

道路和河道可以先排入施工计划再逐步生效，而不是一次性全部施加到地形：

~~~bash
python -m terrain_system.cli stage ./terrain_demo --name 一期
python -m terrain_system.cli stage ./terrain_demo --name 二期
python -m terrain_system.cli schedule ./terrain_demo --work 2 --stage 1 --start 0 --end 0.5
python -m terrain_system.cli schedule ./terrain_demo --work 2 --stage 2 --start 0.5 --end 1
python -m terrain_system.cli view ./terrain_demo --stage 1 --chunk 0,0
python -m terrain_system.cli plan ./terrain_demo
~~~

schedule 把某项工程（可只给 start/end 之间的一段）安排到某个阶段；junction 修订只支持整体排期。view 在指定阶段即时计算某分块地形，不写入 chunks/：已完成部分按设计高度、临时过渡面单独计数、尚未施工的部分保持原地形，并列出该阶段生效的永久工程、临时过渡和已拆除工程。跨阶段交汇仍由显式 priority 决定采用谁的设计，与施工先后无关。没有排期的工程视为不受施工计划管理，在任何阶段都完整生效。分块文件始终保存最终状态（全部排期完成、该拆的过渡已拆除），重开工程或改变分块生成顺序后，同一阶段的视图与施工记录一致。

### 临时过渡与拆除

transition 是带 built_stage/demolish_stage 的临时线性工程，与永久工程共用同一套 priority 规则：

~~~bash
python -m terrain_system.cli transition ./terrain_demo --x1 40 --y1 50 --x2 60 --y2 50 --width 8 --height 6 --built-stage 1 --demolish-stage 2
python -m terrain_system.cli demolish ./terrain_demo --work 2 --stage 3
~~~

过渡在 built_stage 生效，到达 demolish_stage（或有针对它的 demolish 记录）后不再影响任何视图和最终地形。demolish 把“拆除已完成部分”记录为指定阶段的新施工决定，而不是撤销历史。plan 按阶段分别列出永久工程、新建过渡和拆除的过渡，便于比较不同施工顺序。

### 半途修改设计

~~~bash
python -m terrain_system.cli revise ./terrain_demo --work 2 --x1 150 --y1 50 --x2 250 --y2 50 --width 12 --height 9
~~~

revise 用于已部分施工的工程：旧编辑及其排期保持不变，已施工段维持现状；新设计成为一条新的未排期编辑。输出给出每个施工断面（已建成区间端点）处的现状高度、新设计高度和高差，高差非零时 suggest_transition 为 true，提示可用 transition 加一段临时过渡。undo 只能撤回尚未施工的修订；对已排期施工的工程或临时过渡执行 undo 会报错并提示改用 demolish，因此撤销不会回退已完成地形，拆除已完成部分必须是新的施工决定。

## 持久化和非法参数

所有 JSON 都通过临时文件加原子替换写入。初始化时会先完整校验参数，参数非法不会覆盖旧工程。编辑操作使用唯一正数 ID，撤回可在序列中留下空隙；重新打开工程后仍按 ID 从小到大固定重放。

## 测试

~~~bash
python -m pytest
~~~

测试覆盖不同分块生成次序、独立子进程和重开工程后的逐文件一致性、负坐标分块、局部重算后的编辑保留、跨分块道路边界零裂缝、道路/河道交汇处的优先级而非编辑先后、junction 小范围修订及其单独撤销、移除道路后河道保留与 junction 失效说明、预览不落盘、非法参数保留旧工程、分段施工视图与最终地形、临时过渡的搭建/拆除生命周期、半途修改设计时保留已施工段并报告连接高差、已施工工程禁止 undo 而需 demolish、重开工程与分块生成顺序变化后阶段视图一致、跨阶段交汇的优先级，以及完整 CLI 流程。
