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
python -m terrain_system.cli construct ./terrain_demo --work 2 --start 0 --end 0.5
python -m terrain_system.cli connect-preview ./terrain_demo --kind road --x1 0 --y1 0 --x2 200 --y2 200 --width 12 --height 6 --revises 2 --t 0.5
python -m terrain_system.cli ramp ./terrain_demo --x1 90 --y1 90 --x2 110 --y2 110 --width 8 --height 6 --from-stage 1 --until-stage 1
python -m terrain_system.cli construct-junction ./terrain_demo --junction 4 --stage 2
python -m terrain_system.cli stage ./terrain_demo --number 1 --chunk 0,0
python -m terrain_system.cli basis ./terrain_demo --junction 4 --x 0 --y 50
python -m terrain_system.cli demolish ./terrain_demo --work 2 --start 0 --end 0.25
python -m terrain_system.cli generate ./terrain_demo 2,0
python -m terrain_system.cli stitch ./terrain_demo
python -m terrain_system.cli undo ./terrain_demo --id 2
~~~

edit --delta 为正是抬高，为负是压低。road 把道路中心线半宽内压到指定绝对高度，并在 shoulder 宽度内用平滑坡面过渡到原地形；道路可穿过分块边界，函数只依赖世界坐标，因此两侧边界高度一致。编辑只记录一次，覆盖到的已生成块会立即更新；以后补生成的块也会自动应用同一编辑，不需要烘焙成随机地形的一部分。

道路（road）和河道（river）都是带显式 priority 的线性工程：交汇处由 priority 较大的工程保留自己的设计高度，priority 相同则由较小的编辑 ID 决定，因此结果与“谁后编辑”无关，也不受分块生成顺序或进程影响；编辑仍按 ID 固定重放。junction 是独立的第三次修订，只在以某点为中心的小半径区域内覆盖两个被引用工程的设计，feather 给出与周边平滑过渡的羽化宽度，半径之外的地形不变。

preview 不写入工程，返回新线性工程会更新的已生成分块以及每个交汇点的坐标和保留/被覆盖的工程 ID。undo --id 只撤回这一次修订并重放其余编辑；省略 --id 时撤回最近一次编辑。撤回道路或河道时，引用它的 junction 修订会一并失效并在 undo 输出和 project.json 的 voided_edits 中给出原因，另一条工程（如河道）的修改完整保留；失效的 junction 不再作用于地形，重新补一条 junction 修订会使用新的最大 ID，旧 ID 不会被复用。

## 分阶段施工

道路和河道的几何与设计高度是设计修订；construction 记录的是施工决定，两者分开保存。`construct` 按中心线参数 0..1 只让一个区间在指定阶段生效，因此同一条道路可以先完成前半段，之后沿施工时间查看已完成永久面、尚未施工的原地形，以及临时通行面。分块 JSON 同时保存 `base_heights`、`permanent_heights`、`heights`、`temporary_deltas`、截至该阶段的 `construction_records` 和 `active_temporary_ramps`；重新打开工程或改变分块补生成顺序后，地形仍可与施工记录逐条对应。

如果道路完成一半后修改剩余设计，新道路/河道可使用 `road --revises` 引用旧工程 ID。新设计只在自己的施工区间生效，旧设计的已施工区间继续保留；`connect-preview` 给出连接处的世界坐标、当前永久高度、新设计高度和高度差，可据此加入 `ramp` 临时过渡。临时过渡有 `active_from/active_until`，只在这些阶段的 `heights` 中生效，`permanent_heights` 不包含它；阶段结束后自动失效，不进入后续结果。

`undo` 只能撤回尚未形成永久地形的设计修订；已经施工的道路或河道不能通过撤销回退，必须使用 `demolish` 记录一个新的拆除施工决定。junction 也可以用 `construct-junction` 指定生效阶段：跨阶段交汇高度采用该 junction 的显式记录阶段，`basis` 命令返回坐标、阶段、生效工程 ID 和最终采用依据（junction ID 或获胜工程 ID），不依赖生成或编辑顺序。

## 持久化和非法参数

所有 JSON 都通过临时文件加原子替换写入。初始化时会先完整校验参数，参数非法不会覆盖旧工程。编辑操作使用唯一正数 ID，撤回可在序列中留下空隙；重新打开工程后仍按 ID 从小到大固定重放。

## 土方台账

挖方、填方和临时堆土记录在与施工记录分离、但同属一个工程的追加式台账 `earthwork` 中。每个材料批次有独立身份：道路开挖产生 `C` 批次（挖方），外部补料是 `S` 批次，拆除永久面或临时坡道到期产生 `R` 批次（回收土）。已经填入的材料不会因为后来改设计而重新回到库存；拆除必须作为新阶段的新决定，产生新的回收批次。

运输用“预留 (`P`) → 运抵 → 投放”三步表达：`earth-reserve` 只锁定尚未运走的堆土数量，`earth-arrive` 表示材料实际到达填筑点，`earth-place` 才成为永久填筑。两个填筑目标不能同时占用同一份预留：超量预留或在运抵前投放都会被拒绝。设计修订 (`road --revises`)、`earth-cancel` 和分段拆除发生在不同阶段时分别处理：修订/取消只释放还没有发生的预留，已经运抵或已填入的材料不回库；拆除则按真实地形下降量生成新的回收批次。

损耗 (`earth-loss`) 和外部补料 (`earth-supply`) 必须显式记录，台账永远不会自动用它们补平填筑缺口。每段施工自动按阶段网格的实际地形差积分出挖/填体积并生成挖方批次或填筑需求 (`F`)：交汇道路、河道和临时坡道覆盖同一空间时只有一个最终高度，名义体积不会重复相加。`stage --number N --chunk x,y` 的输出除实际地形和已发生施工记录外，还包含截至该阶段的台账事件、批次余额、填筑需求与每批材料去向。

`earth-plan --work ID --span stage:start:end ...` 在不写盘的前提下，对“继续利用现有堆土（含本段新挖方）”与“全部外部补料”两种方案逐阶段给出缺口，并标注因补料不足而不能开工的部分。`earth-balance` 给出每批材料的产出、未运抵预留、在途、已投放、损耗和空闲数量；`earth-trace --target` 可从最终填筑经预留、回收链一路追溯到来源挖方、回收或补料批次。所有台账结果都按 `(stage, seq)` 重放，改变分块生成或记录载入顺序不会改变阶段地形、余额或重复记录判断（`dedup_key` 冲突会报错），保存重开后追溯关系完整保留。

## 测试

~~~bash
python -m pytest
~~~

测试覆盖不同分块生成次序、独立子进程和重开工程后的逐文件一致性、负坐标分块、局部重算后的编辑保留、跨分块道路边界零裂缝、道路/河道交汇处的优先级而非编辑先后、junction 小范围修订及其单独撤销、移除道路后河道保留与 junction 失效说明、预览不落盘、非法参数保留旧工程，以及完整 CLI 流程。
