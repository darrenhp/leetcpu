---
title: 内存层次与延迟隐藏：从指针追逐说起
module: memory
date: 2026-09-14
order: 12
summary: 为什么单链表遍历一定吃不满内存带宽？本文用一个可推导的指针追逐实验给出答案：Little's Law 算出需要的并发访存数，MSHR 决定硬件能给多少，内存调度与预取只能在这个上限内做文章。
tags: [内存层次, MLP, MSHR, 内存调度, 硬件预取]
---
# 内存层次与延迟隐藏：从指针追逐说起

> 一个单链表遍历能把"内存延迟"直接读出来；把它拆成 N 条链并行推进，就能把"内存带宽"读出来。这两件事之间的差距，就是本文要讲的延迟隐藏。

---

## 一、从一次指针追逐到 Little's Law

### 最小实验：串行依赖链

配套题目见 [指针追逐 — 恢复内存级并行](../problems/interleaved-pointer-chasing.html)。把问题简化到最纯粹的形式：一个大链表，节点随机散布在远大于缓存的内存里，每个节点只放一个指针。

```c
typedef struct node { struct node *next; } node;

long chase1(node *p) {          // 串行依赖：下一步地址来自上一步结果
    long n = 0;
    while (p) { p = p->next; n++; }
    return n;
}
```

这个循环里**没有任何可重叠的东西**：第 i+1 次 load 的地址是第 i 次 load 的结果。CPU 再宽、乱序窗口再大也没用，因为指令之间的依赖关系是真的。于是吞吐被锁死：

> 吞吐 = 1 / 访存延迟

取桌面平台常见的 80 ns（DDR4 本身约 50 ns，加上互连、一致性与排队延迟后典型总延迟在 80–200 ns 区间，见 [Cimple, PACT 2018](https://commit.csail.mit.edu/papers/2018/kiriansky-pact18-cimple.pdf)），得到 1 / 80ns ≈ **1250 万次/秒**。每次步进至少读一个 64 B 的 cache line，折算带宽 = 12.5M × 64 B ≈ **0.8 GB/s**。而一套双通道 DDR5-6400 的理论带宽约 **102 GB/s**。也就是说这个循环只用了不到 1% 的内存带宽，却已经把延迟全部暴露出来了。

这就是"延迟受限"最干净的定义：**带宽用不满，但时间全花在等**。

### N 路交错：把延迟换成带宽

把一条链拆成 N 条互不依赖的链，同一轮里同时推进 N 个指针：

```c
long chaseN(node *h[], int N) {
    long n = 0;
    while (h[0]) {
        for (int i = 0; i < N; i++) h[i] = h[i]->next;   // 迭代间无依赖
        n += N;
    }
    return n;
}
```

现在 N 个 load 之间没有依赖，可以同时在飞。硬件能同时推进几条链，性能就是几倍——直到撞上下一个天花板。

### 用 Little's Law 反推需要多高的并发度

排队论里的 Little's Law 在这里是：

> 在途请求数 = 吞吐 × 延迟

要吃满带宽 B、延迟 L，就必须同时在途 B × L 字节的访存请求。换算成 64 B 的 cache line：

| 目标带宽 | 访存延迟 | 需要在途字节 | 折合 64 B cache line 数 |
|---|---|---|---|
| 30 GB/s | 60 ns | 1.8 KB | 约 28 条 |
| 60 GB/s | 80 ns | 4.8 KB | 约 75 条 |
| 100 GB/s | 80 ns | 8.0 KB | 约 125 条 |
| 100 GB/s | 100 ns | 10.0 KB | 约 156 条 |

注意这个结论的锋利之处：桌面平台的"满带宽"要求**上百个 cache line 同时在飞**。而下一节会看到，单核硬件能同时跟踪的 miss 数量，比这个数字小一到两个数量级。

顺带说明：本站 [跨步求和 — 预取隐藏延迟](../problems/strided-sum-prefetch.html) 与 [不规则聚集 — 随机访问预取](../problems/irregular-gather-prefetch.html) 两题，本质是在同一约束下的不同取舍——前者靠规则步长让硬件预取器替你造并发度，后者只能靠软件预取或 gather 硬凑。

---

## 二、硬件全景：谁在决定"同时能有几个 miss 在飞"

### MSHR / Line Fill Buffer：真正的 MLP 上限

缓存未命中后，CPU 需要一个地方记录"这个 line 我正在取，回来后填到哪里、给谁"。这个结构叫 **MSHR（Miss Status Holding Register）**，Intel 语境下 L1 侧的对应物常称为 **Line Fill Buffer（LFB）**。它位于加载队列与下一级缓存之间，是**并发 miss 数量的硬上限**。

难能可贵的是，这个数字在学术文献里有第三方实测值（厂商通常不公开）：

| 处理器 | L1 MSHR / fill buffer | L2 MSHR / miss queue | 来源性质 |
|---|---|---|---|
| Intel Pentium 4 | 8 | — | 论文引用，非官方 |
| ARM Cortex-A72 | 6 | — | 论文引用，非官方 |
| Intel Haswell | 10 | 16 | 论文引用，非官方 |
| Intel Lion Cove | 24 | 80 | Intel 官方演讲整理 |

前三项来自 Kiriansky 的 [Cimple 论文（PACT 2018）](https://commit.csail.mit.edu/papers/2018/kiriansky-pact18-cimple.pdf) 与同一作者的 [MIT 博士论文（2019）](https://commit.csail.mit.edu/papers/2019/vkiriansky19phd.pdf)，论文同时指出"现代处理器通常只有 6–10 个 L1 MSHR"，原因是 MSHR 需要做内容关联搜索，面积与功耗都随条目数快速上升，很难扩展。Lion Cove 的 24 + 80 来自 [Intel 官方 Lion Cove 演讲的整理稿](https://www.realworldtech.com/forum?curpostid=218335&threadid=218335)（该演讲同时给出 48 KB L0D 4 周期、192 KB L1D 9 周期、L2 17 周期 load-to-use）。

把 Little's Law 反过来用：Haswell 的 16 个 L2 MSHR 在 80 ns 延迟下能提供 16 × 64 B / 80 ns ≈ **12.8 GB/s** 的上限。Cimple 中给出的另一组实测参照是：当代 Intel Xeon 服务器上**单个核能分到的带宽只有 4–6 GB/s**。作为横向参照，Chips and Cheese 实测 Zen 2 的单核只能压出 **24–25 GB/s** 的 DRAM 带宽（[Pushing AMD's Infinity Fabric to its Limits](https://chipsandcheese.com/p/pushing-amds-infinity-fabric-to-its)）。

**结论：单线程几乎不可能吃满内存带宽。** 这个结论的适用条件是"随机访问、无硬件预取可依赖、单线程"；顺序流的场景硬件预取器会用同一批 MSHR 提前取数，单核带宽可以高得多（例如 Zen 5 单核 L3 带宽可达 170 GB/s 量级），但那已经不是"随机访问"了。

### 第二道天花板：指令窗口

即使 MSHR 够用，ROB 也会先撑不住。Cimple 给出的 Haswell 资源是 192 项 ROB、168 个物理寄存器、72 项 load buffer、42 项 store buffer。论文算了一笔账：把资源摊到 10 个并行访存请求上，**每个常规 load 平均只能配到 19 个 µop、16 个寄存器、7 个 load 和 4 个 store**——指针追逐之外的计算一旦变多，窗口就先满了。

### DRAM 内部：为什么"并行"必须靠结构提供

一次 DRAM 访问要拆成 bank / row / column 三级。每个 bank 有一个行缓冲（row buffer），三种情形代价完全不同：

| 情形 | 需要的命令 | 额外延迟 |
|---|---|---|
| 行命中：目标行已在行缓冲 | 直接列访问 | 仅列访问延迟 |
| 行关闭：bank 空闲 | 激活 + 列访问 | 加一次激活 |
| 行冲突：bank 里开着别的行 | 预充电 + 激活 + 列访问 | 加预充电与激活 |

[Rixner 等人的 ISCA 2000 论文](http://cva.stanford.edu/classes/ee482a/papers/mas.pdf) 指出：**同一行内连续列访问与同一 bank 内跨行访问之间，带宽相差接近一个数量级**。论文里的示例很直观：假设预充电占 3 个 DRAM 周期、激活 3 个、列访问 1 个，8 个访存请求按原顺序执行需要 56 个 DRAM 周期，重排后只要 19 个。

并行度因此必须靠结构堆出来：多通道 × 多 rank × 多 bank，每个 bank 各自持有一个行缓冲，控制器才能把请求铺开。

### 内存控制器调度：FR-FCFS 与它的代价

既然访问顺序极大影响吞吐，控制器自然会重排请求。事实标准是 **FR-FCFS（First-Ready, First-Come-First-Served）**：优先服务行命中的请求，其次按年龄。Rixner ISCA 2000 报告的收益是：保守重排（只服务第一个就绪请求）在五个媒体 benchmark 的 trace 上提升带宽 **40%**，激进重排提升 **93%**，在合成基准上最高 **144%**，媒体处理应用的实际提升约 **30%**。

代价是不公平。FR-FCFS 会系统性偏袒行局部性好的线程。[Moscibroda 与 Mutlu 在 USENIX Security 2007 的论文](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/DRAM-Unfairness.pdf) 证明这是一种可被利用的 DoS：在一台双核 Intel Pentium D（Windows XP）上，一个"内存性能霸占者"能把同跑的 **gcc 拖慢 2.82 倍，而它自己只慢 1.18 倍**（论文摘要把这个数字概括为 2.9 倍）；在模拟的 16 核系统上，受害线程的减速可达 **14.6 倍**。这组数字在 Mutlu 的 [ACACES 2013 讲义](https://people.inf.ethz.ch/omutlu/pub/onur-ACACES2013-Topic3-memory-qos-short.pdf) 中被反复引用。

换句话说：**FR-FCFS 优化的是"系统的总吞吐"，牺牲的是"单个线程的可预测性"**。多核共享内存控制器之后，后者变成了必须补的课。

### 硬件预取器：既是帮手，也是 MSHR 的竞争者

现代核心同时部署多个预取器。Intel 在官方调优文档里把它们列得最清楚（[Hardware Prefetch Controls for Intel E-Cores](https://intel.github.io/optimization-zone/hardware/HWPrefetchTuning)）。该文档给出 MSR 0x1A4 的逐位定义（下表为 E-core；文档明确注明 P-core 也用 0x1A4，但位定义略有差异）：

| MSR 0x1A4 位 | 预取器 | 置 1 的效果 |
|---|---|---|
| 0 | MLC / LLC Streamer | 关闭 L2 流预取 |
| 1 | Adjacent Cache Line | 关闭相邻行预取 |
| 2 | DCU Streamer（L1 NLP） | 关闭 L1 数据流预取 |
| 3 | DCU IP Prefetcher（L1 IPP） | 关闭按 load 指令 IP 学习的预取 |
| 4 | DCU Next Page Prefetcher | 关闭跨页预取 |
| 5 | AMP | 关闭 AMP |
| 6 | LLC Page Prefetch | 关闭 LLC 页预取 |
| 8 | Stream prefetch code fetch | 关闭取指流预取 |

比"开关"更少见的是**调参**。同一文档给出了 MSR 0x1320 的激进度旋钮，这几个字段解释了为什么"预取越激进越好"是错的：

| MSR 0x1320 位 | 含义 | 调大的效果 |
|---|---|---|
| 4:0 | L2 Stream/AMP 的 XQ 阈值：需要多少空 XQ 条目才发射预取 | 预取更少 |
| 19:17 | L2 预取触发窗口 | 预取更激进 |
| 24:20 | L2 Stream 领先核心执行的最大距离 | 预取更远 |
| 42:37 | LLC Stream 领先核心执行的最大距离 | 预取更远 |
| 62:58 | LLC Stream 的 XQ 阈值 | 预取更少 |

文档同时记录了三套自动节流机制：DPT（按 uncore 队列占用率动态降压）、DBP（Dead Block Predictor，用"预取来的 line 有没有被用上"反推是否该继续预取）、DPC（动态调节预取激进度）。

两个实操要点：

其一，**预取不是越激进越好**，文档原文：*"A high utilization of the DDR interface will increase access latency."*——DDR 接口利用率升高会推高访问延迟。这正是"带宽与延迟是两个变量"的硬件注脚：预取越多，队列越满，排队延迟越长。

其二，**怎么量化预取效果**。文档给出的方法是用 IMC 的 DDR 读/写计数器：*"These counters measure requests per cache line, so the value should be multiplied by 64 to get the number of bytes transferred."*，取两个时间点的差值即为预取带来的额外 DDR 读流量。注意 MSR 是 per-core（同一 module 内共享）的，改设置要对所有核执行或绑核测试，否则不可复现。

关键点是**预取请求也要占 MSHR/fill buffer**。Cimple 给出了一个很实用的判据：如果性能计数器显示相关 load 既 miss L1 又没有命中 MSHR，说明预取**太早**（数据已被换出）；如果 load 命中 MSHR 而不是 L1，说明预取**太晚**（请求还在途中）。

---

## 三、演进史：每一步解决了什么，引入了什么代价

| 阶段 | 解决什么 | 引入什么代价 |
|---|---|---|
| CPU 直接等 DRAM | — | 访存成为绝对瓶颈 |
| 多级缓存 | 用局部性把"平均延迟"降下来 | 面积、功耗、一致性复杂度；对无局部性的负载（指针追逐）完全无效 |
| 大乱序窗口 / Runahead | 用 ILP 填充等待期 | ROB、寄存器文件、功耗爆炸式增长；窗口受分支预测准确率牵制 |
| MLP 被明确提上议程 | 认识到"延迟不重要，能不能重叠才重要" | 需要软件配合（交错、预取、gather），编译器与程序员负担增加 |
| FR-FCFS 内存调度 | 用重排把 DRAM 行局部性吃干榨净 | 线程间不公平、可被恶意负载 DoS |
| QoS 感知调度 | 补上公平性与 SLA | 需要估计每个线程的"减速"，控制器复杂度显著上升 |
| 多级、可节流的预取器 | 覆盖更广的访问模式，避免预取泛滥 | 与需求请求抢 MSHR、抢带宽、污染缓存，因此必须再加一层节流 |

关于 MLP 这一行：Andrew Glew 在 1998 年 ASPLOS 的 Wild and Crazy Ideas 环节提出 ["MLP yes! ILP no!"](https://people.eecs.berkeley.edu/~kubitron/asplos98/abstracts/andrew_glew.pdf)，主张真正该关心的指标是"单线程能同时产生多少个未完成的 cache miss"，而不是 IPC。二十年后 Cimple 用同一套语言重新量化了这件事，结论是：**当代软件的 MLP 太低，以至于 MSHR 根本不是瓶颈——瓶颈是软件自己一次只发一个请求**。

### 被淘汰或降级的思路

**"软件 prefetch 指令万能论"。** 在预取器还不成熟的年代，手工插 `prefetch` 是硬功夫；今天在顺序流上它已经完全没必要，硬件做得比人好。它现在只在两类场景有价值：步长不规则但可提前算出的 gather/图遍历，以及跨页边界导致硬件预取器停摆的地方。代价是预取距离必须针对不同微架构重调，而且每条软件预取都要占 L1 MSHR。

**"带宽够了延迟就不是问题"。** 这是 Little's Law 两个变量混淆导致的典型误判。Arrow Lake 是最新的反例：Chips and Cheese 实测它的读取带宽**接近 DDR5 理论值**，但 DRAM 延迟反而退步到 106 ns 量级，结果是 505.mcf、520.omnetpp 这类访存密集项上被上一代 Raptor Lake 反超（[Analyzing Lion Cove's Memory Subsystem in Arrow Lake](https://chipsandcheese.com/p/analyzing-lion-coves-memory-subsystem)）。带宽与延迟是两个独立的优化目标。

**"把末级缓存堆大就行"。** 容量上去，延迟也跟着上去：Arrow Lake 把 L3 从 Lunar Lake 的 12 MB 加到 36 MB，P-core 的 L3 load-to-use 延迟也从约 52 周期涨到 80 周期以上（[Examining Intel's Arrow Lake, at the System Level](https://old.chipsandcheese.com/?p=33481)）。

### 关于"预取器也要学"

学术界确实在往这个方向走，代表作是 [Pythia](https://arxiv.org/abs/2109.12021)（arXiv 2021，MICRO 2021 的扩展版）：把预取建模成强化学习问题，用程序上下文（PC、地址 delta 历史）加系统反馈（当前带宽占用）作为状态，用 Q 值表选预取偏移。论文报告在多种核数与带宽配置下优于 MLOP/Bingo/SPP，面积开销约 1.03%（桌面级核心）、每核约 25.5 KB 存储。

**必须区分清楚**：这是论文里的仿真与硬件综合结果。没有找到任何厂商公开确认在量产 CPU 中部署了基于机器学习的硬件数据预取器。目前量产实现仍然是"多个启发式预取器 + 动态节流"的组合，Intel 官方文档里能看到的也是基于计数器和占用率的反馈，而不是学习算法。

---

## 四、真实处理器对比（2024–2025 世代）

| 维度 | Intel Lion Cove（Core Ultra 9 285K） | AMD Zen 5（Ryzen 9 9900X / 9950X） | ARM Neoverse V2（AWS Graviton 4） | Apple M4 / M4 Pro / M4 Max |
|---|---|---|---|---|
| DRAM 延迟 | **106.3 ns**（DDR5-6400，2024 实测） | **82.43 ns**（9900X，同篇对照）；9950X + DDR5-6000 实测"略高于 70 ns" | **114.08 ns** 本地内存；跨插槽再贵 **142.5 ns** | 未找到官方数据；第三方 tinymembench 在 M4 Mac mini、64 MiB 工作集下测得约 **71 ns**（非官方，且该数值是叠加在 L1 延迟之上的额外延迟） |
| L3 / SLC 延迟 | 84 周期 ≈ **14.83 ns**，36 MB | 48 周期 ≈ **8.44 ns**，每 CCX 32 MB | 68 周期 ≈ **25 ns**（16 MB 测试点） | 未找到公开数据 |
| L2 延迟 | 17 周期，3 MB | 14 周期，1 MB | 11 周期，2 MB | 未找到公开数据 |
| MSHR / fill buffer | **24 个 L1 fill buffer + 80 个 L2 miss queue**（Intel 官方演讲整理）；Haswell 世代为 10 / 16（第三方论文，非官方） | 未找到公开数据 | L3 事务队列 96 或 92 项（Hot Chips 2023 / TRM），L1i fill buffer 16 项；**L1D fill buffer 未公开** | 未找到公开数据 |
| 内存配置与带宽 | 双通道 DDR5-6400，理论约 102 GB/s，实测读取接近理论值 | 双通道 DDR5；单 CCD 的 IFOP 在 2 GHz FCLK 下读 64 GB/s、写 32 GB/s，跨 die 链路可能先于 DRAM 成为瓶颈 | 12 通道 DDR5-5200 | LPDDR5X-7500，官方标称 **120 / 273 / 410 / 546 GB/s** |

数据来源：Intel 与 Zen 5 的对比数字取自 [Chips and Cheese 的 Arrow Lake 系统级评测（2024）](https://old.chipsandcheese.com/?p=33481)；Zen 5 桌面数据的原始出处为 [AMD's Ryzen 9950X: Zen 5 on Desktop（2024）](https://chipsandcheese.com/p/amds-ryzen-9950x-zen-5-on-desktop)；Neoverse V2 取自 [Chips and Cheese 的 Graviton 4 评测（2024）](https://old.chipsandcheese.com/2024/07/22/arms-neoverse-v2-in-awss-graviton-4/)；Apple 带宽为 [Apple 官方技术规格](https://support.apple.com/zh-cn/121553)，M4 延迟为 [第三方 tinymembench 实测](https://github.com/geerlingguy/sbc-reviews/issues/57)。

### 怎么读这张表

**Intel**：把筹码压在"更深的窗口 + 更大的核心私有缓存 + 更多 fill buffer"上。Lion Cove 把 L1 fill buffer 提到 24、L2 miss queue 提到 80，同时插入一级 192 KB 的中间缓存，目的正是提高单核消费外部带宽的能力。代价是 chiplet 化之后 DRAM 延迟退到 100 ns 以上（Tom's Hardware 用 AIDA 在 DDR5-5600 下测得 91.9–94.1 ns，对比 14900K 的 79.1 ns，Intel 自己给出的解释是 15–20 ns 的额外代价）。

**AMD**：Zen 5 的打法是低延迟优先——L3 只要 48 周期（8.44 ns），DRAM 比 Intel 低 20 ns 以上。代价是 chiplet 拓扑带来不对称：单个 CCD 对外的 Infinity Fabric 链路在 2 GHz FCLK 下只读 64 GB/s / 写 32 GB/s，带宽饥渴的负载如果只落在一个 CCD 上，会先被跨 die 链路卡住，而不是被 DRAM 卡住。

**ARM**：Neoverse V2 走的是"大 L2 挡住慢 L3"的路子——2 MB L2 只要 11 周期，但共享 L3 命中要 25 ns，单核 L3 带宽只有 30 GB/s 出头。论文与 TRM 给出的 L3 事务队列 96/92 项是一个罕见的公开数字。

**Apple 与统一内存（UMA）**：Apple 的取舍和另外三家不在一个坐标系里。CPU、GPU、NPU、媒体引擎共享同一个物理内存池与同一条带宽通道，好处是**零拷贝**（GPU 直接用 CPU 的指针，不存在 PCIe 搬运），代价是这份带宽被多方争抢，而且容量与带宽必须在封装阶段定死、不可升级。官方标称带宽从 M4 的 120 GB/s 一路到 M4 Max 的 546 GB/s，确实远高于桌面双通道 DDR5 的约 100 GB/s。

关于"Apple 的内存延迟更高"这一常见说法，需要谨慎：能查到的第三方实测（tinymembench，64 MiB 工作集，约 71 ns）与 Zen 5（82 ns）、Arrow Lake（106 ns）相比**并不更高**，但三者的测量口径、工作集大小与页面配置都不同，不宜直接跨来源比较。可以确认的只有：Apple 不公开任何延迟与 fill buffer 数字，相关讨论都应标注为非官方。

### 服务器一侧的例外

桌面平台纠结的是"单核如何在 100 GB/s 里多抢一点"，服务器侧则直接把通道数堆上去：AMD EPYC 9004 系列单路提供 **12 条 DDR5-4800 通道**，官方标称单路理论带宽 460.8 GB/s（[AMD EPYC 9004 官方页](https://www.amd.com/ja/products/processors/server/epyc/9004-series.html)）。Intel 的对应答案是 [Xeon CPU Max 系列](https://www.intel.ca/content/dam/www/central-libraries/us/en/documents/2023-01/xeon-cpu-max-series-product-brief.pdf)：封装内堆 64 GB HBM2e（四组堆栈，峰值 3200 MT/s），并提供 HBM-Only / HBM Flat / HBM Cache 三种内存模式——本质上是用容量换带宽，或者把 HBM 当作 DDR 前面的一级缓存。

---

## 五、延伸知识点

**1. 单线程吃不满内存带宽，这是可以算出来的，不是经验之谈。** 用 Little's Law：需要的并发 cache line 数 = 目标带宽 × 延迟 / 64 B。吃满 100 GB/s @ 80 ns 需要约 125 条 line 在飞，而 L1 MSHR 的公开数字只有个位数到 24 这个量级。想逼近峰值带宽，只有三条路：多线程（每个线程各自带一批 MSHR）、硬件预取（让 MSHR 提前被填满）、向量化 gather（一条指令发多个地址）。适用条件是"访问模式本身没有串行依赖"；如果像单链表那样有真依赖，前两条路都失效。

**2. Little's Law 是性能分析里最便宜的一把尺子。** 任何"排队"系统都能用：CPU 流水线（在途指令 = IPC × 延迟）、磁盘 IO（队列深度 = IOPS × 响应时间）、网络（BDP = 带宽 × RTT）。看到一个系统"资源利用率很低但延迟很高"，第一反应应该是算一下在途量够不够，而不是去升级硬件。

**3. 软件预取的距离要按时间算，不按迭代数算。** 提前的迭代数 d ≈ 访存延迟 / 单次迭代耗时。举例：延迟 80 ns、核心 5.7 GHz、每次迭代约 4 ns（约 23 周期），那么 d ≈ 20 次迭代。注意分母是**迭代的吞吐**而不是延迟——循环体越重，d 越小，这也是为什么重循环里手工预取经常失效。

**4. 预取过度有两种代价，第二种更隐蔽。** 明面上的代价是浪费带宽、污染缓存；隐蔽的代价是**耗尽 MSHR**：预取把 fill buffer 占满后，真正的需求 load 反而排不进去，延迟不降反升。判据见第二节——需求 load 既 miss L1 又没命中 MSHR，就是预取太早。

**5. `perf` 里 memory-bound 高 ≠ 带宽瓶颈。** 这两个词在自上而下分析里是同一个槽位，但成因完全不同。区分方法是同时看带宽利用率：如果 memory-bound 很高而实测 DRAM 带宽离峰值还很远，那就是延迟/MLP 问题（在途量不够），加带宽毫无用处。算实际带宽最直接的口径是读 IMC 的 DDR 计数器再取差值——**计数器按 cache line 计，要乘 64 才得到字节数**（Intel 官方调优文档）。Chips and Cheese 在 Lion Cove 的游戏负载分析里正是这么做的——通过 ARB 队列的占用率折算延迟，发现延迟并未随请求堆积而恶化，从而判定 DRAM 带宽不是瓶颈。

**6. NUMA 场景下，"延迟"是一个范围而不是一个数字。** Graviton 4 的实测很典型：本地内存 114 ns，跨插槽访问再加 142.5 ns（超过 250 ns），而同插槽内跨核的 cache line  bounce 只要 30–60 ns，跨插槽则是 138.6 ns。多插槽服务器上做内存亲和性绑定，收益往往比任何微优化都大。

**7. 与相邻主题的分工。** 缓存的映射方式、替换策略（含 RRIP）与一致性协议见 [缓存层次完全指南](../reading/cache-hierarchy-complete-guide.html)；地址转换与 TLB miss 的代价见 [TLB 与地址转换](../reading/tlb-address-translation-guide.html)；乱序窗口、端口与物理寄存器如何限制"能往前看多远"，见 [ILP 与流水线完全指南](../reading/ilp-and-pipeline-complete-guide.html) 与 [分支预测完全指南](../reading/branch-prediction-guide.html)。本文只负责"从核心到 DRAM 这条路径上的延迟与并发"。

---

## 参考来源

- [Cimple: ILP & MLP（Kiriansky et al., PACT 2018）](https://commit.csail.mit.edu/papers/2018/kiriansky-pact18-cimple.pdf) —— Haswell 10 个 L1 MSHR / 16 个 L2 MSHR、Little's Law 形式、Xeon 单核 4–6 GB/s、Haswell ROB 与 load/store buffer 规模、预取过早/过晚的判据（2018）
- [Kiriansky 博士论文（MIT, 2019）](https://commit.csail.mit.edu/papers/2019/vkiriansky19phd.pdf) —— Pentium 4 8 个、Cortex-A72 6 个 L1 MSHR，MSHR 难以扩展的原因（2019）
- [Memory Access Scheduling（Rixner et al., ISCA 2000）](http://cva.stanford.edu/classes/ee482a/papers/mas.pdf) —— DRAM 三维结构、行命中与跨行访问带宽差近一个数量级、56→19 周期示例、保守重排 +40% / 激进 +93% / 最高 +144%（2000）
- [Memory Performance Attacks（Moscibroda & Mutlu, USENIX Security 2007）](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/DRAM-Unfairness.pdf) —— Pentium D 上 gcc 被拖慢 2.82×、霸占者自身只慢 1.18×、16 核模拟下 14.6×（2007）
- [Scalable Many-Core Memory Systems Topic 3（Mutlu, ACACES 2013）](https://people.inf.ethz.ch/omutlu/pub/onur-ACACES2013-Topic3-memory-qos-short.pdf) —— FR-FCFS 的不公平性、DoS 脆弱性、QoS 感知调度的动机（2013）
- [Intel's Lion Cove Microarchitecture（RealWorldTech 论坛整理，2024）](https://www.realworldtech.com/forum?curpostid=218335&threadid=218335) —— 24 个 L1 fill buffer、80 个 L2/MLC queue、各级缓存 load-to-use 周期（2024，源自 Intel 官方演讲整理，非数据手册）
- [Analyzing Lion Cove's Memory Subsystem in Arrow Lake（Chips and Cheese, 2024）](https://chipsandcheese.com/p/analyzing-lion-coves-memory-subsystem) —— Arrow Lake DRAM 延迟退步、mcf/omnetpp 被 Raptor Lake 反超（2024）
- [Examining Intel's Arrow Lake, at the System Level（Chips and Cheese, 2024）](https://old.chipsandcheese.com/?p=33481) —— Arrow Lake 106.3 ns DRAM 延迟、L3 84 周期、Lunar Lake L3 约 52 周期、读取带宽接近理论值（2024）
- [AMD's Ryzen 9950X: Zen 5 on Desktop（Chips and Cheese, 2024）](https://chipsandcheese.com/p/amds-ryzen-9950x-zen-5-on-desktop) —— Zen 5 与 DDR5-6000 下"略高于 70 ns"的内存延迟（2024）
- [Arm's Neoverse V2 in AWS's Graviton 4（Chips and Cheese, 2024）](https://old.chipsandcheese.com/2024/07/22/arms-neoverse-v2-in-awss-graviton-4/) —— 本地 DRAM 114.08 ns、跨插槽 +142.5 ns、L2 11 周期、L3 68 周期/25 ns、L3 事务队列 96/92 项（2024）
- [Pushing AMD's Infinity Fabric to its Limits（Chips and Cheese）](https://chipsandcheese.com/p/pushing-amds-infinity-fabric-to-its) —— Zen 2 单核只能压出 24–25 GB/s、单 CCD IFOP 读 64 GB/s / 写 32 GB/s
- [Hardware Prefetch Controls for Intel E-Cores（Intel 官方调优文档）](https://intel.github.io/optimization-zone/hardware/HWPrefetchTuning) —— MSR 0x1A4 逐位定义（E-core，并注明 P-core 位定义不同）、MSR 0x1320 的 XQ 阈值与预取距离调参位、DPT/DBP/DPC 三类节流机制、"DDR 接口高利用率会推高访问延迟"原文、用 IMC 计数器（×64 字节）量化预取流量的方法
- [Pythia: A Customizable Hardware Prefetching Framework Using Online Reinforcement Learning（arXiv 2021）](https://arxiv.org/abs/2109.12021) —— ML 预取器的学术代表工作，1.03% 面积开销、每核约 25.5 KB 存储（2021，仿真与综合结果，非量产实现）
- [MLP yes! ILP no!（Glew, ASPLOS WACI 1998）](https://people.eecs.berkeley.edu/~kubitron/asplos98/abstracts/andrew_glew.pdf) —— MLP 概念的原始提出（1998）
- [MacBook Pro 14 英寸（M4 Pro 或 M4 Max）技术规格（Apple 官方）](https://support.apple.com/zh-cn/121553) 与 [MacBook Pro 14 英寸（M4）技术规格](https://support.apple.com/zh-cn/121552) —— 统一内存带宽 120 / 273 / 410 / 546 GB/s（2024）
- [M4 Mac mini 第三方 tinymembench 实测（geerlingguy, 2024）](https://github.com/geerlingguy/sbc-reviews/issues/57) —— 64 MiB 工作集下单次随机读约 71 ns（非官方）
- [AMD EPYC 9004 系列官方产品页](https://www.amd.com/ja/products/processors/server/epyc/9004-series.html) —— 12 条 DDR5 内存通道（2022）
- [Intel Xeon CPU Max Series Product Brief（Intel 官方）](https://www.intel.ca/content/dam/www/central-libraries/us/en/documents/2023-01/xeon-cpu-max-series-product-brief.pdf) —— 封装内 64 GB HBM2e、峰值 3200 MT/s、HBM-Only / Flat / Cache 三种模式（2023）
- [Intel Core Ultra 9 285K 评测（Tom's Hardware, 2024）](https://www.tomshardware.com/pc-components/cpus/intel-core-ultra-9-285k-cpu-review) —— AIDA 内存延迟 91.9–94.1 ns vs 14900K 79.1 ns，Intel 称 chiplet 带来 15–20 ns 额外延迟（2024）

---

## 系列导航：处理器微架构深度指南

本文是《处理器微架构深度指南》系列之一。

**同系列其他文章**：

- [分支预测完全指南：从一次数据重排优化说起](branch-prediction-guide.html)
- [编译器如何自动优化分支预测](compiler-branch-optimization.html)
- [缓存层次完全指南：从一次矩阵乘法分块说起](cache-hierarchy-complete-guide.html)
- [ILP 与流水线完全指南：从八累加器点积说起](ilp-and-pipeline-complete-guide.html)
- [ROB 与乱序执行完全指南：一次瓶颈会诊的完整拆解](rob-out-of-order-complete-guide.html)
- [前端与指令缓存：为什么过度展开反而更慢](frontend-and-instruction-cache.html)
- [SIMD 与向量化完全指南：从 SAXPY 到 AVX10 与矩阵扩展](simd-vectorization-complete-guide.html)
- [TLB 与地址转换：从一次聚集重排说起](tlb-address-translation-guide.html)

所属模块：[内存层次与延迟隐藏](../modules/memory.html)
