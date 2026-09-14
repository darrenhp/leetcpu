---
title: 前端与指令缓存：为什么过度展开反而更慢
module: frontend
date: 2026-09-14
order: 14
summary: 循环展开能抬高 ILP，但展开倍数越过某个拐点后，循环体会依次撑爆 loop buffer、uop cache 与 L1 指令缓存，前端从"透明"变成新瓶颈。本文设计一个可复现的展开倍数实验，给出三条硬阈值的手算方法，并横向对比 Intel、AMD、ARM、Apple 四家 2024 年前后的前端参数。
tags: [前端, 指令缓存, uop cache, 循环展开, 微架构]
---
# 前端与指令缓存：为什么过度展开反而更慢

> 展开 4 倍比 2 倍快，展开 16 倍却比 8 倍慢——这条先升后降的曲线背后，是三条可以被手算出来的硬阈值。

---

## 一、一个展开倍数实验：三道台阶

### 实验设计

`frontend` 模块目前没有配套题目，所以我们自己设计一个最小实验。原则有三条：**循环体不含访存**（避免内存带宽成为瓶颈）、**不含长依赖链**（避免后端成为瓶颈）、**循环体的字节数与 μop 数可以从汇编直接数出来**（保证数字可推导而不是"据说"）。

```asm
; 一个"展开单元"：4 条 add r64, r64，共 12 字节 / 4 μop
48 01 D8    add rax, rbx
48 01 D9    add rcx, rbx
48 01 DA    add rdx, rbx
48 01 DE    add rsi, rbx
; 循环尾：dec + jne 会被宏融合，共 4 字节 / 1 μop
FF C9       dec ecx
75 xx       jne .L
```

把展开单元复制 K 份（K = 1 / 2 / 4 / 8 / 16），后面接同一段循环尾，用 `taskset` 绑核跑 10^9 次迭代。对一台 Skylake 及以后的 Intel 机器（或 Zen 4 / Zen 5），可以直接数出下面这张表：

| 展开倍数 K | 循环体机器码字节 | 融合后 μop 数 | 落在哪条供给路径 |
|---|---|---|---|
| 1 | 16 | 5 | LSD（循环流检测器） |
| 2 | 28 | 9 | LSD |
| 4 | 52 | 17 | LSD |
| 8 | 100 | 33 | LSD |
| 16 | 196 | 65 | 溢出 → DSB（uop cache） |

65 > 64，K = 16 恰好越过第一道门槛：Skylake 及后续 Intel 核心的 LSD 容量是 **64 μop**（第三方实测值，见 [Leaky Frontends](https://arxiv.org/abs/2105.12224)，2021）。K = 8 的 33 μop 还在里面，K = 16 就掉出去了。

### 三道台阶的具体数字

这个实验的用意不是"16 倍一定更慢"，而是把三条独立的阈值摆出来。设展开单元是 `B` 字节 / `U` 个融合后 μop，循环尾固定 `T` 字节 / `V` 个 μop，那么：

| 台阶 | 约束条件 | 本例的 K 上限 | 越过之后发生什么 |
|---|---|---|---|
| 一、LSD（loop buffer） | `U·K + V ≤ 64` | **K ≤ 15** | 从"零取指、零译码"降到 uop cache 供给 |
| 二、DSB（uop cache）容量 | `U·K + V ≤ 1536` | K ≤ 383 | 部分 μop 回落到传统译码（MITE） |
| 三、L1 指令缓存 | `B·K + T ≲ 0.7 × 32 KB` | K ≤ 1950 | L1I MPKI 上升，每次 miss 打到 L2 |

注意第二行还有一条**比容量更早生效的硬约束**：Intel 的 DSB 是 32 组 × 8 路 × 每行 6 μop = 1536 μop，而**一个 32 字节的取指窗口最多只能占 3 路**，也就是 18 μop；产生超过 18 μop 的 32 字节窗口**永远不会被装进 DSB**，只能走传统译码路径（[WikiChip: Sandy Bridge](https://en.wikichip.org/w/index.php?oldid=98305&title=intel%2Fmicroarchitectures%2Fsandy_bridge_%28client%29)）。本例每 12 字节 4 μop，折算每个 32 字节窗口约 10.7 μop，安全；但如果循环体被调度成一串 3 字节以内的短指令、密度超过 18 μop/32B，再大的 DSB 也救不了。

### 实测锚点：三道台阶确实存在

耶鲁大学 Deng、Huang、Szefer 在 [Leaky Frontends](https://arxiv.org/abs/2105.12224)（arXiv 2105.12224，2021）里做过一个和上面同构的实验——构造分别含 40 / 400 / 4000 个 μop 的循环，各跑 2000 万次，用 Linux `perf` 读 `idq.dsb_uops`、`idq.mite_uops`、`lsd.uops`：

- **40 μop**：全部由 LSD 供给（未超过 64 μop 上限）
- **400 μop**：LSD 装不下，但 DSB 的 1536 μop 装得下，全部走 DSB
- **4000 μop**：DSB 只能覆盖一部分，其余回落到 MITE（传统译码），两条路径混合

三次实验的时序和功耗都有可测差异，这正是他们构造侧信道的基础。换句话说，"循环多大就会换一条供给路径"不是理论推断，是能用性能计数器量出来的。

再补一个更极端、也更日常的实测：在 Skylake 上把同一个函数的入口偏移从 24 字节改成 28 字节（仅 4 字节之差，刚好跨过一个 32 字节窗口边界），单次调用从 **2.392 ns 变成 2.870 ns（+20%）**，IPC 从 **3.19 掉到 2.66**。性能计数器显示 `idq.dsb_uops` 几乎不变（16.17 亿 vs 16.16 亿）、`dsb2mite_switches.penalty_cycles` 只有 3 万 vs 2 万（可忽略）、`icache_16b.ifdata_stall` 也没变化，唯独 `idq_uops_not_delivered.core` 翻倍（4.06 亿 → 8.10 亿）——瓶颈不在"有没有命中"，而在"每个周期能交付多少"（[The Alignment Cliff](https://shubhankar-gambhir.github.io/posts/the-alignment-cliff-code-alignment-and-the-skylake-micro-op-cache)，第三方实测）。

### 一个必须给出的限定条件

Intel 官方优化手册自己就写了：**"Software should use the loop cache functionality opportunistically. Loop unrolling and other code optimizations may make the loop too big to fit into the LSD. For high performance code, loop unrolling is generally preferable for performance even when it overflows the loop cache capability."**（[Intel 64 and IA-32 Architectures Optimization Reference Manual](https://www.cs.princeton.edu/courses/archive/spring13/cos217/reading/ia32opt.pdf)）

也就是说：**第一道台阶（LSD）通常不值得为它放弃展开**。真正让"过度展开反而更慢"成立的是第二、第三道台阶——uop cache 容量溢出与 L1I 容量溢出。这个区分很重要，否则会得出"永远不要展开"的错误结论。相关的取舍判断可以配合站点上的 [瓶颈会诊](../problems/bottleneck-triage.html) 一起看：先确认瓶颈到底在不在前端。

---

## 二、硬件全景：从取指地址到 IDQ

### 全链路与分工

现代高性能核心的前端是一条多级流水线，每一级都在为下一级"提前准备"：

| 阶段 | 做什么 | 关键约束 |
|---|---|---|
| 分支预测 | 给出下一段取指地址 | BTB / TAGE / 感知机预测器，**见 [分支预测完全指南](../reading/branch-prediction-guide.html)**，本文不展开 |
| 取指地址生成 | 把预测目标变成 I-cache 索引 | 每周期能推进几个取指块 |
| L1 指令缓存 | 取出机器码字节 | 容量、关联度、每周期取指字节数 |
| 预译码 | 找变长指令的边界、打标记 | x86 的独有开销；ARM 用定长 + 预译码位规避 |
| 译码 | 机器码 → μop | 译码宽度（几条指令/周期） |
| uop cache | 命中时**完全跳过**上面三级 | 容量、路数、每 32B 窗口的 μop 上限 |
| IDQ / 分配队列 | 顺序前端与乱序后端的接口 | 队列深度决定了能吸收多少前端气泡 |

**这篇文章只覆盖后五级**，尤其是 uop cache 与 L1I。发射宽度、端口压力属于 [ILP 与流水线](../reading/ilp-and-pipeline-complete-guide.html) 篇的范畴，不在这里重复。

### uop cache 什么时候会 miss

Intel 把传统译码路径叫 MITE（Micro-Instruction Translation Engine），把 uop cache 叫 DSB（Decoded Stream Buffer）。命中 DSB 时，取指、预译码、译码三级全部被跳过，其余前端可以被时钟门控——这是它同时省性能和省功耗的原因（[Chips and Cheese: Sandy Bridge](https://chipsandcheese.com/p/sandy-bridge-setting-intels-modern-foundation)）。

已知的失效条件可以归成四类：

- **窗口超限**：一个 32 字节窗口产生超过 18 μop（3 路 × 6 μop），永不入 DSB
- **布局问题**：只支持完整的 32B 窗口，部分命中必须重走 MITE；代码跨 32B / 64B 边界会额外消耗 μop
- **指令类型**：微码指令（>4 μop 的指令）、长度改变前缀（LCP）等会限制或禁止入缓存
- **一致性**：DSB 是虚拟地址寻址的，L1I 与 iTLB 对它**是包含关系**；一次 iTLB miss 或 iTLB 驱逐会连带把 DSB 冲掉（[Chips and Cheese](https://chipsandcheese.com/p/sandy-bridge-setting-intels-modern-foundation)）

WikiChip 给出的 Sandy Bridge DSB 平均命中率约 80%。也正因为命中率不如传统 L1I，Intel 只在**分支之后**才切到 DSB，避免一直在查标签白烧功耗。

### 融合：用一条 μop 的价钱买两条指令

- **micro-fusion（微融合）**：把同一条指令产生的多个 μop 合成一个"复杂 μop"。所有 store、所有 load+op、load-and-jump、以及带立即数与内存的 `CMP`/`TEST` 都可以微融合（[Intel Optimization Manual](https://www.cs.princeton.edu/courses/archive/spring13/cos217/reading/ia32opt.pdf)）。注意**un-lamination**：索引寻址的 load+op 与 store 在译码器/DSB 里是一个 μop，进入 IDQ 后被拆成两个，因为 SnB/IvB 的融合域 μop 最多允许 2 个输入、Haswell 之后是 3 个（[easyperf: MicroFusion in Intel CPUs](https://easyperf.net/blog/2018/02/15/MicroFusion-in-Intel-CPUs)）。所以微融合能省 DSB 空间和译码带宽，但不一定省后端。
- **macro-fusion（宏融合）**：把两条指令合成一个 μop。第一条必须修改标志位（Nehalem 只有 `CMP`/`TEST`；Sandy Bridge 起加入 `ADD`/`SUB`/`AND`/`INC`/`DEC`），第二条必须是条件分支，且第一条的源/目的必须是寄存器。手册里有一条特别值得记住的边界条件：**如果第一条指令结束在 cache line 的第 63 字节、而条件分支从下一行的第 0 字节开始，融合不会发生**——这就是"代码对齐"能直接影响 μop 数的硬件原因。

### ARM / Apple 侧的差异

AArch64 是定长 32 位指令集，找指令边界几乎是免费的，译码开销天然比 x86 小得多。ARM 的处理办法是**预译码**：Cortex-X925 的技术参考手册显示 L1I 的数据按 **76 位**粒度存放——正好两条 32 位指令加 12 位附加信息（[Chips and Cheese: Cortex-X925](https://chipsandcheese.com/p/arms-cortex-x925-reaching-desktop)，2026）。也就是说指令边界信息在填充 L1I 时就写进去了，之后每周期只要读两次就能拿到两条指令。

关于"现代 ARM 大核是否引入 uop cache"，证据指向的是**相反方向**，值得谨慎表述：Chips and Cheese 明确指出 Cortex-X925 和 A725 一样**去掉了前几代的 MOP cache**，理由是 ARM 已经用预译码和更低的时钟频率把译码成本压住了，再加一级 MOP cache 不划算。Wikipedia 汇总的 ARM 公开资料对比表把 Cortex-A78 / Cortex-X1 / Cortex-X2 的 L0 MOP 项数分别列为 1536 / 3072 / 1536，Cortex-X3 之后标记为无（第三方汇总，非 ARM 官方原件）。所以更准确的说法是：**ARM 试过 MOP cache，然后在旗舰大核上又扔掉了**。Apple 侧则是"未找到官方数据"，下文 Part 4 会说明为什么它可能根本不需要。

---

## 三、演进史：四次"绕过译码"的尝试

### 1. 顺序取指 → 分支预测辅助取指

朴素方案是取完一条（或一块）指令、等分支 resolved 再取下一块，遇到分支就停。解决的问题是"正确性"，代价是每次分支都要空转。分支预测辅助取指把它变成"猜一个地址先取下去"——代价是猜错要清空重来。预测器本身的演进（BTB 层级、TAGE、感知机）见 [分支预测完全指南](../reading/branch-prediction-guide.html)。

### 2. x86 的译码墙 → Sandy Bridge 的 uop cache

x86 指令长度从 1 到 15 字节不等，预译码器必须先扫一遍找边界，才能把指令分发给并行译码器。在 4 宽译码的 Sandy Bridge 上，传统路径的取指窗口只有 **16 字节/周期**——如果平均指令长度超过 4 字节，就填不满 4 个译码槽（[WikiChip](https://en.wikichip.org/w/index.php?oldid=98305&title=intel%2Fmicroarchitectures%2Fsandy_bridge_%28client%29)）。

Sandy Bridge 的解法是**缓存译码结果**而不是缓存指令字节：DSB 直接对应 32 字节对齐的内存区域，命中时以 4 μop/周期直送 IDQ，其余前端全部门控。得到的是带宽、延迟、功耗三重收益；代价是新增一块 SRAM、以及"虚拟地址寻址 + L1I/iTLB 包含"带来的一致性复杂度。

### 3. ARM / RISC-V 这类定长指令集需要 uop cache 吗

从第一性原理看，定长指令集的译码开销小得多，uop cache 的收益也小得多。ARM 的实际路径印证了这一点：大核曾经配过 1.5K~3K 项的 MOP cache，到 Cortex-X3 / X925 这一代干脆删掉了，改为靠预译码 + 更大的 L1I + 更宽的取指。Zen 5 的架构师在 Hot Chips 2024 上也表达过类似的取舍逻辑：任何特性都有工程成本，要和收益一起称（[Chips and Cheese: Discussing Zen 5 at Hot Chips](https://old.chipsandcheese.com/2024/09/15/discussing-amds-zen-5-at-hot-chips-2024/)）。

需要强调：这是一个"收益/成本比"问题，不是"先进/落后"问题。x86 核心跑 5 GHz+、流水线深、译码贵，uop cache 是刚需；ARM 大核目标频率低得多，删掉它是理性的。

### 4. loop buffer 的兴衰

loop buffer（Intel 叫 LSD / Loop Stream Detector）是"最小循环的特供方案"：检测到 IDQ 里的小循环就锁住它反复重放，连 uop cache 都不用查。Sandy Bridge 要求循环小于 **28 μop**（IDQ 每线程 28 项），Skylake 一代放宽到 **64 μop**（第三方实测）。

它的衰落是个很好的"结构被淘汰"案例。AMD 在 Zen 4 上有一个 144 项的 loop buffer，Zen 5 直接没有了。Chips and Cheese 向 AMD 工程师核实后的说法很有意思：**loop buffer 不是被"删掉"的，而是 Zen 5 的前端是全新设计、它压根没被加回来**；而且它原本主要是一个功耗优化——让 Zen 4 在小循环里关掉大部分前端——对 IPC 只是偶有帮助。既然 op cache 已经提供了足够的带宽，为它付出的工程成本就不划算了（[Chips and Cheese](https://chipsandcheese.com/p/amds-ryzen-9950x-zen-5-on-desktop)，2024）。

Intel 一侧，Lion Cove 的前端有四个 μop 来源，但角色非常不均衡：**loop buffer 和微码序列器只起次要作用，绝大多数 μop 来自 DSB**（[Chips and Cheese: Lion Cove and Gaming](https://old.chipsandcheese.com/?p=36842)）。

### 5. 被淘汰的思路：Pentium 4 的 trace cache

NetBurst（Pentium 4）做过一次远比 uop cache 激进的尝试：用 **12K μop 的 execution trace cache 直接取代 L1 指令缓存**。它不按内存地址组织，而是按"实际执行过的动态路径"组织——同一条指令如果是多个分支的目标，就会在 trace cache 里存多份，目的是把 taken branch 在取指逻辑眼里变成 not-taken，从而绕过 taken branch 的带宽损失（[Chips and Cheese: Intel's Netburst](https://chipsandcheese.com/p/intels-netburst-failure-is-a-foundation-for-success)）。

它为什么没延续下来：

- **miss 代价极高**：trace cache 取代了 L1I，miss 只能去 L2 取指，而 NetBurst 的 L2 延迟约 11 周期，再叠加 20+ 级流水线
- **存储效率差**：重复存放导致 12K μop 的命中率只相当于 8–16 KB 的 I-cache；而 Sandy Bridge 1.5K μop 的 DSB 相当于 6 KB I-cache，单位面积效率高出 4 倍以上（[WikiChip 引 Intel 数据](https://en.wikichip.org/w/index.php?oldid=98305&title=intel%2Fmicroarchitectures%2Fsandy_bridge_%28client%29)）
- **复杂度爆炸**：需要配套的 trace BTB、trace 构建逻辑，而且上下文切换要 flush

后世的 uop cache 吸取的教训是：**不要取代 L1I，只做它的一个补充层**；miss 了就退回传统路径，代价可控。

---

## 四、真实处理器对比

下表每个数字都标了来源与年份。凡厂商未公开、只能靠第三方逆向或实测的数字，都显式标注。

| 维度 | Intel | AMD | ARM | Apple |
|---|---|---|---|---|
| 代表核心（年份） | Sandy Bridge 2011 → Golden/Redwood Cove 2021–2023 → Lion Cove 2024 | Zen 4 2022 / Zen 5 2024 | Cortex-X925 2024 / Neoverse V3 | M4（P-core）2024 |
| L1I 容量 | 32 KB 8-way（Sandy Bridge，[Intel 手册](https://www.cs.princeton.edu/courses/archive/spring13/cos217/reading/ia32opt.pdf)）→ **64 KB**（Redwood / Lion Cove，[Chips and Cheese 2024](https://old.chipsandcheese.com/?p=36842)） | **32 KB 8-way**，Zen 4 与 Zen 5 相同，64 B/周期取指（[AMD Hot Chips 2024 官方幻灯片](https://www.igorslab.de/wp-content/uploads/2024/08/Zen-5-Architecture.pdf)） | X925 **64 KB**（[Chips and Cheese 2026](https://chipsandcheese.com/p/arms-cortex-x925-reaching-desktop)）；Neoverse V3 **64 KB 4-way 64B line**（[Arm TRM](https://developer.arm.com/documentation/107734/0001/Technical-overview/Core-components?lang=en)） | **192 KB 6-way 64B line**（sysctl 官方值 + Apple Silicon CPU Optimization Guide，[实测复核](https://jia.je/hardware/2025/05/21/apple-m4/)）；E-core 128 KB |
| uop cache | DSB **1536 μop**（32 组 × 8 路 × 6 μop，Sandy Bridge，[WikiChip](https://en.wikichip.org/w/index.php?oldid=98305&title=intel%2Fmicroarchitectures%2Fsandy_bridge_%28client%29)）→ 4096 项 8-way（Redwood Cove）→ **5250 项 12-way，输出 12 μop/周期**（Lion Cove，[Intel 官方演讲整理](https://www.realworldtech.com/forum?curpostid=218335&threadid=218335)、[参数汇总](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html)） | Op Cache **6.75K 项 8-way** → Zen 5 **6K 项 16-way**，每周期 12 macro-op（2 管道 × 6）；未命中时每线程只有 4 指令/周期（[Chips and Cheese 2024](https://chipsandcheese.com/p/amds-ryzen-9950x-zen-5-on-desktop)、[AMD 官方幻灯片](https://www.igorslab.de/wp-content/uploads/2024/08/Zen-5-Architecture.pdf)） | **无 MOP cache**（X925 与 A725 均已删除，[Chips and Cheese 2026](https://chipsandcheese.com/p/arms-cortex-x925-reaching-desktop)） | **未找到官方数据** |
| loop buffer | LSD 28 μop（Sandy Bridge）→ 64 μop（Skylake 代，第三方实测）；Lion Cove 上退居次要角色 | Zen 4 有 144 项；**Zen 5 删除** | 无 | 未找到官方数据 |
| 取指 / 译码宽度 | 传统路径 16 B/周期（Sandy Bridge）；Lion Cove **128 B/周期从 L1I 取指 + 8 指令/周期译码**（[Intel 官方演讲整理](https://www.realworldtech.com/forum?curpostid=218335&threadid=218335)） | 4 宽 × 2 条译码管道（单线程只用其中一簇，[Hot Chips 侧访](https://old.chipsandcheese.com/2024/09/15/discussing-amds-zen-5-at-hot-chips-2024/)） | X925 **10 路译码**，实测持续 10 指令/周期（[Chips and Cheese 2026](https://chipsandcheese.com/p/arms-cortex-x925-reaching-desktop)） | 取指 **16 指令/周期**（实测）；持续 uops/cycle M1→M4 = 8 / 8 / 9 / **10**（[Apple 官方优化指南 + 实测复核](https://jia.je/hardware/2025/05/21/apple-m4/)） |

### 三个值得单独拎出来说的点

**第一，Apple 的 192 KB L1I 是这张表里最反常的数字。** 它是 Zen 5（32 KB）的 6 倍、Lion Cove（64 KB）的 3 倍，而且这不是猜测——`sysctl hw.perflevel0.l1icachesize` 直接返回 196608。实测也完全吻合：构造一个巨大指令 footprint 的 `nop` 循环，footprint 在 192 KB 之前能稳定跑满 10 IPC，**一旦越过 192 KB，IPC 断崖式掉到 2.5**（[Apple M4 微架构评测](https://jia.je/hardware/2025/05/21/apple-m4/)，2025）。这是本文能拿到的最干净的一条"L1I 容量悬崖"曲线。一种合理的推测（**非厂商确认**）是：Apple 用"超大 L1I + 定长指令 + 16 指令/周期取指"直接解决了指令供给问题，从而不需要 x86 那种 uop cache；但 Apple 未公开前端细节，这里只能停留在推测。

**第二，Intel 和 AMD 在 uop cache 上做了相反的方向调整。** Zen 5 把容量从 6.75K 缩到 6K，同时把相联度从 8 路翻倍到 16 路、并让每个 entry 可以存最多 6 条（融合后的）指令——净结果是实测 op cache 命中率不降反升（[Chips and Cheese](https://chipsandcheese.com/p/amds-ryzen-9950x-zen-5-on-desktop)）。Intel 则是容量和路数一起加：4096 项 8 路 → 5250 项 12 路。两家的共同点是：**都在提高 uop cache 的等效覆盖率，因为它是主要 μop 来源**。

**第三，uop cache 不是"越宽越有用"的银弹。** Zen 5 上第三方实测关闭 op cache 后性能下降约 23%（[David Huang 的实测](https://blog.hjc.im/?p=574/)，非官方数字），说明它确实是主力；但 Lion Cove 把译码器从 6 路加宽到 8 路，在 SPEC 里几乎没吃到红利——因为绝大多数工作负载的 op cache 命中率都很高，译码器根本没机会成为瓶颈（[Chips and Cheese](https://old.chipsandcheese.com/?p=36842)）。译码宽度是"兜底能力"，不是"日常能力"。

---

## 五、延伸知识点

**1. 展开倍数的拐点怎么估。** 从循环体的汇编里数出展开单元的字节数 `B` 和融合后 μop 数 `U`，然后套三条不等式：`U·K ≤ LSD 容量`（Intel 现代核心 64 μop，Zen 5 无此层）、`U·K ≤ uop cache 容量`（Sandy Bridge 1536、Lion Cove 5250、Zen 5 6K）、`B·K ≲ 0.7 × L1I 容量`。再补一条密度检查：每 32 字节窗口的 μop 数不要超过 18（Intel）。三条里最先被违反的那条就是你的拐点。**注意第一道台阶通常不值得为它牺牲展开**，见 Intel 手册原文。

**2. `-funroll-loops` 与 PGO 的取舍。** 全局 `-funroll-loops` 会把所有循环一起撑大，热区 footprint 膨胀，冷代码也被波及。更稳的做法是 PGO：让编译器只对真正热的循环做激进展开，同时把冷分支移出去。LLVM 自己就有对应的护栏——部分展开的阈值由 `LoopMicroOpBufferSize` 控制，而这个值就是按目标的 uop cache 大小设的（[LLVM issue #42332](https://gitmemories.com/llvm/llvm-project/issues/42332)，Agner Fog 参与讨论），说明"展开到 uop cache 装不下就亏"已经是编译器社区的共识。

**3. 代码对齐不是玄学。** 两个已核实的硬件事实支撑它：（a）Intel 的宏融合在第一条指令结束于 cache line 第 63 字节、条件分支起于下一行第 0 字节时不发生；（b）DSB 只认完整 32 字节窗口，跨边界的窗口要额外消耗 μop。前面那组实测数据更是把代价量化了：偏移 24 → 28 字节，IPC 3.19 → 2.66。`-falign-functions=32`、`-falign-loops=32` 值得一试，但一定要实测，对齐填充本身也会增大 footprint。

**4. 把冷代码移出去能提速，机制是 I-cache 而不是分支。** `__attribute__((cold))` / `__builtin_expect` 的真正收益常常不在"少猜错一次分支"，而在于让错误处理、日志、边界检查这些冷路径**不要和热循环共享 64 字节的 cache line 与缓存组**。热区的指令 footprint 越小，L1I MPKI 越低。这和 [查表法替代嵌套分支](../problems/lookup-table-grade-bands.html) 里的取舍是同一类：用一点点数据侧代价换指令侧局部性。

**5. LTO / 内联膨胀是前端的隐形杀手。** LTO 让内联跨越翻译单元边界，单个循环体可能瞬间膨胀几倍甚至几十倍。内联省掉的是调用开销，付出的是代码体积。经验规则：对**每个调用点都极热但函数体很大**的被调用方，要警惕 LTO 把同一个大函数体复制进多个热路径，导致 L1I 被稀释。可以用 `-finline-limit`、函数级 `__attribute__((noinline))` 或者 BOLT / Propeller 这类链接后布局优化来控制。

**6. 用 `perf` 直接看 uop cache 失效（Intel）。** `dsb2mite_switches.penalty_cycles`（EventCode `0xab`，UMask `0x2`）统计的是"从 DSB 切回 MITE 时的取指惩罚周期数"，是判断 uop cache 是否被撑爆最直接的指标；配套还有 `frontend_retired.any_dsb_miss` 与 `frontend_retired.dsb_miss`（后者只统计"critical" miss，即真正暴露给后端的停顿），以及 `idq.dsb_uops` / `idq.mite_uops` / `lsd.uops` 用于算覆盖率（事件定义见 [Linux perf vendor events 补丁](https://lkml.iu.edu/hypermail/linux/kernel/2201.3/10768.html)）。注意这些事件是 Intel 专有的，AMD 与 Apple 需要各自的 PMC。

```text
taskset -c 2 perf stat -e \
  idq.dsb_uops,idq.mite_uops,lsd.uops,\
  dsb2mite_switches.penalty_cycles,\
  frontend_retired.dsb_miss,frontend_retired.l1i_miss,\
  idq_uops_not_delivered.core,\
  instructions,cycles ./bench_k16

# 两个派生指标
DSB 覆盖率   = idq.dsb_uops / (idq.dsb_uops + idq.mite_uops + lsd.uops)
前端停顿占比 = idq_uops_not_delivered.core / (cycles x 发射宽度)
```

判读顺序：先看 `lsd.uops` 占比，掉到 0 说明循环已超出 loop buffer；再看 DSB 覆盖率，低于 90% 要警惕；最后看 `dsb2mite_switches.penalty_cycles / cycles`，这个比值超过 5%（Intel topdown 里 `tma_dsb_switches` 的阈值）就说明 uop cache 切换已经构成可观的停顿。

**7. 同样的病在 GPU 上也会犯。** NVIDIA 官方记录过一个 Smith-Waterman 负载：循环展开把热指令 footprint 撑到 39360 条指令，warp 的 "No Instruction" 停顿随规模急剧上升；把外层展开去掉、只在二级循环上保留 2 倍展开后，footprint 降到 16912 条指令，指令缓存 miss 几乎消失，全规模平均性能反而提升（[NVIDIA Developer Blog](https://developer.nvidia.com/blog/improving-gpu-performance-by-reducing-instruction-cache-misses-2/)）。指令供给的容量约束是跨架构的。

---

## 参考来源

- [WikiChip: Intel Sandy Bridge (client) 微架构](https://en.wikichip.org/w/index.php?oldid=98305&title=intel%2Fmicroarchitectures%2Fsandy_bridge_%28client%29) —— DSB 32 组 × 8 路 × 6 μop = 1536 μop、32B 窗口最多 18 μop、平均命中率 80%、LSD 28 μop 上限、trace cache 存储效率对比（2011 年架构，页面持续更新）
- [Chips and Cheese: Sandy Bridge — Setting Intel's Modern Foundation](https://chipsandcheese.com/p/sandy-bridge-setting-intels-modern-foundation) —— DSB 虚拟地址寻址、L1I/iTLB 包含关系、只在分支后切 DSB、16B/周期传统取指窗口（2023）
- [Chips and Cheese: AMD's Ryzen 9950X — Zen 5 on Desktop](https://chipsandcheese.com/p/amds-ryzen-9950x-zen-5-on-desktop) —— op cache 6.75K→6K、8-way→16-way、Zen 4 144 项 loop buffer 被移除（2024）
- [Chips and Cheese: Discussing AMD's Zen 5 at Hot Chips 2024](https://old.chipsandcheese.com/2024/09/15/discussing-amds-zen-5-at-hot-chips-2024/) —— AMD 工程师对 loop buffer 未被"加回"的说明、单线程只用一簇译码器（2024）
- [Chips and Cheese: Intel's Lion Cove P-Core and Gaming Workloads](https://old.chipsandcheese.com/?p=36842) —— Lion Cove 64KB L1I、DSB 是主要 μop 来源、8 宽译码在 SPEC 上收益有限（2024/2025）
- [Chips and Cheese: Arm's Cortex-X925 — Reaching Desktop Performance](https://chipsandcheese.com/p/arms-cortex-x925-reaching-desktop) —— X925 删除 MOP cache、L1I 64KB 且按 76 位粒度预译码、10 路译码实测 10 指令/周期（2026）
- [Chips and Cheese: Intel's Netburst — Failure is a Foundation for Success](https://chipsandcheese.com/p/intels-netburst-failure-is-a-foundation-for-success) —— Pentium 4 12K μop trace cache、取代 L1I、miss 后走 L2（约 11 周期）
- [Deng, Huang, Szefer: Leaky Frontends (arXiv:2105.12224)](https://arxiv.org/abs/2105.12224) —— DSB 32 组 × 8 路 × 6 μop、LSD 64 μop 上限、L1I 8-way/64 组/64B、40/400/4000 μop 循环分别走 LSD/DSB/DSB+MITE 的 perf 实测（2021，学术论文）
- [Intel 64 and IA-32 Architectures Optimization Reference Manual](https://www.cs.princeton.edu/courses/archive/spring13/cos217/reading/ia32opt.pdf) —— Sandy Bridge ICache 32KB 8-way、micro-fusion 与 macro-fusion 规则、cache line 63/0 字节边界不融合、"展开通常仍优于塞进 loop cache"原文
- [Intel Lion Cove 官方演讲要点整理（RealWorldTech 论坛）](https://www.realworldtech.com/forum?curpostid=218335&threadid=218335) —— 128 B/周期从 L1I 取指、8 指令/周期译码、5250 项 uop cache 输出 12 μop/周期、192 项 uop queue（2024）
- [TechPowerUp: Intel Core Ultra 9 285K（Arrow Lake）架构页](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html) —— Redwood Cove 4096 项 8-way → Lion Cove 5250 项 12-way op cache（2024）
- [AMD "Zen 5" 架构官方幻灯片（Hot Chips 2024，igor'sLAB 托管 PDF）](https://www.igorslab.de/wp-content/uploads/2024/08/Zen-5-Architecture.pdf) —— I-Cache 32KB 8-way、2×32B 取指、Op-Cache 6K 指令、16-way、2×6 宽 = 12 指令/周期（2024，厂商一手材料）
- [Apple M4 微架构评测（jia.je / 杰哥的运维编程小笔记）](https://jia.je/hardware/2025/05/21/apple-m4/) —— sysctl 官方 192KB/6-way/64B L1I、取指 16 指令/周期、M1–M4 持续 uops/cycle = 8/8/9/10、footprint 越过 192KB 后 IPC 从 10 掉到 2.5（2025，第三方实测 + Apple 官方优化指南引用）
- [Arm Neoverse V3 Core Technical Reference Manual](https://developer.arm.com/documentation/107734/0001/Technical-overview/Core-components?lang=en) —— L1 指令缓存 64KB、4-way、64 字节行（厂商官方文档）
- [Shubhankar Gambhir: The Alignment Cliff — Code Alignment and the Skylake Micro-Op Cache](https://shubhankar-gambhir.github.io/posts/the-alignment-cliff-code-alignment-and-the-skylake-micro-op-cache) —— 偏移 24 vs 28 字节：2.392 ns vs 2.870 ns、IPC 3.19 vs 2.66、四项 PMU 数据排除 DSB miss 与 L1I miss（第三方实测）
- [easyperf: MicroFusion in Intel CPUs](https://easyperf.net/blog/2018/02/15/MicroFusion-in-Intel-CPUs) —— un-lamination 机制、SnB/IvB 融合域 2 输入上限、Haswell 后 3 输入（2018，带实测计数器）
- [LLVM issue #42332: Excessive loop unrolling](https://gitmemories.com/llvm/llvm-project/issues/42332) —— Agner Fog 关于"展开后的大循环装不进 micro-op cache/loopback buffer 因而更慢"的论述，以及 LLVM `LoopMicroOpBufferSize` 阈值机制（2019）
- [Linux perf vendor events: Icelake 更新补丁（LKML 存档）](https://lkml.iu.edu/hypermail/linux/kernel/2201.3/10768.html) —— `DSB2MITE_SWITCHES.PENALTY_CYCLES`、`FRONTEND_RETIRED.DSB_MISS` / `ANY_DSB_MISS`、`IDQ.MITE_CYCLES_OK` 等事件的官方定义（2022）
- [David Huang: Zen 5 补充测试（移动端）](https://blog.hjc.im/?p=574/) —— Zen 5 op cache 组织（64 set × 16 way、每 entry 最多 6 mop）、关闭 op cache 性能下降约 23%（2024，第三方实测）
- [NVIDIA Developer Blog: Improving GPU Performance by Reducing Instruction Cache Misses](https://developer.nvidia.com/blog/improving-gpu-performance-by-reducing-instruction-cache-misses-2/) —— 展开导致热指令 footprint 从 16912 涨到 39360 条指令、指令缓存 miss 造成性能随规模下降（厂商官方博客）

---

## 系列导航：处理器微架构深度指南

本文是《处理器微架构深度指南》系列之一。

**同系列其他文章**：

- [分支预测完全指南：从一次数据重排优化说起](branch-prediction-guide.html)
- [编译器如何自动优化分支预测](compiler-branch-optimization.html)
- [缓存层次完全指南：从一次矩阵乘法分块说起](cache-hierarchy-complete-guide.html)
- [ILP 与流水线完全指南：从八累加器点积说起](ilp-and-pipeline-complete-guide.html)
- [内存层次与延迟隐藏：从指针追逐说起](memory-hierarchy-latency-hiding.html)
- [ROB 与乱序执行完全指南：一次瓶颈会诊的完整拆解](rob-out-of-order-complete-guide.html)
- [SIMD 与向量化完全指南：从 SAXPY 到 AVX10 与矩阵扩展](simd-vectorization-complete-guide.html)
- [TLB 与地址转换：从一次聚集重排说起](tlb-address-translation-guide.html)

所属模块：[前端与指令缓存](../modules/frontend.html)
