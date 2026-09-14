---
title: ROB 与乱序执行完全指南：一次瓶颈会诊的完整拆解
module: rob
date: 2026-09-14
order: 13
summary: 用一道"三重瓶颈"题目建立 ROB 占用率 × IPC × 分支误预测时占用 的三指标诊断框架；讲清 ROB、调度队列、物理寄存器堆与 Load/Store Queue 的分工；再从 Tomasulo 与 Smith-Pleszkun 走到 Lion Cove、Zen 5、Neoverse V2 与 Apple M 系列的真实参数对比。
tags: [ROB, 乱序执行, 精确异常, 内存消歧, SMT, 微架构]
---
# ROB 与乱序执行完全指南：一次瓶颈会诊的完整拆解

> 一个 IPC 0.7 的循环里同时藏着三处病灶。本文先给你一套能把它们分开的诊断框架，再讲清 ROB 在硬件上到底做了什么、为什么长成今天这样，最后把四家厂商的真实参数摆在一起对比。

---

## 一、现象：三重瓶颈，与一套可复用的诊断框架

### 题目与基线数字

站点题目 [瓶颈会诊 — 诊断并修复](../problems/bottleneck-triage.html) 的基线代码把三种完全不同的瓶颈刻意混合在一个循环里：

```c
#define N 100000
long solve(int *arr, int n) {
    long sum = 0;
    for (int i = 0; i < n; i++) {
        int v = arr[i * 16];        /* ① 步长 16 个 int = 64 字节，每次一条新缓存行 */
        sum = sum + (v * 0);        /* ② 人为冗余依赖，语义上恒等，但制造一次串行化 */
        if (v & 1) sum += v;        /* ③ 随机数据上接近 50/50 的分支 */
    }
    return sum;
}
```

题目给出的基线指标是 **IPC 0.7、L1 MPKI 36.0、Branch MPKI 18.0**；目标是三项同时达成 **IPC > 3.0、L1 MPKI < 4.0、Branch MPKI < 2.0**。

先看几个能从代码逻辑直接推出来的数字：

- **访存**：`int` 是 4 字节，步长 16 个 `int` 恰好是 64 字节，也就是**每次迭代都落在一条全新的缓存行上，而只用了其中 4 字节**——缓存行利用率 4/64 = 6.25%。100,000 次迭代对应 100,000 × 64 B ≈ **6.25 MiB** 的工作集，这个规模已经超出典型的 1–2 MB 私有 L2，大部分访问会落到 L3 甚至内存。
- **打包之后**：把 `packed[i] = arr[i*16]` 预取成连续数组，元素数不变但总大小降到 100,000 × 4 B ≈ **390 KiB**，每条 64 B 行承载 16 个元素，只需 6,250 条缓存行。每 16 个元素才产生一次 L1 miss，若循环体指令数基本不变，**L1 MPKI 从 36 降到约 2.3**，满足 < 4 的目标。
- **分支**：`v & 1` 在随机数据上是 iid 无偏序列。正如 [分支预测完全指南](../reading/branch-prediction-guide.html) 里论证的，**没有任何预测器能在无偏 iid 序列上超过 50% 命中率**。Branch MPKI 18 意味着每 1000 条指令有 18 次误预测，即**平均每 55.6 条指令就清空一次窗口**。

### 只看 IPC，一定会误判

IPC 0.7 这个数字本身不携带任何病因信息：窗口被误预测的废指令填满、窗口等一次 300 周期的内存访问、或者循环相关的依赖链把发射节奏锁死——**这三种故障的表现都是 IPC 0.7，但修复手段完全不同且互相冲突**。对第一种要改数据布局或消除分支，对第二种要提升局部性与 MLP，对第三种要拆依赖链、加累加器。分不清就无法下手。

### 三指标交叉定位框架

真正的判据需要三个指标交叉：**ROB 占用率、IPC、以及分支误预测时刻的 ROB 占用**。

| ROB 占用率 | 误预测时占用 | 判据 | 该怎么修 |
|---|---|---|---|
| 高 | 高 | **前端 / 分支瓶颈**：窗口被错误路径上的无用指令填满 | 数据重排提高可预测性、改写为无分支、稳定划分 |
| 高 | 低 | **后端 / 访存瓶颈**：窗口是满的，但指令在等数据 | 提升局部性、分块、软件预取、提高 MLP |
| 低 | — | **依赖链瓶颈**：窗口里根本没几条指令 | 拆分依赖链、多累加器、重结合 |

三个指标缺一不可的原因在于：**"ROB 占用率高"本身有两种截然相反的成因**。窗口被填满，可能说明核心看到了大量可用并行度（只是被长延迟卡住），也可能说明核心看到了大量错误路径上的垃圾（每次误预测都要重来）。区分这两者的唯一办法，就是看**这些被占用的条目在误预测发生时是不是也要一起被丢掉**——如果"误预测时占用"同样高，说明窗口里装的很大一部分是注定作废的推测工作，病灶在前端。

### 定量锚点：Little's Law 与"窗口够不够"

上面这套判据需要一个定量支点，否则"高"和"低"没有标准。这个支点就是**排队论里的 Little's Law**：

> 稳态下，若 ROB 最老的那条未退休指令因某个事件阻塞了 L 个周期，那么在这 L 个周期里最多只能吞吐 ROB 容量那么多条指令。于是 **IPC ≤ ROB / L**，反过来 **要达到 IPC = T，必须满足 ROB ≥ T × L**。

Travis Downs 在 [Performance Speed Limits](https://travisdowns.github.io/blog/2019/06/11/speed-limits.html)（2019）里给了最直观的例子：Haswell 的 ROB 是 192 条目，一次典型的内存访问约 300 周期，于是这段代码的 IPC 上限就是 **192 / 300 = 0.64**——不管后端有多少执行单元都没用。

把这条定律套到 ROB = 512 的当代大核（Golden Cove 级别）上：

| 最老指令的阻塞来源 | 典型延迟 L | IPC 上界 = 512 / L |
|---|---|---|
| L1D 命中 | 约 5 周期 | 102 |
| L2 命中 | 约 14 周期 | 36 |
| L3 命中 | 约 45 周期 | 11 |
| 内存（DRAM） | 约 300 周期 | **1.7** |

（L2 / L3 延迟随型号与频率变化，这里取的是 x86 大核的常见量级；DRAM 的 300 周期是 Travis Downs 给出的典型值。）

这张表直接给出两个结论：

- **要达成 IPC > 3.0，最老指令的阻塞延迟必须压到 512 / 3 ≈ 171 周期以内。** 只要工作集还会碰到 DRAM（300 周期），IPC 上界就是 1.7，目标不可能达成。这就是"必须先把 6.25 MiB 压到 390 KiB"的定量理由——不是"缓存小一点快一点"，而是**把延迟档位从 DRAM 换到 L2，窗口上界从 1.7 跳到 36**。
- **反过来可以诊断**：用测到的 IPC 反解"有效头阻塞延迟" L_eff = ROB / IPC。若这个值大得离谱（远超任何一级访存延迟），说明核心根本不是在等某一条指令——窗口压根没被访存填满，病灶在别处。

### 为什么"改对了一项却没提速"

把修复后的稳态 IPC 写成四项的最小值，就一目了然：

```text
IPC ≈ min( 窗口上界 ROB / L ,  1 / (CPI_exec + m × P) ,  派发/退休宽度 ,  依赖链上界 )
       └── 访存          └── 分支（m = 每指令误预测数，P = 误预测代价）
```

基线里这三个瓶颈被刻意做得**势均力敌**，于是：

- **只打包（修访存）**：L 从 300 降到约 14，窗口上界抬到 36，访存这一项让位了。但分支还在：m = 18/1000 = 0.018，一次误预测要等分支解析（它依赖那条 load，L2 命中量级，加上重定向后重新填满，取 P ≈ 18 周期），分支项变成 1/(0.25 + 0.018×18) ≈ 1.8，仍远低于 3.0。**分支接管。**
- **只消分支（不打包）**：m → 0，分支项让位。但工作集仍是 6.25 MiB，窗口上界被钉在 512/300 = 1.7；即便部分访问命中 L3，缓存行利用率只有 6.25%，每取 64 字节只用 4 字节，内存带宽会先于窗口耗尽。**访存接管。**
- **只删掉 `v * 0` 这条冗余依赖**：这条依赖在当前配置下既不是窗口瓶颈也不是访存瓶颈，删掉它 IPC 几乎不动。**这正是"改对了却没提速"的典型形态——你修的东西不是当前的最大值项。**

三个瓶颈必须一起看，本质上是 **min() 的性质：只抬高其中一项，最大值项会立刻换人**。

---

## 二、硬件全景：ROB、调度队列、物理寄存器堆与 Load/Store Queue

### ROB 条目里到底装了什么

ROB 是一个按程序序分配的环形缓冲。每个条目在指令派发（dispatch / allocate）时创建，在指令退休（retire / commit）时释放。教科书模型里每个条目至少包含四类字段（[Imperial College 高级体系结构讲义, 2023](https://www.doc.ic.ac.uk/~phjk/AdvancedCompArchitecture/Lectures/pdfs/Ch02-part2-DynamicSchedulingWithPreciseExceptions.pdf)）：**指令类型、完成状态、目的寄存器、结果值**。现代实现还要在此基础上加上：

- **目的（物理）寄存器号 + 被覆盖的旧物理寄存器号**——后者用于退休时回收，也是回滚重命名表的依据；
- **PC**——异常要能精确定位到指令；
- **异常 / 故障标志**——执行阶段发现的异常不立即上报，而是先记在条目里（见 2.6 节）；
- **分支相关字段**——预测方向、预测目标、分支掩码；
- **向量相关字段**——AVX-512 操作掩码。Chips and Cheese 指出，Intel 自 Skylake 起把 AVX-512 掩码寄存器与 MMX/x87 寄存器放在**同一个物理寄存器堆**里分配，Lion Cove 这一堆约 166 条目；
- **访存相关字段**——指向 Load / Store Queue 对应条目的索引。

**具体字段布局与位宽厂商不公开**，以上是公开文档与教科书模型可推得的部分。

### 环形缓冲的头尾指针

分配在 `tail` 位置写入并严格按程序序；退休从 `head` 位置读出，每周期检查至多 W 条（退休宽度，Lion Cove 是 12，Zen 5 是 8）**已完成且未标异常**的条目，通过后释放并前移 `head`。满 / 空判据的经典做法是 `head == tail` 判空、`tail + 1 == head` 判满（牺牲一格以区分两态），或额外维护一个计数器。还有一个容易忽略的细节：**x86 上 ROB 在融合域计数**，一条宏融合后的 compare + branch 只占一格——这就是"减少 uop 数能等效扩大窗口"的原因。

### 最常见的误解：ROB 不是调度器

这一点值得单独强调，因为它是最普遍的错误认知。**乱序发射靠的是调度队列（Scheduler / Reservation Station），ROB 只负责顺序退休。**

Intel 自家的手册把两者的分工讲得非常清楚（[Intel Architecture Optimization Reference Manual, 1999](https://download.intel.com/design/PentiumII/manuals/24512701.pdf)）：乱序核心"把 uop 缓冲在**保留站**中，直到操作数就绪且资源可用"；而"缓冲**已完成** uop 的单元是重排序缓冲（ROB），ROB 按程序语义顺序更新架构状态，并管理异常的顺序"。Intel 另一位工程师 David Levinthal 在官方的[周期分析方法论](https://www.intel.com/content/dam/develop/external/us/en/documents/cycle-accounting-analysis-181827.pdf)里把发射的前置条件列为**三项并列**的资源：保留站空间、ROB 空间、以及 load/store buffer 空间——三者是不同东西，会分别成为瓶颈。

一句话总结：**调度队列决定"什么时候可以执行"，ROB 决定"什么时候可以退休"。** 指令可以在调度队列里已经执行完、结果已经广播出去，却因为前面还有一条未完成的指令而在 ROB 里继续占着格子。

### 四个结构的分工与数据流

| 结构 | 何时分配 | 何时释放 | 耗尽时的表现 | Lion Cove 容量 |
|---|---|---|---|---|
| ROB | 派发 / 重命名 | 退休 | 分配停顿（Intel `RESOURCE_STALLS.ROB`） | 576 |
| 调度队列（Scheduler / RS） | 派发 | 发射后 | `RESOURCE_STALLS.RS`，RS 空则 `RS_EVENTS.EMPTY` | 97 INT + 114 FP + 62 MEM |
| 物理寄存器堆（PRF） | 重命名时分配目的物理寄存器 | 同名指令退休后回收旧条目 | 重命名停顿 | 约 290 整数 / 约 406 浮点 |
| Load Queue | 派发 | 退休 | load 无法派发 | 约 189 |
| Store Queue | 派发 | store 提交后 | `RESOURCE_STALLS.SB` | 120 |

（Lion Cove / Redwood Cove / Zen 5 的容量均来自 [Chips and Cheese 的实测](https://old.chipsandcheese.com/2024/09/27/lion-cove-intels-p-core-roars)，2024 年，非厂商官方数字。）

完整的数据流是这样的：

```text
取指 → 译码 / 宏融合 → 重命名 → 派发 → 调度队列 → 发射 → 执行 → 写回 → 退休
                          │                                    │           │
                          ├─ 分配 ROB 条目（程序序）            │           └─ 释放 ROB 条目
                          ├─ 分配目的物理寄存器（PRF）          └── 写 PRF + 广播唤醒依赖者
                          └─ 访存指令额外分配 LQ / SQ 条目                   + 置完成标志
```

Intel 在 Lion Cove 上还给调度队列配了一层**非调度队列（non-scheduling queue）**：调度队列满时，重命名器可以把 uop 暂存在这里而不是直接停顿；里面的 uop 不参与发射选择，等调度队列腾出格子再进入。这套做法 AMD 的 Zen 和 Intel 自家的 E-core 早就在用（Zen 5 的非调度队列是 96 条目，来自 AMD 官方架构文档），Lion Cove 是第一次在 P-core 上引入。

### Load/Store Queue 与内存消歧

load 和 store 是最难乱序的一类指令，因为**内存地址的别名关系在执行前往往未知**。一条 load 想越过前面的 store 提前发射，就必须先证明两者地址不冲突——这就是**内存消歧（memory disambiguation）**。

三种基本结局：

1. **地址已知且不冲突** → load 提前发射，正常命中缓存；
2. **地址已知且重叠** → **store-to-load forwarding**：直接从 store queue 把数据转发给 load，不经过缓存；
3. **地址未知** → 要么保守等待（牺牲 MLP），要么**预测**"不冲突"并提前发射，事后若发现冲突则 **replay / 清空流水线重来**。

Henry Wong 在 [Store-to-Load Forwarding and Memory Disambiguation in x86 Processors](https://blog.stuffedcow.net/2014/01/x86-memory-disambiguation/)（2014）里实测了这些代价：在地址相同且对齐的"理想转发"下，Sandy Bridge / Ivy Bridge / Haswell 的转发延迟是 **5.32 / 5.00 / 5.07 周期**；但当 store 地址解析较晚、强制处理器去**推测**依赖时（fast data 变体），延迟恶化到 **9.80 / 13.01 / 12.21 周期**——不是完整清空，但代价是两倍多。而 Bulldozer / Piledriver 基本没有动态依赖预测器，一旦推测错误延迟就跳到 **35–40 周期**，那已经是流水线清空的量级了。

当代核心的实测数据（Chips and Cheese，2024）：

| 情形 | Lion Cove | Golden Cove | Zen 5 |
|---|---|---|---|
| 地址精确匹配 | 2 条/周期，0 周期 | 2 条/周期，0 周期 | 2 条/周期，0 周期 |
| load 被 store 包含但地址不等 | 8–9 周期 | 5–6 周期 | 7 周期 |
| 部分重叠（无法转发） | **19 周期** | 19–20 周期 | 14 周期 |

**"部分重叠"这一行很值得记住**：一次失败的 store forwarding 要 14–20 周期，比一次 L2 命中还贵。

最后，store 本身不能提前提交——它必须等到到达 ROB 头部、且前面所有指令都确认不会异常之后，才能把数据写进 L1D。这是顺序退休在内存侧的直接体现。

### 精确异常：为什么非得顺序退休

**精确异常**的判据来自 [Smith & Pleszkun 的经典论文](https://www.eecg.utoronto.ca/~moshovos/ACA07/readings/smith-interrupts.pdf)（ISCA 1985 / IEEE TC 1988）：保存的进程状态必须与"一条指令完成后才开始下一条"的顺序模型一致——即异常指令之前的指令都已提交，之后的指令都没有修改过架构状态。

**Tomasulo 算法做不到这一点。** 它是"顺序发射、乱序执行、乱序完成"，结果直接写进寄存器。同一篇论文明确指出，IBM 360/91 在某些情况下（例如浮点异常）会产生**不精确中断**，而这与 IBM 360 的架构约定相冲突。

ROB 的解法很优雅：**异常不在发生时上报，而是记在 ROB 条目里；等这条指令走到 ROB 头部、即将退休时才处理。** 此时可以保证——

- 它前面的所有指令都已经提交完毕；
- 它后面的所有指令要么已完成但结果还锁在自己的 ROB 条目 / 物理寄存器里（未成为架构状态），要么还没执行——把 `tail` 指针回退到异常指令之后，整批作废即可。

这也解释了为什么物理寄存器要保存"旧的那个"：异常回滚时，把重命名表恢复到异常点即可，因为架构寄存器堆从未被乱序写过。

### 分支误预测恢复：代价是"已投入的窗口深度"

分支同样占一个 ROB 条目。当它执行完、发现预测方向错误时，硬件要做的是：**把该分支之后的所有 ROB 条目一并作废，`tail` 指针回退，重命名表恢复到分支点的快照，然后从正确目标重新取指。**

因此误预测的代价**不是"流水线级数"，而是"从分支发射到解析之间已经填进窗口的指令数"**，量级约等于 `派发宽度 × 分支解析延迟`。这也解释了为什么现代核心的误预测惩罚并没有随流水线加深而暴涨——它主要受限于分支解析的延迟：

- P6（Pentium Pro, 1995）：**10–15 周期**（[基于 Shen & Lipasti 教材的 P6 案例研究](https://people.cs.clemson.edu/~mark/330/colwell/case_p6.html)）；
- AMD Zen 5：**12–18 周期**，常见 15（LLVM 调度模型，数据取自 AMD 官方 Software Optimization Guide）。

顺带纠正一个常见说法：现代处理器**确实**会在分支处保存重命名表快照（checkpoint）以加速恢复，但这**不能替代 ROB**——ROB 还承担着精确异常、store 顺序、以及物理寄存器回收的职责。更多预测器细节见 [分支预测完全指南](../reading/branch-prediction-guide.html)。

---

## 三、演进史：每一步解决了什么，引入了什么代价

| 阶段 | 解决了什么 | 引入了什么代价 |
|---|---|---|
| CDC 6600 计分板（1964） | 首次实现动态调度 | 集中式相关检测；遇到 WAR / WAW **假相关**就整条停 |
| Tomasulo / IBM 360/91（1967） | 保留站 + CDB 广播 + 寄存器重命名，消除 WAR/WAW | **乱序完成 → 无精确异常** |
| Smith & Pleszkun（1985）给出精确状态方案 | 证明"精确"的代价可控 | 需要额外的缓冲与旁路网络 |
| P6 / Pentium Pro（1995） | ROB 顺序退休换回精确异常，量产 x86 | ROB 条目要**存结果数据**，条目贵、面积大 |
| 现代 PRF 方案 | ROB 只存状态，数据放物理寄存器堆 | 需要独立的寄存器回收逻辑 |
| 分布式调度队列 | 缓解统一队列的唤醒广播代价 | 结构碎片化，需要非调度队列兜底 |
| SMT 与当代取舍 | 提升吞吐 | 窗口被分割，单线程可用深度减半 |

### Tomasulo：真正的乱序，但没有精确异常

Robert Tomasulo 在 1967 年的 [《An Efficient Algorithm for Exploiting Multiple Arithmetic Units》](https://doi.org/10.1147/rd.111.0025)（IBM Journal of Research and Development, 11(1):25–33）里提出了**保留站 + 公共数据总线（CDB）广播 + 寄存器重命名**三件套。相比 CDC 6600 的计分板，它的关键进步是：**假相关（WAR / WAW）不再导致停顿**——每条写同一架构寄存器的指令都被重命名为不同的物理目标，于是短指令可以越过卡住的长指令。论文原话说硬件可以"向前看大约八条指令"自动做局部优化。

代价是**乱序完成**：结果直接写回寄存器，异常发生时无法确定"到底该算在哪条指令头上"。这就是 360/91 的不精确中断问题。

### Smith & Pleszkun：五种方案，以及"收益饱和"的第一次观测

1985 年的这篇论文系统地给出了五种实现精确中断的方案，并在 Cray-1S 标量模型上用前 14 个 Livermore Loops 做了仿真：

1. **顺序完成 + 结果移位寄存器（RSR）**：快指令被慢指令挡住；
2. **重排序缓冲（ROB）**；
3. **ROB + 旁路（bypass）网络**；
4. **历史缓冲（History Buffer）**：改写寄存器前先把旧值存起来，异常时从尾到头回写恢复；
5. **未来文件（Future File）**：双份寄存器堆，一份是架构状态（顺序更新），一份是推测状态（乱序更新），异常时丢弃后者。

论文的量化结论（原文数字）：

- 方案 1 的性能损失 **23% 或 16%**（取决于 store 怎么处理）；
- 8 条目的 ROB，损失 **18% 或 12%**；
- ROB + 旁路，损失降到 **12% 或 3%**；
- 方案 4、5 与方案 3 **性能相当**，"最终三者之间的选择应基于实现成本与复杂度的工艺因素"。

还有一条对今天格外重要的观察：**"当缓冲条目超过十条时，仿真结果显示没有进一步的性能提升"（论文也跑了 15、16、20、25、60 条目的配置）。** 这是"加大乱序窗口有收益上限"这件事在文献里的第一次明确记录——当然，那是在 1985 年的标量流水线上，绝对数字不能直接外推，但**饱和现象本身是真实的**。

历史缓冲和未来文件并没有消失：**"架构寄存器堆 + 物理寄存器堆"的双份结构，本质上就是未来文件思想的现代形态**，只是它现在同时服务于寄存器重命名，而不再是为了异常回滚。

### P6：用顺序退休换回精确异常

1995 年的 Pentium Pro 第一次把这套理论做进量产 x86：x86 指令先译码成定长 uop，再走"顺序发射前端 → 乱序核心 → 顺序退休后端"三段。它的乱序窗口是 **40 条目 ROB + 20 条目保留站**，每周期最多发射 4 条 uop、退休 3 条 uop，误预测惩罚 10–15 周期。

代价很清楚：**在那个年代，ROB 条目是要保存结果数据的**（退休时才把数据从 ROB 写回引退寄存器堆 RRF）。条目里要放一条完整的结果，还要支持大量读写端口和旁路比较器，因此**每加一条 ROB 都非常贵**——这就是为什么 P6 只有 40 条。

### 现代 PRF 方案：解耦之后窗口才能做大

今天的设计把**数据**和**状态**分开：结果直接写进物理寄存器堆（PRF），ROB 条目只存指针和状态位。条目变便宜了，窗口才可能从几十涨到几百：

```text
P6 (1995) 40  →  Haswell (2013) 192  →  Skylake (2015) 224
             →  Sunny Cove ≈ 352* →  Golden Cove (2021) 512  →  Lion Cove (2024) 576
* Sunny Cove 数字由 Chips and Cheese 所述"Golden Cove 的 ROB 比 Sunny Cove 大 45%"反推得到
```

### 从统一调度队列到分布式

统一调度队列的问题在于**唤醒广播的代价随条目数增长**：每周期要把完成标签广播给所有条目并做比较。Intel 的拆分过程是一条清晰的脉络（Chips and Cheese, 2024）：

- 自 P6 起，整数与浮点共用**一个统一数学调度器**；
- Skylake 把地址生成（AGU）拆到独立调度队列；
- Sunny Cove 进一步拆分访存调度器，Golden Cove 又调整了拆法；
- **Lion Cove 首次把整数与浮点 / 向量彻底拆成两个调度器**，并相应地把重命名也按整数 / 向量分开——"这让人觉得 Intel 的核现在布局上很像 AMD 的 Zen 了"。

### SMT：分割还是共享？

SMT 下这些结构怎么分，是个真实的工程取舍。AMD Zen 首席架构师 Mike Clark 在 [Chips and Cheese 的访谈](https://old.chipsandcheese.com/2024/07/15/a-video-interview-with-mike-clark-chief-architect-of-zen-at-amd)（2024）里讲得很直接：

> "我们**静态分区**的是 retire queue、reorder buffer、store queue、micro-op queue……缓存和 TLB 是竞争共享的……另外我们还有 watermarking——对竞争共享的资源预留一部分，免得一个线程卡在长延迟上、醒来时发现资源已被另一个线程吃光。"

代价是单线程可用窗口直接减半：**AMD 官方口径的 Zen 5 ROB / 退休队列是 448 条目（1 线程）/ 每线程 224 条目（2 线程）**（LLVM 调度模型引自 AMD Software Optimization Guide）。

而 Intel 在 Lion Cove 这一代更激进：**物理移除 SMT**。据 [HWCooling 对 Intel 官方材料的整理](https://www.hwcooling.net/en/intels-new-p-core-lion-cove-is-the-biggest-change-since-nehalem/)（2024），移除超线程逻辑后核心面积缩小约 15%，同功耗下性能提升约 5%（单线程口径），还省掉了为区分线程归属而增加的验证复杂度与安全面。代价是多线程吞吐。这是"把窗口完整交给一个线程"与"把窗口切成两半换吞吐"之间的一次明确站队。

### 被淘汰的思路

- **计分板（CDC 6600）**：集中式检测，遇到 WAR / WAW 假相关就停。Tomasulo 的寄存器重命名就是针对它的改进。
- **结果移位寄存器（RSR）**：快指令必须为慢指令让路，论文实测损失 16–23%，是所有方案里最差的。
- **完整状态快照式恢复**：把整个机器状态（含部分执行的流水线内部状态）存成交换包、中断后再恢复（CDC CYBER 200 的 invisible exchange package 就是这么做的）。Smith & Pleszkun 评价说这"能让进程重启，但它是否具备架构级精确中断的全部特性是有争议的"。这条路在大窗口下完全不可行。

---

## 四、真实处理器对比

### Intel / AMD 的世代演进

| 微架构 | 年份 | ROB | 整数 PRF | 浮点 / 向量 PRF | 调度队列 | Load / Store Queue |
|---|---|---|---|---|---|---|
| Intel P6（Pentium Pro） | 1995 | 40 | —（ROB 存数据） | — | 20（统一保留站） | 未找到官方数据 |
| Intel Haswell | 2013 | 192 | 未找到官方数据 | 未找到官方数据 | 60 | 72 / 42 |
| Intel Skylake | 2015 | 224 | 未找到官方数据 | 未找到官方数据 | 97 | 72 / 56 |
| Intel Sunny Cove | 2019 | 约 352* | 未找到官方数据 | 未找到官方数据 | — | — |
| Intel Golden Cove | 2021 | 512 | 约 280 | 约 332 | 统一数学调度器 + 独立 AGU | 192 / 114 |
| Intel Redwood Cove | 2023 | 512 | 280 | 332 | 同上 | 192 / 114 |
| Intel Lion Cove | 2024 | **576** | 约 290 | 约 406 | 97 INT + 114 FP + 62 MEM（分布式） | 约 189 / 120 |
| AMD Zen 1 | 2017 | 192 | 168 | 160 | 分布式（每端口独立） | 未找到官方数据 |
| AMD Zen 3 | 2020 | 256 | 192 | 160 | 分布式 | 未找到官方数据 |
| AMD Zen 4 | 2022 | 320 | 224 | 192 | 1×24 ALU + 2×32 FP | 未找到官方数据 |
| AMD Zen 5 | 2024 | **448 / 224（1T / 2T）** | 240 | 384 | 88 ALU + 56 AGU + 3×38 FP，NSQ 96 | 未找到官方数据 |

说明：Intel 的 ROB / PRF / 队列条目数**官方基本不公布**，上表 Haswell 之后主要来自 [Chips and Cheese](https://old.chipsandcheese.com/2024/09/27/lion-cove-intels-p-core-roars) 与 [Travis Downs](https://travisdowns.github.io/blog/2019/06/11/speed-limits.html) 的**第三方实测（逆向工程，非官方数字）**；AMD 一列来自 **AMD 官方 Zen 5 架构文档**（[PDF](https://www.igorslab.de/wp-content/uploads/2024/08/Zen-5-Architecture.pdf)，2024）与 [LLVM 的 Znver5 调度模型](https://lists.llvm.org/pipermail/llvm-commits/Week-of-Mon-20250317/1590206.html)（2025）；Zen 1–4 的 ROB / PRF 数字来自公开整理的架构对比表。P6 一行来自 [Shen & Lipasti 教材的 P6 案例研究](https://people.cs.clemson.edu/~mark/330/colwell/case_p6.html)。

Chips and Cheese 还有一句评价值得引用：Golden Cove 的整数寄存器堆条目数**偏少**，在纯整数负载下"可能在 ROB 填满之前就先把整数寄存器用光，从而用不好它那个 512 条目的 ROB"。这正是下一节"加大 ROB 有收益上限"的现实注脚。

### ARM 与 Apple

| 微架构 | 年份 | ROB | 调度器 | Load / Store Buffer | 数据来源 |
|---|---|---|---|---|---|
| Arm Neoverse V2 | 2022 | **320** | 17 条发射流水线 | 未找到官方数据 | [Arm 官方 Neoverse V2 Software Optimization Guide](https://documentation-service.arm.com/static/668bc0a369e89f01e39c4668)（数据经 [LLVM 调度模型](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AArch64/AArch64SchedNeoverseV2.td) 转录） |
| Amazon Graviton 2（Neoverse N1） | 2020 | 约 124 | 约 48 | 约 62 / 40 | Travis Downs 实测 |
| Apple M1 Firestorm（P 核） | 2020 | **636**（实测 in-flight renames） | 326 | 130 / 60 | Travis Downs 实测 |
| Apple M1 Icestorm（E 核） | 2020 | 111 | 71 | 30 / 18 | Travis Downs 实测 |
| Apple M4 及以后 | 2024+ | 未找到官方数据（第三方估计 600+） | — | — | Apple 不公开微架构参数 |

关于 Apple 有两点必须说清。第一，**Apple 官方从不公开 ROB / 调度器 / LSQ 的条目数**，上表 M1 的数字全部来自第三方逆向实测（[Travis Downs, 2019](https://travisdowns.github.io/blog/2019/06/11/speed-limits.html)），**是非官方数字**；M4 及以后的具体数字**未找到官方数据，第三方估计在 600 以上**。第二，Apple 的 ROB 结构本身很特殊：据 [jia.je 的 M1 微架构逆向评测](https://jia.je/hardware/2024/12/26/apple-m1/)（2024），Firestorm 的 ROB **不是"一条 uop 一格"的平坦结构**，而是"约 330 个 group，每个 group 最多 7 条 uop"；有副作用的指令（load / store）只能放在 group 开头，所以用 load / store 去测只能测到约 325，用 NOP 去测却能得到 2000 以上。这也说明**跨厂商比较"ROB 条目数"这个指标本身是有歧义的**。

### 统一叙事：Intel 赌深度，AMD 赌宽度

同一代产品上，Intel Lion Cove 的乱序窗口是 **576**，AMD Zen 5 是 **448**——Intel 深了 28.6%。但拆开看，Zen 5 的派发 / 重命名 / 退休都是 **8 宽**（Zen 4 是 6 宽），并且有 6 个 ALU 与 4 个 AGU；Lion Cove 是 8 宽分配 / 重命名、**12 宽退休**，18 个执行端口。
当然，288 个条目也远远盖不住一次完整的 DRAM miss，这才是"更大窗口未必更快"的物理原因 —— 完整推导见 [内存层次与延迟隐藏](../reading/memory-hierarchy-latency-hiding.html)。

换句话说：**Intel 把预算押在"看得更远"（更深的窗口），AMD 把预算押在"每一拍走得更快"（更宽的前端与更多执行单元）**。这两种选择在真实负载上互有胜负——深窗口对长延迟、低 ILP 的代码更友好，宽机器对高 ILP、可向量化的代码更友好——而它们最终都受制于同一个事实：**窗口和宽度必须匹配，任何一边单独扩张都会被另一边顶住**。

---

## 五、延伸知识点

**1. ROB 不是调度器，这是最常见的误解。** 乱序发射由调度队列（Scheduler / RS）负责，ROB 只负责顺序退休与精确状态。Intel 官方手册的措辞是：uop 在保留站里"等到操作数就绪且资源可用"，而 ROB 缓冲的是"**已完成**的 uop"。一条指令可以执行完了却仍在 ROB 里占着格子。

**2. 窗口深度要匹配"延迟 × 吞吐"——ROB ≥ IPC × L。** 这是 Little's Law 的直接推论：想让 IPC 达到 T、而最老的未退休指令平均阻塞 L 周期，窗口至少要 T × L 条（Travis Downs 的 Haswell 例子：192 / 300 = IPC 0.64）。反过来说，**窗口不够深时，加大 ROB 和提高 IPC 是一回事；窗口已经够深时，加大 ROB 一分钱收益都没有**。

**3. 加大 ROB 的收益上限来自三个方向。** 一是**饱和**：Smith & Pleszkun 早在 1985 年就观测到，Cray-1S 上 ROB 超过 10 条目后仿真不再有性能提升（跑到了 60 条目）。二是**配套结构先耗尽**：Chips and Cheese 指出 Golden Cove 的整数寄存器堆条目偏少，纯整数负载下"可能在 ROB 填满之前就用光整数寄存器，用不好它那个 512 条目的 ROB"。三是**清空代价同步上升**：窗口越深，一次分支误预测作废的指令就越多，误预测率高的代码反而可能因为更大的 ROB 而变慢。

**4. 用 perf 定位 ROB 相关瓶颈（Intel 平台）。** 最直接的三个事件：`RESOURCE_STALLS.ROB`（EventSel `A2H`, UMask `10H`，统计 ROB 满导致的分配停顿周期，定义见 [Intel 官方 perfmon 事件库](https://perfmon-events.intel.com/platforms/broadwell/core-events/core/)）、`RESOURCE_STALLS.RS`（调度队列）、`RESOURCE_STALLS.SB`（store buffer）。配合 `CYCLE_ACTIVITY.STALLS_LDM_PENDING`（有未完成 load 且无指令执行）可以区分"窗口满"还是"在等内存"；用 `INT_MISC.RECOVERY_CYCLES` 看分支恢复占了多少周期。注意：**ARM / Apple 平台没有对应的公开事件名**，Apple 甚至不公开 PMU 事件定义。

**5. 一次失败的 store forwarding 比一次 L2 命中还贵。** 地址精确匹配时 store-to-load forwarding 是 0 周期、每周期 2 条；但"部分重叠"这种无法转发的情形，Lion Cove 是 **19 周期**，Zen 5 是 14 周期（Chips and Cheese 实测）。而"load 被 store 包含但地址不等"也要 7–9 周期。所以**让 load 和 store 的粒度与地址严格对齐**是有实际价值的，不是洁癖。

**6. 打开 SMT 后，你的窗口直接减半。** AMD 官方口径的 Zen 5 ROB 是 448 条目（单线程）/ 每线程 224 条目（双线程）；Mike Clark 确认 ROB、退休队列、store queue、micro-op queue 都是**静态分区**而非竞争共享。这意味着一个单线程应用在开启 SMT 的机器上，即使另一个逻辑核空闲，可用的乱序窗口也可能已经被切掉一半——**对延迟敏感、窗口受限的单线程负载，关掉 SMT 往往是净收益**。

**7. 减少 uop 数本身就是"变相加宽窗口"。** ROB 在融合域计数，宏融合（compare + branch 合成一条 uop）能省格子；更巧的是**软件预取**：预取指令在数据返回前就可以退休，不会占住 ROB 头部——"如果你在实际 load 之前 200 条指令处插入预取，相当于针对这条 load 把 ROB 加宽了 200 条"（Travis Downs）。这是少数几个能绕开硬件窗口限制的软件手段。

---

## 参考来源

- [An Efficient Algorithm for Exploiting Multiple Arithmetic Units（Tomasulo, IBM J. R&D, 1967）](https://doi.org/10.1147/rd.111.0025) —— 保留站 + CDB + 寄存器重命名的原始论文，支撑"Tomasulo 实现真乱序但无精确异常"（1967）
- [Implementing Precise Interrupts in Pipelined Processors（Smith & Pleszkun, ISCA 1985 / IEEE TC 1988）](https://www.eecg.utoronto.ca/~moshovos/ACA07/readings/smith-interrupts.pdf) —— 精确异常的定义、五种方案、百分比性能数据与"超过 10 条目不再有收益"的观测（1985/1988）
- [Intel Architecture Optimization Reference Manual（1999）](https://download.intel.com/design/PentiumII/manuals/24512701.pdf) —— 官方对"乱序核心缓冲在保留站、ROB 缓冲已完成 uop 并管理异常顺序"的表述（1999）
- [Cycle Accounting Analysis on Intel Core2 Processors（Levinthal, Intel）](https://www.intel.com/content/dam/develop/external/us/en/documents/cycle-accounting-analysis-181827.pdf) —— 发射所需三项资源（RS / ROB / load-store buffer）并列的官方方法论
- [Intel perfmon 事件库：RESOURCE_STALLS.ROB](https://perfmon-events.intel.com/platforms/broadwell/core-events/core/) —— `A2H / UMask 10H` 的官方定义，支撑"如何用 perf 定位 ROB 瓶颈"
- [Chips and Cheese: Lion Cove — Intel's P-Core Roars](https://old.chipsandcheese.com/2024/09/27/lion-cove-intels-p-core-roars)（2024）—— Lion Cove / Redwood Cove / Zen 5 的 ROB、PRF、LQ / SQ、调度器条目数与 store forwarding 延迟（第三方实测）
- [Chips and Cheese: Popping the Hood on Golden Cove](https://chipsandcheese.com/p/popping-the-hood-on-golden-cove)（2021）—— Golden Cove ROB 512、统一调度器布局、"整数寄存器堆先于 ROB 耗尽"（第三方实测）
- [Chips and Cheese: A Video Interview with Mike Clark](https://old.chipsandcheese.com/2024/07/15/a-video-interview-with-mike-clark-chief-architect-of-zen-at-amd)（2024）—— AMD Zen 首席架构师确认 SMT 下 ROB / 退休队列 / store queue 静态分区
- [AMD Zen 5 官方架构文档（PDF）](https://www.igorslab.de/wp-content/uploads/2024/08/Zen-5-Architecture.pdf)（2024）—— Zen 5 的 ROB/退休队列 320→448、GPR 240、向量寄存器 384、NSQ 96、8 宽派发
- [LLVM Znver5 调度模型提交](https://lists.llvm.org/pipermail/llvm-commits/Week-of-Mon-20250317/1590206.html)（2025）—— 引述 AMD SOG：ROB 448（1T）/ 224（2T）、误预测惩罚 12–18 周期
- [Arm Neoverse V2 Core Software Optimization Guide（官方 PDF）](https://documentation-service.arm.com/static/668bc0a369e89f01e39c4668) —— Neoverse V2 微架构参数的一手来源
- [LLVM AArch64 Neoverse V2 调度模型](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AArch64/AArch64SchedNeoverseV2.td)（2024）—— 转录自 Arm SOG：ROB 320 条目、17 条发射流水线
- [Travis Downs: Performance Speed Limits](https://travisdowns.github.io/blog/2019/06/11/speed-limits.html)（2019）—— ROB/LB/SB 实测表、Haswell 192/300 = IPC 0.64 的 Little's Law 示例、Apple M1 Firestorm 636
- [jia.je: Apple M1（Firestorm & Icestorm）微架构评测](https://jia.je/hardware/2024/12/26/apple-m1/)（2024）—— Firestorm ROB 的 group 结构与"测出 325 还是 2277"的解释（第三方逆向）
- [Henry Wong: Store-to-Load Forwarding and Memory Disambiguation in x86 Processors](https://blog.stuffedcow.net/2014/01/x86-memory-disambiguation/)（2014）—— 转发成功 / 推测失败 / 无预测器时的实测周期数
- [Intel P6 Microarchitecture Case Study（Clemson，基于 Shen & Lipasti 教材）](https://people.cs.clemson.edu/~mark/330/colwell/case_p6.html) —— P6 的 40 条目 ROB、20 条目保留站、10–15 周期误预测惩罚
- [Imperial College 高级体系结构讲义：Dynamic Scheduling with Precise Exceptions](https://www.doc.ic.ac.uk/~phjk/AdvancedCompArchitecture/Lectures/pdfs/Ch02-part2-DynamicSchedulingWithPreciseExceptions.pdf)（2023）—— ROB 条目字段（指令类型 / 状态 / 目的 / 值）与推测式 Tomasulo 四阶段
- [HWCooling: Intel's new P-Core — Lion Cove](https://www.hwcooling.net/en/intels-new-p-core-lion-cove-is-the-biggest-change-since-nehalem/)（2024）—— Lion Cove ROB 576、移除 SMT 及其官方给出的代价 / 收益

---

## 系列导航：处理器微架构深度指南

本文是《处理器微架构深度指南》系列之一。

**同系列其他文章**：

- [分支预测完全指南：从一次数据重排优化说起](branch-prediction-guide.html)
- [编译器如何自动优化分支预测](compiler-branch-optimization.html)
- [缓存层次完全指南：从一次矩阵乘法分块说起](cache-hierarchy-complete-guide.html)
- [ILP 与流水线完全指南：从八累加器点积说起](ilp-and-pipeline-complete-guide.html)
- [内存层次与延迟隐藏：从指针追逐说起](memory-hierarchy-latency-hiding.html)
- [前端与指令缓存：为什么过度展开反而更慢](frontend-and-instruction-cache.html)
- [SIMD 与向量化完全指南：从 SAXPY 到 AVX10 与矩阵扩展](simd-vectorization-complete-guide.html)
- [TLB 与地址转换：从一次聚集重排说起](tlb-address-translation-guide.html)

所属模块：[ROB 与乱序执行](../modules/rob.html)
