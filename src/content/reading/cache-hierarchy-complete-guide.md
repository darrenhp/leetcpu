---
title: 缓存层次完全指南：从一次矩阵乘法分块说起
module: cache
date: 2026-09-14
order: 10
summary: 从 N=1024 双精度矩阵乘法的工作集推导出发，算出朴素 ijk 为何有 188 MPKI、分块后算术强度如何跨过机器平衡点，再展开组相联位划分、VIPT 别名、RRIP、MESI/MOESI 与四家当代处理器的实测参数。
tags: [缓存层次, 分块, VIPT, RRIP, 伪共享, 一致性]
---
# 缓存层次完全指南：从一次矩阵乘法分块说起

> 一个问题贯穿全文：为什么"把工作集塞进 L1"是错的？从 N=1024 双精度矩阵乘法的字节数开始算，一路算到组相联的位划分、替换策略的演进、以及 Intel/AMD/ARM/Apple 四家当代核心的真实参数。

---

## 一、现象与量化案例：N=1024 矩阵乘法的工作集

### 1.1 题目与参数

本站配套题目 [矩阵乘法 — 缓存分块](../problems/matrix-multiply-tiling.html) 用的是 `N=512` 的 `float`。为了把结论推到更极端的量级，本文把参数放大到 **`N=1024`、`double`（8 字节）**，行优先存储：

```c
#define N 1024
void matmul(const double *A, const double *B, double *C) {
    for (int i = 0; i < N; i++)
        for (int j = 0; j < N; j++) {
            double s = 0.0;
            for (int k = 0; k < N; k++)
                s += A[i*N + k] * B[k*N + j];   // ← 热点
            C[i*N + j] = s;
        }
}
```

先算三个不变量：

| 量 | 计算 | 结果 |
|---|---|---|
| 单个矩阵大小 | N² × 8B | 8 MiB |
| 三个矩阵总工作集 | 3 × N² × 8B | **24 MiB** |
| 内层 FMA 次数 | N³ | 1.074 × 10⁹ |
| 浮点运算总量 | 2N³ | 2.147 × 10⁹ FLOP |

对照典型当代核心的容量：L1D 32–48 KB（Apple M4 为 128 KB）、L2 1–3 MB、LLC 8–36 MB（AMD X3D 可达 96 MB）。**24 MiB 的工作集在任何一级私有缓存里都装不下**，这一点决定了后面所有的分析。

### 1.2 朴素 ijk 的访存模式

固定 `(i, j)` 时，内层 `k` 循环读两个数：

- `A[i*N + k]`：地址步长 8 B，连续。一条 64 B 缓存行装 8 个 `double`，**行利用率 100%**。
- `B[k*N + j]`：地址步长 **N × 8 = 8192 B = 8 KiB**。每次迭代都跳到一条新行，而这条行里只用 8/64 = **12.5%**。

8 KiB 这个步长不只是"跨行"，它还有一个更致命的性质——**它是 2 的幂**。设 L1D 为 32 KB、8 路、64 B 行，则行内偏移 6 bit、组索引 6 bit（64 组）。`B[k*N+j]` 的字节偏移是 `k × 8192 + j × 8`，而 `k × 8192` 的 bit 0–12 全为 0，于是**索引位完全由 `j` 决定：同一列的 1024 个元素全部映射到同一个组**。8 路 LRU 装不下 1024 个不同行，复用率为零。

这个结论对当代所有 L1D 都成立，不只是 32 KB 那一档：

| L1D 配置 | 组数 | 索引位 | 一列 1024 个元素落在几组 | 每组元素数 vs 路数 |
|---|---|---|---|---|
| 32 KB / 8 路 / 64B（Zen 4） | 64 | [6:11] | 1 组 | 1024 vs 8 → 必冲突 |
| 48 KB / 12 路 / 64B（Zen 5、Golden Cove） | 64 | [6:11] | 1 组 | 1024 vs 12 → 必冲突 |
| 64 KB / 4 路 / 64B（Neoverse V2/V3） | 256 | [6:13] | 2 组 | 512 vs 4 → 必冲突 |
| 128 KB / 8 路 / 64B（Apple M4） | 256 | [6:13] | 2 组 | 512 vs 8 → 必冲突 |

### 1.3 失效次数、MPKI 与命中率

按"无跨迭代复用"的最坏情况估算（1.2 节已证明这是成立的）：

| 来源 | 失效行数 | 说明 |
|---|---|---|
| B 的列访问 | N³ = 1.074 × 10⁹ | 每个 `B[k][j]` 一条新行 |
| A 的行访问 | 128 × N² = 1.342 × 10⁸ | 每个内积重取 A 的一行（8 KB = 128 行），被 B 流冲掉 |
| C 的写分配 | N²/8 = 1.31 × 10⁵ | 8 个连续 `j` 共享一行 |
| **合计** | **≈ 1.208 × 10⁹** | |

取内层迭代约 6 条指令，总指令数 ≈ 6.44 × 10⁹ = 6.44 × 10⁶ 千条指令：

- **L1D MPKI ≈ 1.208 × 10⁹ / 6.44 × 10⁶ ≈ 188**
- L2→L1 重填流量 = 1.208 × 10⁹ × 64 B ≈ **77.3 GB**
- 但落到 **DRAM** 的只有 3 × (8 MiB / 64 B) = 393,216 行 ≈ **24 MiB**（前提是 LLC 装得下这三块 8 MiB）
- **LLC MPKI ≈ 0.061，LLC 命中率 ≈ 99.97%**

> **这一组数字是本文最反直觉的地方**：朴素 ijk 的 LLC 命中率高达 99.97%，看上去"缓存工作得很好"，但它慢得离谱。因为瓶颈根本不在 DRAM，而在 **L2→L1 这条重填通道的带宽与 L2 命中延迟**。只看 LLC 命中率会得出完全错误的诊断。

### 1.4 分块：工作集与算术强度

按 `T × T × T` 分块后，任一时刻的活跃工作集是 3 个 `T²` 块：

| T | 工作集 3T²×8B | 落在哪一级 | L2→L1 流量 24N³/T | 算术强度 AI = T/12 FLOP/B |
|---|---|---|---|---|
| 32 | 24 KiB | 48 KB L1D 可装但余量只剩一半；32 KB L1D 只剩 8 KB 余量，几乎不可用 | 805 MB | 2.67 |
| 64 | 96 KiB | L1D 全部装不下 → **L2** | 403 MB | 5.33 |
| 128 | 384 KiB | L2（1–3 MB） | 201 MB | 10.67 |
| 256 | 1.5 MiB | 超出 1 MB L2 | 100 MB | 21.3 |
| 朴素 ijk | 24 MiB | 远超所有私有缓存 | 77.3 GB | 0.028 |

### 1.5 Roofline：为什么分块到 L2 就够了

给出一个显式建模假设（便于读者替换成自己机器的数字）：核心每周期 16 个 double FMA（两条 512-bit FMA 流水线 × 8 lane），主频 5 GHz，L1 重填接口 64 B/clk。

- 峰值算力 = 16 × 2 × 5 × 10⁹ = **160 GFLOP/s** → 纯计算时间 = 2.147 × 10⁹ / 1.6 × 10¹¹ = **13.4 ms**
- L1 重填带宽 = 64 B × 5 GHz = **320 GB/s**
- **机器平衡点 = 160 / 320 = 0.5 FLOP/B**

把 1.4 节的 AI 画上去：

| 版本 | AI（FLOP/B） | 与 0.5 比较 | 访存时间 | 结论 |
|---|---|---|---|---|
| 朴素 ijk | 0.028 | 远低于 | 77.3 GB / 320 GB/s = **242 ms** | 带宽受限，比计算慢 **18×** |
| T=32 | 2.67 | 高于 | 2.5 ms | 已计算受限 |
| T=64 | 5.33 | 高于 | 1.26 ms | 已计算受限 |
| T=128 | 10.67 | 高于 | 0.63 ms | 已计算受限 |

**结论：AI 一旦跨过 0.5 这个机器平衡点（T ≈ 32 就跨过了），再把 T 加大只是在减少本来就不构成瓶颈的流量，拿不到任何加速。** 这就是"分块尺寸必须让工作集装进 L2 而不是 L1"的真正含义——不是"L2 比 L1 更强"，而是：

1. 装进 L2 就**已经足够**把程序推到计算受限区；
2. 硬往 L1 挤（T=32，24 KiB）在 32 KB L1D 上只剩 8 KB 余量、在 48 KB L1D 上也只剩一半，还要跟硬件预取器抓进来的相邻行、栈、其他数据抢空间，极易抖动；
3. T=64（96 KiB）稳稳落在 L2，代价只是每次命中多付十几个 cycle（L1D 约 4 cycle，L2 实测约 14 cycle（Zen 4）/ 17 cycle（Lion Cove）），而这点延迟在乱序 + SIMD 下被大量在途失效掩盖了。

Intel 官方优化手册对服务器端的建议与这个推导一致——原文是："Consider blocking to L2 on Skylake Server microarchitecture if L2 can sustain the application's bandwidth requirements."（[Intel 64 and IA-32 Architectures Optimization Reference Manual](https://cdrdv2-public.intel.com/779559/355308-Software-Optimization-Manual-047-Changes-Doc.pdf)，2019）

### 1.6 换循环序 vs 分块：两件事，收益差一个数量级

一个常见的误解是"转置/换序就够了"。把循环改成 `ikj` 后，内层 `j` 循环里 `B[k][j]` 与 `C[i][j]` 都是单位步长：

```c
for (int i = 0; i < N; i++)
    for (int k = 0; k < N; k++) {
        double a = A[i*N + k];
        for (int j = 0; j < N; j++)
            C[i*N + j] += a * B[k*N + j];   // B 行、C 行都是顺序访问
    }
```

此时 `C` 的一行（8 KB）在整个 `k` 循环里常驻 L1D（与当前 `B` 行共 16 KB，32 KB L1D 装得下），只在换 `i` 时重取一次。但 `B` 仍然是**每个 `i` 被完整扫一遍**：单个 `i` 就要取 1024 × 128 = 131,072 条行 = 8 MB，1024 个 `i` 合计 **1.342 × 10⁸ 次失效**。

| 版本 | L1D 失效次数 | L2→L1 流量 | MPKI | 带宽口径耗时（320 GB/s） | 是否计算受限（13.4 ms） |
|---|---|---|---|---|---|
| 朴素 `ijk` | 1.208 × 10⁹ | 77.3 GB | 188 | 242 ms | 否，慢 18× |
| 只换 `ikj` | 1.34 × 10⁸ | 8.6 GB | 21 | 26.8 ms | 否，仍慢 2× |
| `ikj` + 分块 T=64 | 6.3 × 10⁶ | 403 MB | ≈1 | 1.26 ms | 是 |

换循环序把行利用率从 12.5% 拉回 100%，一次拿到 **9 倍**；分块解决的是另一个问题——**让 `B` 的每一行被 64 个不同的 `i` 复用**——再拿到 **21 倍**。两者正交，缺一不可，这正好对应本站题目"先转置、再分块"的两步解法。

---

## 二、硬件全景：从地址切分到一致性

### 2.1 层级与分工

取指 → 译码 → 执行 → 访存 → retire 这条链路上，缓存插入的位置是：**L1I 喂取指、L1D 接访存，L2 兜住二者的失效，LLC 兜住 L2 的失效并充当一致性归结点。**

- **L1I / L1D 分离**（哈佛式前端）：指令流只读、可容忍更高的失效延迟，数据流要读写且参与一致性，拆开后两者互不干扰。
- **L2 私有并统一指令与数据**：Intel/AMD/ARM 的当代大核都如此。
- **LLC 共享**：x86 上通常是 L3，被同 die / 同 CCX / 同 ring 上的所有核共享。

例外值得单独记一笔：

- **Apple 没有传统 L3**。以 M4 为例（数据来自 [Apple Silicon CPU Optimization Guide，经第三方整理](https://jia.je/hardware/2025/05/21/apple-m4/)）：4 个 P 核共享 16 MiB L2（16 路，128 B 行），E 核簇共享 4 MiB L2（16 路，128 B 行）；再往上是全 SoC 共享的 **Memory Cache（即 SLC）**，M1/M2/M3/M4 标准版为 8 MiB、16 路、128 B 行。它同时服务 GPU、NPU、媒体引擎，更接近"内存侧缓存"而非"CPU 的 L3"。
- **L4 / 内存侧缓存**：Intel 曾在 Haswell / Broadwell 的 Iris Pro 型号上挂过 128 MB eDRAM，官方称之为 Level 4 cache（代号 Crystal Well，CPU 与 GPU 共享，见 [TechSpot i7-5775C 评测参数表](https://www.techspot.com/review/1028-intel-core-i7-5775c-broadwell/)，2015）。这条路线后来没有延续。

### 2.2 组相联与地址位划分

一条 64 B 行需要 6 bit 行内偏移。32 KB / 8 路的 L1D 有 512 行、64 组 → 组索引 6 bit，剩下的是 tag。命中判定 = 用索引挑组，再用 tag 并行比对该组的 8 个路。

**为什么 index 用虚拟地址低位、tag 用物理地址？** 因为 TLB 查表可以和缓存取行并行做——先拿虚拟地址低位去读数据阵列和 tag 阵列，等物理地址出来再比 tag，省掉整整一次 TLB 延迟，这就是 VIPT。

代价是 **别名（synonym）问题**：

- **homonym（同名）**：同一虚拟地址在不同进程映射到不同物理地址。物理 tag 天然解决。
- **synonym（同义）**：不同虚拟地址映射到同一物理地址。物理 tag 解决不了——同一个物理行可能被缓存在多个组里，写一处不会反映到另一处。

工程上的规避办法是让索引位全部落在**页内偏移**之内（4 KB 页 → 页内 12 bit，减去 6 bit 行内偏移，只剩 6 bit 索引 = 64 组）。于是 VIPT 缓存的容量上限 = **页大小 × 路数**：

| 配置 | 页大小 | 索引位 | 组数 | 容量上限 = 页 × 路数 |
|---|---|---|---|---|
| x86，4 KB 页，8 路 | 4 KB | 6 | 64 | 32 KB |
| x86，4 KB 页，12 路 | 4 KB | 6 | 64 | 48 KB |
| Apple M4，16 KB 页，8 路 | 16 KB | 8 | 256 | **128 KB** |

最后一行解释了为什么只有 Apple 能把 L1D 做到 128 KB：它用的是 16 KB 页（[Apple Silicon CPU Optimization Guide](https://jia.je/hardware/2025/05/21/apple-m4/)，M4 P 核 L1D 官方标称为 128 KiB / 8 路 / 64 B 行）。

被淘汰的做法是 **page coloring**（操作系统保证虚拟别名不共存），早期 SPARC / RS/6000 用过，现在没人用了——硬件检测并驱逐别名的成本已经降下来，而软件维持完美着色的复杂度和性能损失太高（[Wikipedia: CPU cache](https://en.wikipedia.org/wiki/CPU_cache)）。L2 及以上几乎全是 PIPT，因为容量一大，虚拟索引的收益抵不上别名代价。

### 2.3 替换策略：从 LRU 到 RRIP

真 LRU 在 8 路、12 路、16 路上要维护全序，元数据与比较逻辑成本过高，所以实际用的是近似：

- **NRU / Pseudo-LRU**：每块 1 bit，命中清零，全 1 时整体翻转（或树形 bit 指向受害者）。
- **RRIP（Re-Reference Interval Prediction）**：每块 2 bit 的 RRPV，插入时预测"很久以后才会再访问"，命中时把 RRPV 降到 0，驱逐 RRPV 最大的块。扫描型负载插入即 3，很快被换出，不会把活跃工作集冲掉（[Jaleel et al., ISCA 2010](https://csg.csail.mit.edu/6.S078/6_S078_2012_www/handouts/isca2010-rrip.pdf)）。

### 2.4 一致性：MESI、MOESI 与伪共享

MESI 的四态里，若 Core 0 持有一条 **Modified** 行而 Core 1 要读它，必须先写回内存再由 Core 1 读——一次写回 + 一次读。

**MOESI 增加 Owned 态**（[AMD64 Architecture Programmer's Manual Vol.2 描述的协议，见 Wikipedia: MOESI](https://en.wikipedia.org/wiki/MOESI_protocol)）：Core 0 从 M 转 O，**直接把脏数据交给 Core 1**，内存里的旧副本保持陈旧，由 O 态的持有者负责将来写回。省掉的是"写回内存 + 从内存读"这两步，换来的是协议状态机更复杂，且必须保证共享者中恰好一个响应 snoop。Intel 走的是另一条路：**MESIF**，增加 Forward 态，指定唯一的干净响应者，避免多个 S 态同时回应的"惊群"。

**伪共享（false sharing）** 是这套机制最常见的副作用：两个核写同一条行内的不同变量，行在 M/I 之间来回迁移。第三方实测（[unseel.com，Cascade Lake](https://unseel.com/cs/false-sharing)）：16 线程无填充时 12 M 次/秒，按 64 B 对齐填充后 **1400 M 次/秒**，相差两个数量级。

### 2.5 写策略，以及 L1D 真正稀缺的三种资源

现代数据缓存几乎一律是**写回（write-back）+ 写分配（write-allocate）**：store 只改缓存并置脏，等行被驱逐时才写回；store 未命中时先把整行取进来再写。写直达（write-through）只留在 L1I 和部分嵌入式设计里——它对只读的指令流无所谓，对数据流则会把每次 store 都变成一次下游写事务。

写回的代价是**失效不总是免费的**：驱逐一个脏行要在同一条通道上多排一次写回，和填充请求争用带宽。这也是为什么在带宽吃紧的负载里，减少失效次数比缩短单次失效延迟更值钱。

L1D 上真正稀缺的其实是三种**有限条目**资源，它们互相争用：

- **填充缓冲 / MSHR**：跟踪在途的 L1D 失效，决定了能同时有多少个失效在飞（即 MLP 上限）。
- **写回缓冲**：暂存被驱逐的脏行。
- **snoop / 一致性端口**：处理别核发来的探测请求（见 2.4 节）。

MSHR 与填充缓冲的数量厂商基本不公开，只能靠第三方实测反推：Haswell 的 L1 约 10 个 MSHR，Pentium 4 约 8 个，Cortex-A72 约 6 个（[Kiriansky, MIT 博士论文, 2019](https://commit.csail.mit.edu/papers/2019/vkiriansky19phd.pdf)；[Cimple, PACT 2018](https://commit.csail.mit.edu/papers/2018/kiriansky-pact18-cimple.pdf)）——**均为第三方实测/逆向估计，非官方数字**。这部分与本站「内存层次与延迟隐藏」模块交叉，那里会展开 MLP 与延迟隐藏。

### 2.6 包含策略：inclusive / non-inclusive / exclusive

| 策略 | 含义 | 一致性代价 | 有效容量 |
|---|---|---|---|
| Inclusive | L2 的内容必在 L3 | L3 目录即可判定全片有无，snoop 只需查 L3 tag | = L3 |
| Non-inclusive | L2 内容**可能**在 L3 | 需要额外的 snoop filter / 目录 | ≈ L2 + L3 |
| Exclusive | L2 内容**必不**在 L3 | 同 non-inclusive | = L2 + L3 |

Intel 从 **Skylake-Server** 起把 L3 转为 non-inclusive（[Intel Optimization Reference Manual §2.5.1.2](https://cdrdv2-public.intel.com/779559/355308-Software-Optimization-Manual-047-Changes-Doc.pdf)），同时把 L2 从 256 KB 放大到 1 MB。动机与后果都很清楚：

- **动机**：inclusive L3 里有一整份 L2 副本是纯浪费。转 non-inclusive 后有效容量从 1.375 MB/核 变成 L2 + L3 ≈ 2.375 MB/核。
- **后果**：L3 未命中不再意味着"片上一定没有"，必须靠分布式 snoop filter 追踪各核 L1/L2 的内容（[WikiChip: Skylake server](https://en.wikichip.org/w/index.php?title=intel/microarchitectures/skylake_(server)&oldid=76794&diff=prev)，Skylake-SP snoop filter 为 2048 组 × 12 路）。手册原话是：程序若要在运行时估计每核有效缓存容量，应把 mid-level 与 last-level **相加**。

AMD 的公开描述与此一致：Hot Chips 2024 的 Zen 5 材料写明 "L3 is filled from L2 victims"、"L2 tags duplicated in L3 for probe filtering and fast cache transfer"（[AMD, Hot Chips 2024](https://hc2024.hotchips.org/assets/program/conference/day2/24_HC2024.AMD.Cohen.Subramony.final.pdf)）——即 L3 是受害者缓存 + 目录，属于非包含式。

---

## 三、演进史：每一步解决了什么、代价是什么

### 3.1 直接映射 → 组相联 → 路数增长的边际递减

- **直接映射**：命中路径最短（一次取行、一次比较），但两个恰好映射到同一槽的热变量会**每次访问互相驱逐**（抖动/冲突失效）。
- **组相联**：给了 N 个候选槽，冲突失效显著下降。代价是 N 个 tag 比较器并行工作、N 路数据 mux、功耗与面积上升，命中时间变长。
- **路数继续增长**：从 2 路到 4 路收益很大，8 路到 16 路收益已经很薄，这也是为什么 L1D 长期停在 8–12 路，而 LLC 才用 12–16 路（LLC 更怕冲突失效，且延迟预算宽松）。

### 3.2 替换策略：LRU → Pseudo-LRU → RRIP

每一步都在解决"上一个策略对某类访问模式的系统性误判"：

1. **LRU**：假设"最近用过 = 很快再用"。工作集大于缓存时（thrashing），这个假设彻底失效——LRU 退化成随机驱逐。
2. **DIP / LIP**（ISCA 2007）：改成"新来的很可能很久以后才用"，保留部分工作集。代价是**对所有访存用同一个预测**，遇到"扫描 + 活跃工作集"混合模式就不灵了。
3. **RRIP**（ISCA 2010）：给每块一个 2-bit 的重引用间隔预测，混合模式也能区分。论文的实测数据（[Jaleel et al., ISCA 2010](https://csg.csail.mit.edu/6.S078/6_S078_2012_www/handouts/isca2010-rrip.pdf)，单核 2 MB LLC / 4 核 8 MB LLC）：

| 指标 | SRRIP | DRRIP / TA-DRRIP |
|---|---|---|
| 单核吞吐平均提升（vs LRU） | **4%** | **10%** |
| 4 核 CMP 吞吐平均提升 | **7%**（最高 25%） | **10%**（最高 2.1×） |
| MPKI 下降幅度 | 5–15%（14 个负载中的 8 个） | — |
| 不同 LLC 容量（512 KB–8 MB）下的表现 | 优于 LRU **5–20%** | — |
| 硬件开销 | 每块 2 bit，比 LRU 少 **2×**，比 LFU 少 **2.5×** | 同 |

论文里两个容易被忽略、但很关键的负结果：

- **RRIP 在 L1 上没有任何收益**（缓存太小、时间局部性太高），在 256 KB 的 L2 上收益也不显著。它只对 **LLC** 有效——因为上游小缓存已经把强局部性的访问过滤掉了，到达 LLC 的才是需要预测的那部分。
- TA-DRRIP 在 1001 个多道负载里有 **不到 25 个**出现 2–5% 的性能下降，代价来自 set dueling 的试错开销。

后续还有 **SHiP**（用签名/PC 预测死块）等基于采样的策略，思路一致：把"驱逐谁"从启发式变成预测问题。

### 3.3 预取器出现之后，"命中率"这个指标开始失真

预取器会把大量**从未被真正使用**的行塞进缓存，于是"命中率上升"不再等价于"程序变快"。评价预取要同时看三个维度（[Wikipedia: Cache prefetching](https://en.wikipedia.org/wiki/Cache_prefetching) 与 [Srinath et al., HPCA 2007](https://hps.ece.utexas.edu/pub/srinath_hpca07.pdf)）：

- **Coverage**：被预取消除的原始失效占比。
- **Accuracy**：发出的预取中真正被用到的比例。
- **Timeliness**：行到得够不够早——太晚仍要停顿，太早可能在被用之前就被换出。

HPCA 2007 的实测给出了两个分界点：

- 在 `applu`、`galgel`、`ammp` 上预取**准确率低于 40%**，开流预取器**总是比不开更慢**；准确率超过 40% 的负载则普遍明显变快。
- `mcf` 的准确率接近 100%，但**超过 90% 的有效预取都到得太晚**，所以几乎没有收益。

预取器的细节属于本站 [内存层次与延迟隐藏](../reading/memory-hierarchy-latency-hiding.html) 篇的范畴，这里不展开。

### 3.4 被淘汰的思路

- **全相联内容寻址（CAM）**：命中率最优，但每次查找要与所有行的 tag 并行比较，功耗和面积随容量线性爆炸，只在极小容量（TLB、部分 uop cache）上用。
- **纯软件管理的 scratchpad / 显式 SPM**：把 SRAM 暴露成可直接寻址的本地内存，由编译器或程序员显式搬运。它在 DSP、GPU 共享内存、Cell SPE 上是主流，但**没能在通用 CPU 上普及**——代价是要求软件显式管理数据布局、破坏二进制兼容、且对不可静态分析的访问模式无能为力。缓存的"对软件透明"这一性质，在通用计算场景里价值高于那点面积和功耗。（这一段属于体系结构界的共识性判断，本文未找到可引用的单一权威一手来源。）
- **eDRAM L4**（Crystal Well 128 MB）：带宽与容量都好看，但成本高、与主流的核显/独显配置耦合，2015 年之后没有延续（[TechSpot, 2015](https://www.techspot.com/review/1028-intel-core-i7-5775c-broadwell/)）。

---

## 四、真实处理器对比（2024–2026）

> 表中所有数字都标注来源。**厂商未公开、只能靠第三方逆向或实测得到的数字已显式标为"第三方实测"。**

### 4.1 参数总表

| 维度 | Intel（Lion Cove / Redwood Cove） | AMD（Zen 5 / Zen 4） | ARM（Neoverse V2 / V3） | Apple（M4 / M3） |
|---|---|---|---|---|
| L1D 容量 / 路数 | 48 KB / 12 路（Intel 称 L0D） | Zen 5：48 KB / 12 路；Zen 4：32 KB / 8 路 | 64 KB / 4 路 | 128 KB / 8 路（M3/M4 P 核相同） |
| L1D 延迟 | 4 cycle（Lion Cove），5 cycle（Redwood Cove） | 4 cycle（第三方实测） | 4 cycle（第三方实测，最低值） | 3 cycle（官方优化指南，pointer-chasing） |
| L2 容量 | 2.5 MB（Lunar Lake）/ 3 MB（Arrow Lake） | 1 MB（Zen 4/Zen 5 相同） | V2：1 或 2 MB；V3：2 MB 或 3 MB | 16 MB / P 簇（4 核共享），4 MB / E 簇 |
| L2 延迟 | 17 cycle（Lion Cove），16 cycle（Redwood Cove） | 14 cycle（Zen 4，第三方实测）；Zen 5 未找到官方数据 | V2：10 cycle（ARM 标称）/ 11 cycle（第三方实测）；V3：10 cycle（2 MB）或 12 cycle（3 MB） | 未找到官方数据；第三方 pointer-chasing 在 14–16 cycle 量级 |
| L2 带宽 | 32 B/clk（Lion Cove 实测上限） | **Zen 4：32 B/clk → Zen 5：64 B/clk（翻倍）** | 128 B/clk（ARM Hot Chips 标称），实测线性读仅约 32 B/clk | 未找到官方数据 |
| LLC / SLC | 36 MB（Arrow Lake 全片共享）；12 MB（Lunar Lake，仅 P 核） | 32 MB / CCD，16 路；X3D 型号 96 MB | 由 SoC 决定（Graviton4 为 36 MB；V3 官方未定） | SLC（Memory Cache）8 MB / 16 路 / 128 B 行 |
| LLC 延迟 | 51 cycle（Lunar Lake）/ 84 cycle（Arrow Lake，ring 更长） | 未找到官方数据；Zen 5 比 Zen 4 低 3.5 cycle | 68 cycle（Graviton4，第三方实测） | 未找到官方数据；第三方约 50 cycle 量级 |
| 行大小 | 64 B | 64 B | 64 B | **L1D/L1I 为 64 B，L2 与 SLC 为 128 B** |
| 中间层 | Lion Cove 增加 192 KB "L1"（9 cycle），形成 L0/L1/L2 三级 | 无 | 无 | 无（L2 即簇内末级） |
| 包含策略 | 客户端未找到官方数据；服务器端自 Skylake-SP 起 non-inclusive | L3 由 L2 victim 填充 + L2 tag 复制做目录 → 非包含式 | 未找到官方数据 | 未找到官方数据 |
| L4 / 内存侧缓存 | 无（Crystal Well 已停产） | 无 | 视 SoC 而定 | SLC 由 CPU/GPU/NPU/媒体引擎共享，接近内存侧缓存 |

主要来源：[Chips and Cheese: Lion Cove](https://chipsandcheese.com/p/lion-cove-intels-p-core-roars)（2024，第三方实测）；[AMD Hot Chips 2024 "Zen 5" 演讲页](https://hc2024.hotchips.org/assets/program/conference/day2/24_HC2024.AMD.Cohen.Subramony.final.pdf)（官方）；[Wikipedia: Zen 5](https://en.wikipedia.org/wiki/Zen_5)（2024）；[ARM Neoverse V3 官方规格页](https://developer.arm.com/compute-ip/neoverse-v3)（官方）；[Chips and Cheese: Neoverse V2 in Graviton 4](https://old.chipsandcheese.com/2024/07/22/arms-neoverse-v2-in-awss-graviton-4/)（2024，第三方实测）；[Apple Silicon CPU Optimization Guide 整理（jia.je）](https://jia.je/hardware/2025/05/21/apple-m4/)（2025）；[TechPowerUp: Core Ultra 9 285K](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html)（2024）。

### 4.2 Zen 5 的当代变化：容量不动，带宽翻倍

Zen 5 相对 Zen 4 在缓存上的改动可以一句话概括：**L2 容量仍是 1 MB，但路数 8 → 16、带宽 32 B/clk → 64 B/clk，L3 延迟降低 3.5 cycle，L1D 从 32 KB / 8 路扩到 48 KB / 12 路**（[AMD Hot Chips 2024](https://hc2024.hotchips.org/assets/program/conference/day2/24_HC2024.AMD.Cohen.Subramony.final.pdf)）。这是"先补带宽再补容量"的典型取舍——前几代 L2 已经从 256 KB 涨到 1 MB，继续涨容量的收益不如把数据更快地搬到执行单元。

### 4.3 3D V-Cache：哪些负载收益大，哪些几乎没收益

AMD 的 3D V-Cache 把一片 64 MB SRAM 堆在 CCD 上。Zen 3 的 CCD 原生就有 32 MB L3，叠加后 **96 MB**（[TechInsights, 2022](https://www.techinsights.com/blog/amd-ships-3d-v-cache-processors)）；Zen 5 的 9800X3D 改把缓存放在核心**下方**，改善了散热，基频比 7800X3D 高 500 MHz 并首次允许超频（[Wikipedia: Zen 5](https://en.wikipedia.org/wiki/Zen_5)，2024）。

关键在于收益的**极度不均衡**：

**收益大的**：工作集落在"原生 L3 装不下、96 MB 装得下"这个窗口里的负载。

- 游戏（Windows，1080p）：5800X3D 相对非 X3D 同款平均 **+21%**；《F1 2021》+44%、《微软模拟飞行》+49%（前两个数字为 5800X3D vs 5800X，引自 [Tom's Hardware 7950X3D 评测](https://www.tomshardware.com/reviews/amd-ryzen-9-7950x3d-cpu-review/6)，2023；7950X3D vs 7950X 在这两款游戏上分别为 +38% 与 +53%）。
- Linux 技术计算：Phoronix 在 285 项基准里，AI/ML（oneDNN、ONNX Runtime、NCNN、Lc0）与 HPC（OpenFOAM、Incompact3D、ASKAP）收益显著，部分项目领先幅度很大（[Phoronix, 2022](https://www.phoronix.com/review/amd-5800x3d-linux/8)）。
- 压缩：Zstd 是通用负载里少见的明显赢家（同上文）。

**几乎没收益、甚至变慢的**：

- **通用桌面/综合负载**：Phoronix 的 285 项基准里，几何平均只快 **3.9%**，而且 **61% 的项目 5800X 反而更快**。
- **工作集本来就装得进 32 MB 的负载**：多加 64 MB 只是多了一层更慢的查找。
- **工作集远超 96 MB 的负载**：还是要去 DRAM，多出来的容量只是把悬崖往后推了一点。
- **对频率敏感、或对核数敏感的负载**：5800X3D 基频 3.4 GHz，而 5900X 是 4.7 GHz；TechInsights 明确写道，不受益于额外缓存的负载在旧型号上反而更快。
- **GPU 受限的场景**：同一款游戏在 4K + 中端显卡下，缓存差距被 GPU 吃掉。
- 即使是同一款游戏也不保证：7950X3D 在《GTA V》上相对 Core i9-13900K 是 **−1.7%**（同一 Tom's Hardware 篇）。

一句话归纳：**3D V-Cache 买的是"把悬崖往右推 64 MB"，只对恰好站在那段悬崖上的负载付款。**

---

## 五、延伸知识点

**1. 分块的目标层是 L2，不是 L1。** 这是本文第一部分的核心结论，也是 Intel 官方手册在 Skylake-Server 上的明确建议（[Intel Optimization Reference Manual](https://cdrdv2-public.intel.com/779559/355308-Software-Optimization-Manual-047-Changes-Doc.pdf)）。判断标准不是"能不能装进 L1"，而是"算术强度有没有跨过机器平衡点"。

**2. 步长为 2 的幂时要警惕组冲突，而不只是跨行。** 8 KiB 步长让 `B` 的一整列 1024 个元素落进同一个组，在 8 路缓存里复用率直接归零。改 `T` 也救不了——要在数据结构层面加 padding、做转置（本站题目的解法），或者改用 `ikj` 循环序。

**3. 64 B 填充在 Apple 平台上不一定够。** x86 上按 64 B 对齐填充即可（第三方实测 12 M/s → 1400 M/s，[unseel.com](https://unseel.com/cs/false-sharing)）；但 Apple Silicon CPU Optimization Guide 列出 M4 的 **L2 与 Memory Cache 行大小为 128 B**（[jia.je 整理](https://jia.je/hardware/2025/05/21/apple-m4/)），第三方的 M4 实测也按 128 B 描述粒度。跨核一致性粒度若按 128 B 计，64 B 填充就不够。**Apple 未公开跨核一致性粒度的明确说明，这条属于推断，跨平台代码建议直接按 128 B 填充。**

**4. 排查伪共享用 `perf c2c`，不要靠猜。** `perf c2c record` + `perf c2c report --stdio` 会给出 Shared Data Cache Line Table，重点看 **HITM**（命中了别核的 Modified 行）与 **Remote HITM**（跨 NUMA 节点，代价最高）两列；Red Hat 的示例输出里"LLC Misses to Remote cache (HITM)"占到 57.3% 就是典型信号（[Red Hat RHEL 8 文档](https://docs.redhat.com/es/documentation/Red_Hat_Enterprise_Linux/8/html/monitoring_and_managing_system_status_and_performance/_detecting_false_sharing_with_perf_c2c_2)，[perf c2c 实例](https://coffeebeforearch.github.io/2020/03/27/perf-c2c.html)）。

**5. "命中率上升但程序变慢"是可诊断的。** 把命中率拆成 coverage / accuracy / timeliness 三个维度看：accuracy 低于 40% 时预取器几乎必然拖慢程序；accuracy 接近 100% 但大多数预取到得太晚（如 `mcf`）同样没有收益（[Srinath et al., HPCA 2007](https://hps.ece.utexas.edu/pub/srinath_hpca07.pdf)）。同理，本文 1.3 节里朴素 ijk 的 LLC 命中率有 99.97%，却慢了 18 倍——**命中率必须注明是在哪一级、以什么为分母**。

**6. 分块尺寸与 TLB 是耦合的。** `N=1024` 的 `double` 矩阵每个 8 MiB，在 4 KB 页下是 2048 页，三个矩阵共 6144 页，远超过 L1 DTLB 的条目数（Zen 5 为 96 项、Zen 4 为 72 项、Apple M4 P 核为 160 项）。分块到 `T=64` 后工作集只有 96 KiB = **24 页**，稳稳落在 L1 DTLB 里。换句话说，分块同时也在给 TLB 做局部性优化——这部分与本站 [TLB 与地址转换](../reading/tlb-address-translation-guide.html) 篇的内容交叉，那里会展开页大小、大页与 TLB 失效的处理。

**7. 顺序访问时，硬件预取器会让分块的收益缩水。** 1.6 节的流量是按"每次失效都要从 L2 取一行"算的。实际上一旦把 `B` 转置、或改用 `ikj` 序，`A`、`B`、`C` 全变成顺序流，硬件 stride 预取器会把大部分失效提前取走，实际耗时通常好于表中的带宽口径估计——这就是为什么"只转置不分块"在实测里往往能拿到朴素版的大部分收益。分块剩下的、预取器替代不了的价值是**容量复用**：让同一块 `A`/`B` 被 64 个不同的 `i`/`j` 复用，这是预取器再准也做不到的。反过来说，如果你的负载本来就是纯流式（遍历一次就不再访问），分块基本没用，该做的是提高带宽利用率。预取器的工作细节见本站 [内存层次与延迟隐藏](../reading/memory-hierarchy-latency-hiding.html) 篇。

---

## 参考来源

- [Intel 64 and IA-32 Architectures Optimization Reference Manual](https://cdrdv2-public.intel.com/779559/355308-Software-Optimization-Manual-047-Changes-Doc.pdf) —— Skylake-Server non-inclusive LLC、L2 1 MB、"Consider blocking to L2" 官方建议、各级缓存延迟与带宽对比表（2019）
- [Jaleel et al., High Performance Cache Replacement Using Re-Reference Interval Prediction (RRIP), ISCA 2010](https://csg.csail.mit.edu/6.S078/6_S078_2012_www/handouts/isca2010-rrip.pdf) —— 2-bit RRPV、SRRIP 平均 +4%/+7%、DRRIP +10%、MPKI 降 5–15%、硬件比 LRU 少 2×、L1/L2 上无收益（2010）
- [Next Generation "Zen 5" Core, AMD Hot Chips 2024](https://hc2024.hotchips.org/assets/program/conference/day2/24_HC2024.AMD.Cohen.Subramony.final.pdf) —— L1D 48 KB/12 路、L2 1 MB/16 路、L2 带宽 32→64 B/clk、L3 由 L2 victim 填充、L2 tag 复制做 probe filter（2024）
- [Wikipedia: Zen 5](https://en.wikipedia.org/wiki/Zen_5) —— Zen 4 vs Zen 5 各级缓存容量/路数/带宽对照表，Zen 5 3D V-Cache CCD 共 96 MB、缓存置于核心下方（2024）
- [Chips and Cheese: Lion Cove, Intel's P-Core Roars](https://chipsandcheese.com/p/lion-cove-intels-p-core-roars) —— L0D 48 KB 4 cycle、新增 192 KB "L1"/L1.5 9 cycle、L2 2.5/3 MB 17 cycle、L3 延迟与历代 L2/L3 演进表（2024，第三方实测）
- [TechPowerUp: Intel Core Ultra 9 285K 架构解析](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html) —— Arrow Lake Lion Cove L2 3 MB/核、全片共享 36 MB L3、E 核簇共享 4 MB L2（2024）
- [ARM Neoverse V3 官方规格页](https://developer.arm.com/compute-ip/neoverse-v3) —— L1I/L1D 64 KB 4-way、私有 L2 2 MB 8-way 10 cycle / 3 MB 12-way 12 cycle（ARM 官方）
- [Chips and Cheese: Arm's Neoverse V2 in AWS's Graviton 4](https://old.chipsandcheese.com/2024/07/22/arms-neoverse-v2-in-awss-graviton-4/) —— L1D 64 KB 4 cycle、L2 2 MB 11 cycle、L3 68 cycle、ARM 从 pseudo-LRU 换成 RRIP（2024，第三方实测）
- [Apple M4 微架构评测（整理自 Apple Silicon CPU Optimization Guide）](https://jia.je/hardware/2025/05/21/apple-m4/) —— M4 P/E 核 L1D 容量与路数、L2 16 MB/4 MB 簇共享、Memory Cache 8 MB、L2/SLC 行 128 B、load-to-use 3 cycle、16 KB 页（2025）
- [Apple M2 微架构评测（Apple Silicon CPU Optimization Guide 整理，含 M1–M4 全系 SLC 配置）](https://jia.je/hardware/2026/07/06/apple-m2/) —— M1/M2/M3/M4 SLC 均 8 MiB 16 路 128 B 行；M3 Max/M4 Max 为 48 MiB（2026）
- [WikiChip: Intel Skylake (server)](https://en.wikichip.org/w/index.php?title=intel/microarchitectures/skylake_(server)&oldid=76794&diff=prev) —— non-inclusive L3、1.375 MB/核、11 路、snoop filter 2048 组 12 路、L2 1 MB 16 路 14 cycle
- [Wikipedia: MOESI protocol](https://en.wikipedia.org/wiki/MOESI_protocol) —— Owned 态允许脏共享、避免写回内存后再读，引自 AMD64 Architecture Programmer's Manual Vol.2
- [unseel.com: False Sharing 实测](https://unseel.com/cs/false-sharing) —— Cascade Lake 16 线程 12 M/s → 64 B 填充后 1400 M/s
- [Srinath et al., Feedback Directed Prefetching, HPCA 2007](https://hps.ece.utexas.edu/pub/srinath_hpca07.pdf) —— 预取 accuracy/coverage/lateness 定义；accuracy <40% 时流预取器必然拖慢；mcf 准确率近 100% 但 >90% 太晚
- [Wikipedia: Cache prefetching](https://en.wikipedia.org/wiki/Cache_prefetching) —— 预取三指标 coverage / accuracy / timeliness 的标准定义
- [Phoronix: AMD Ryzen 7 5800X3D on Linux](https://www.phoronix.com/review/amd-5800x3d-linux/8) —— 285 项基准几何平均仅 +3.9%、61% 项目反而更慢、AI/ML 与 HPC 收益显著（2022）
- [Tom's Hardware: Ryzen 9 7950X3D 评测](https://www.tomshardware.com/reviews/amd-ryzen-9-7950x3d-cpu-review/6) —— 5800X3D vs 5800X：F1 2021 +44%、微软模拟飞行 +49%；7950X3D vs 7950X：+38% / +53%；7950X3D vs 13900K 在 GTA V 上 −1.7%（2023）
- [TechInsights: AMD Ships 3D V-Cache Processors](https://www.techinsights.com/blog/amd-ships-3d-v-cache-processors) —— 64 MB SRAM 堆叠 + 32 MB 原生 L3 = 96 MB；5800X3D 基频 3.4 GHz vs 5900X 4.7 GHz；不受益负载反而更慢（2022）
- [TechSpot: Core i7-5775C Broadwell 评测](https://www.techspot.com/review/1028-intel-core-i7-5775c-broadwell/) —— Crystal Well 128 MB eDRAM L4 参数表（2015）
- [Wikipedia: CPU cache（VIPT / homonym / synonym / page coloring）](https://en.wikipedia.org/wiki/CPU_cache) —— VIPT 索引位受页大小限制、page coloring 被淘汰的原因
- [Red Hat RHEL 8: Detecting false sharing with perf c2c](https://docs.redhat.com/es/documentation/Red_Hat_Enterprise_Linux/8/html/monitoring_and_managing_system_status_and_performance/_detecting_false_sharing_with_perf_c2c_2) —— HITM / Remote HITM 判读方法
- [Detecting False Sharing with Perf C2C](https://coffeebeforearch.github.io/2020/03/27/perf-c2c.html) —— perf c2c 实操与 HITM 含义（2020）

---

## 系列导航：处理器微架构深度指南

本文是《处理器微架构深度指南》系列之一。

**同系列其他文章**：

- [分支预测完全指南：从一次数据重排优化说起](branch-prediction-guide.html)
- [编译器如何自动优化分支预测](compiler-branch-optimization.html)
- [ILP 与流水线完全指南：从八累加器点积说起](ilp-and-pipeline-complete-guide.html)
- [内存层次与延迟隐藏：从指针追逐说起](memory-hierarchy-latency-hiding.html)
- [ROB 与乱序执行完全指南：一次瓶颈会诊的完整拆解](rob-out-of-order-complete-guide.html)
- [前端与指令缓存：为什么过度展开反而更慢](frontend-and-instruction-cache.html)
- [SIMD 与向量化完全指南：从 SAXPY 到 AVX10 与矩阵扩展](simd-vectorization-complete-guide.html)
- [TLB 与地址转换：从一次聚集重排说起](tlb-address-translation-guide.html)

所属模块：[缓存层次](../modules/cache.html)
