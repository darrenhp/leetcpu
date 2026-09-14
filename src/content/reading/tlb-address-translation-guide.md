---
title: TLB 与地址转换：从一次聚集重排说起
module: tlb
date: 2026-09-14
order: 16
summary: 把随机聚集访问按页重排后变快，提升的往往不只是 cache 命中率。本文从可推导的数字出发，讲清页表遍历、多级 TLB、大页与虚拟化两层转换的真实代价，并对比 Intel、AMD、ARM、Apple 四家当前处理器。
tags: [TLB, 地址转换, 大页, 页表遍历, EPT, 微架构]
---
# TLB 与地址转换：从一次聚集重排说起

> 一次"把随机聚集按页重排"的优化，提升的往往不只是 cache 命中率。真正的瓶颈常常是地址转换缓存 TLB —— 一个容量只有几百项、却能决定大内存负载成败的结构。

---

## 一、一个案例：按 4KB 分桶，究竟在优化谁

### 题目背景

站点题目 [聚集重排 — 按缓存区域聚类访问](../problems/gather-reorder-cache-clustering.html) 的基线代码非常短：

```c
#define N 131072

long solve(const int *data, const int *indices, int n) {
    long sum = 0;
    for (int i = 0; i < n; i++)
        sum += data[indices[i]];   // indices 是 0..n-1 的一个随机排列
    return sum;
}
```

`data` 是 131072 个 `int` = 512 KB。基线按排列顺序访问，几乎每次都跳到数组的另一个角落。

官方解法做的事是"先按区域分桶，再按桶顺序聚集"。注意它的分桶粒度：

```c
#define PAGE_BYTES 4096
#define INTS_PER_PAGE (PAGE_BYTES / (int)sizeof(int))
```

**分桶粒度取的是 4096 字节，也就是页大小，而不是 64 字节的 cache line。** 这道被归在 "Cache Locality" 分类下的题，标准解法其实同时在优化两件不同的事。

### 推导一：页切换次数

4 KB 页、4 字节 `int`，每页 1024 个元素；512 KB 数组共 P = 128 个页。

在随机排列中，相邻两次访问落在同一页的概率约等于 1/P = 1/128。于是访问流中的"换页"次数为：

| 访问顺序 | 换页次数 |
|---|---|
| 随机排列 | (n - 1) × (1 - 1/128) = 131071 × 127/128 ≈ **130,048** |
| 按页排序后 | P - 1 = **127** |

比值约 1024 倍，正好等于每页元素数。这不是巧合：n 个元素摊在 P 个页上，顺序遍历时每个页只"冷"一次，随机遍历时每个元素都可能冷一次。

### 推导二：这些换页值多少钱

关键指标是 **TLB 覆盖范围（TLB reach）= 项数 × 页大小**：

| 结构 | 项数 | 4 KB 页覆盖范围 |
|---|---|---|
| L1 dTLB（传统小核） | 64 | 256 KB |
| L1 dTLB（Golden Cove / Zen 5） | 96 | 384 KB |
| L2 STLB（Intel / ARM） | 2048 | 8 MB |
| L2 DTLB（AMD Zen 5） | 4096 | 16 MB |

题目里 512 KB 的数组有 128 个页：128 > 96，随机顺序下 L1 dTLB 装不下；但 128 < 2048，L2 STLB 绰绰有余。所以这道题的随机版本付的是"L1 miss / L2 hit"这一档代价，在 Golden Cove 上用指针追逐实测是 **+7 个周期**（L1 dTLB 容量内 load-to-use 5 周期，超出后 12 周期）。

把规模放到 64 MB（16,777,216 个 `int`，16,384 个页）就完全是另一回事了：16,384 > 2048，L2 STLB 也装不下，随机访问几乎每次都要走完整的页表遍历。

| 访问顺序 | TLB miss 率 | miss 次数 |
|---|---|---|
| 随机排列 | 1 - 96/16384 ≈ **99.4%** | ≈ 1670 万 |
| 按页排序后 | 1/1024 ≈ **0.098%** | 16,384 |

miss 次数相差约 **1000 倍**。这个倍数只由"每页元素数 = 页大小 ÷ 元素大小"决定，与 CPU 型号无关。

### 推导三：不动访问顺序，只把页换成 2MB

4 KB → 2 MB 是 512 倍，这个数字有一手出处。AMD 在 ASPLOS 2008 的二维页表遍历论文里写道：一个 2 MB 大页表项可以存放在单个 TLB 表项中，而同样的 2 MB 地址范围若用 4 KB 页，需要 **512 个 TLB 表项**。

于是：

| 结构 | 4 KB 页 | 2 MB 页 |
|---|---|---|
| 96 项 L1 dTLB | 384 KB | **192 MB** |
| 2048 项 L2 STLB | 8 MB | **4 GB** |

64 MB 的工作集从"必然 miss"变成"全部命中"——**一行代码都不用改**。

### 常被混淆的两件事

| | 数据缓存 | TLB |
|---|---|---|
| 缓存内容 | 内存数据本身 | 虚拟页号 → 物理页号的映射 |
| 管理粒度 | 64 B cache line | 4 KB / 2 MB / 1 GB 页 |
| 未命中后 | 去下一级缓存或内存取行 | 硬件页表遍历器走页表 |
| 软件手段 | 分块、重排、预取 | 重排（同左）+ 大页 |

粒度差 64 倍，所以"按 64 B 分桶"和"按 4 KB 分桶"是两个不同的优化。这道题的标准解法按 4096 分桶，顺带把同一页内的 64 个 cache line 也聚到了一起——**两件事一起做了，但收益来源有两个**。至于预取（提前为随机地址流发请求）是另一条路，见 [不规则聚集](../problems/irregular-gather-prefetch.html) 与内存层次篇；缓存结构与局部性的系统讨论见本站缓存层次篇。
→ 4 KB 页边界会限制硬件预取器的跨页推进，这一点在 [内存层次与延迟隐藏](../reading/memory-hierarchy-latency-hiding.html) 篇展开。

---

## 二、硬件全景：一次访存要过几道关

### TLB 在流水线里的位置

按取指 → 译码 → 执行 → 访存 → retire 的顺序看，TLB 出现在两个地方，而且是**关键路径**上：

| 阶段 | 用到的 TLB | 未命中后果 |
|---|---|---|
| 取指 | L1 iTLB → L2 TLB | 取指停顿，前端空转 |
| 访存（地址生成之后、查 L1d 之前或同时） | L1 dTLB → L2 TLB | 硬件遍历页表，load 的延迟被拉长 |

关键点是：**地址翻译必须先于（或至少不晚于）缓存查找完成**，因为主流 L1 数据缓存是虚拟索引、物理标签（VIPT），需要用物理地址去比对标签。所以 L1 TLB 的查找延迟直接压在 L1 命中延迟上，这也是为什么 L1 TLB 只能做几十到一两百项——再大就查不快了。这个约束正好解释了下一段要讲的"为什么要分多级"。
→ L1D 为什么必须 VIPT、组相联怎么切分地址，见 [缓存层次完全指南](../reading/cache-hierarchy-complete-guide.html)。

### 多级页表与硬件页表遍历

x86-64 默认 4 级页表，48 位虚拟地址切成 4 段 9 位索引加 12 位页内偏移：CR3 → PGD(PML4) → PUD(PDPT) → PMD(PD) → PTE。每级 512 项 × 8 字节，正好一个 4 KB 页。

由此可以直接推出遍历代价：**4 级页表需要 4 次页表内存访问，加上最后真正的数据访问，一次 TLB miss 最多要 5 次内存访问。**

五级页表在 PGD 之上再加一级。Intel 白皮书原文：支持五级分页的处理器允许软件设置新的使能位 `CR4.LA57[bit 12]`；由于"五级分页把线性地址宽度提升到 57 位（四级分页为 48 位）"，因此"允许同时访问最多 128 PBytes 的线性地址空间"。代价是遍历变成 5 次页表访问、共 6 次内存访问。Linux 从 4.14 起支持，Ice Lake 是首代实现该扩展的硬件。

需要说明的一点：**页表遍历的具体周期数没有厂商统一的官方数字**，因为它取决于页表项当时在 L1、L2、LLC 还是 DRAM。能确定的只有两个：架构上必需的页表访问次数（由页表结构直接推出，4 级为 4 次、5 级为 5 次），以及 L1 miss / L2 hit 这一档的附加延迟（Intel Golden Cove、Lion Cove 与 AMD Zen 4、Zen 5 都在 7 周期量级，ARM Neoverse V2 为 6 周期）。

### 多级 TLB

| 层级 | 典型容量 | 组织 | 命中延迟 |
|---|---|---|---|
| L1 dTLB / iTLB | 几十 ~ 一两百项 | 全相联或低路数 | 约 1 周期，与 L1 缓存查找并行 |
| L2 STLB | 上千 ~ 四千项 | 8 ~ 24 路组相联 | 比 L1 多约 7 周期（Intel/AMD）、6 周期（Neoverse V2） |

指令和数据各有独立的 L1 TLB，避免大代码 footprint 把数据侧的转换挤掉。Golden Cove 上还配了 4 个独立的硬件页表遍历器，也就是说最多能同时有 4 条未完成的页表遍历在飞——这个数字限制了大 miss 率负载下能重叠多少遍历延迟。

把 L1 与 L2 串起来看，一次访存的地址翻译有三档结局：

| 结局 | 附加延迟（典型值） | 发生条件 |
|---|---|---|
| L1 TLB 命中 | 0（与 L1 查找重叠） | 工作集小于几百 KB 量级 |
| L1 miss / L2 hit | 约 6–7 周期 | 工作集在 L2 TLB 覆盖内（几 MB ~ 十几 MB） |
| 两级都 miss | 一次完整页表遍历，架构上 4 次（五级为 5 次）页表内存访问 | 工作集超出 L2 TLB 覆盖 |

注意中间那档：它只需要多花几个周期，看起来无害，但一旦 miss 率从 0.1% 涨到 99%，这 7 周期就会乘在几乎每一次访存上。

### 大页：在 PMD 或 PUD 提前终止遍历

非叶子页表项里的 Page Size（PS）位置起来，表示该级就是叶子：

| PS 位位置 | 页大小 | 对齐要求 |
|---|---|---|
| PMD（第 2 级） | 2 MB | 2 MB 对齐 |
| PUD（第 3 级） | 1 GB | 1 GB 对齐 |

2 MB 页省掉 PTE 那一级，1 GB 页再省掉 PMD 那一级。

### 虚拟化：两层地址转换

Guest Virtual → Guest Physical → Host Physical，Intel 叫 EPT，AMD 叫 NPT/RVI（AMD 自第三代 Opteron "Barcelona" 起支持 RVI，Intel 自 Nehalem 起引入 EPT）。

代价可以精确算出来。ASPLOS 2008 那篇 AMD 论文给出的公式是：若 guest 页表 n 级、nested 页表 m 级，二维遍历需要 **n·m + n + m** 次页表项引用。代入 n = m = 4 得 **24 次**，论文同时指出这比 4 级原生遍历多 6 倍。按同一公式，guest 与 EPT 都用五级时是 35 次。

这个 24 有一手出处（不再是"据说"）。论文也给出了补救措施与实测收益：扩展页表遍历缓存（PWC）带来 15%–38% 的 guest 性能提升，再加 nested TLB 与跳过部分表项引用另加 3%–7%，hypervisor 侧使用大页再省 3%–22%。此外 VPID/ASID 给 TLB 表项打上虚拟机标签，避免每次 VM entry/exit 全刷。

一个容易踩的坑是 **splintering（页粉碎）**：guest 用 2 MB 大页、hypervisor 的 EPT 却用 4 KB 小页映射同一块内存时，TLB 必须按两者中较小的页尺寸工作，2 MB 大页可能退化成需要 512 个 4 KB 表项。**云上或容器里开大页，必须两层同时开才有效。**

---

## 三、演进史：每一步解决了什么，又付出了什么

| 阶段 | 解决什么 | 引入什么代价 |
|---|---|---|
| 单级小 TLB | 让分页可用：把"每次访存查页表"变成一次查表 | 覆盖只有几百 KB ~ 几 MB，随内存增长迅速不够用 |
| 多页尺寸 / 大页 | 不加 TLB 项数就扩大覆盖范围 | 内部碎片、需要 OS 支持；页表格式被硬件固定 |
| 硬件页表遍历 | 免掉软件 TLB refill 的异常开销（几十周期） | 页表格式由架构规定，OS 无法自选；**纯软件管理 TLB（经典 MIPS 风格）被淘汰** |
| 多级 TLB | 容量与延迟解耦：L1 快、L2 大 | 多一级查找延迟（约 +7 周期）与额外面积功耗 |
| SLAT / NPT / EPT | 消除影子页表的 VM-exit 与 VMM 复杂度 | 最坏遍历从 4 次涨到 24 次，靠 PWC / nTLB / 大页压回来 |
| THP（Linux 2.6.38 起） | 应用不改代码就用上大页 | khugepaged 扫描与规整开销、分配停顿、内存膨胀、fork 后 COW 放大 |

关于"纯软件管理 TLB"这一支为什么被淘汰，USENIX 1998 那篇论文给了一个很直观的对照：有的处理器（如 i860）内置硬件去走页表找缺失的映射，而另一些（如 MIPS R10000）则触发异常、由操作系统提供的 TLB miss handler 来填表。异常进出本身就几十周期，还要额外承受 OS 页表数据的缓存未命中；当 TLB miss 率一高，这部分开销立刻成为主导。代价是：硬件遍历把页表格式钉死在架构规范里，操作系统再也不能自选更省空间的结构。

"TLB 又成了瓶颈"这件事，最有说服力的证据来自二十年前。Rice 大学的 superpage 论文统计了 1986–2001 年的工作站，发现 **TLB 覆盖 / 主存容量的比值在十年里下降约 100 倍**，并指出"对很多真实应用，TLB miss 让性能下降多达 30%–60%"，而 1980 年代的实测只有 4%–5%。二十多年后这个趋势并未反转：工作集的增长速度远快于 TLB 项数的增长速度。

厂商的应对是可观测的：AMD 把 Zen 5 的 L2 DTLB 从 Zen 4 的 3072 项提到 4096 项；Intel 在 Panther Lake 的 Cougar Cove 上明确把 TLB 容量做到前代 Lion Cove 的 **1.5 倍**（Intel Tech Tour 2025 的表述是"面向现代工作负载的 1.5 倍容量"，未公布绝对项数）。

段式内存管理为什么没成主流，值得单独说一句：段要求连续物理区间，与分页要做的事高度重叠，而分页粒度更细、碎片更好管理。x86-64 直接在 64 位模式下把 CS/DS/ES/SS 的段基址强制为 0、关掉限长检查，只保留 FS/GS 给线程局部存储用。段式今天只剩"特权级与模式切换"这点作用。

---

## 四、真实处理器对比

> 说明：Intel 各代 TLB 的**完整**参数未在官方手册中系统列出，下表按各来源原文标注；Apple 的 TLB 数字出自 Apple Silicon CPU Optimization Guide，但该指南未在网页上逐项公开，下表数字取自对它的第三方转述与实测交叉验证，**非 Apple 官方网页直接给出**。

| 维度 | Intel | AMD | ARM | Apple |
|---|---|---|---|---|
| 代表核心（年份） | Golden Cove 2021 / Redwood Cove 2023 / Lion Cove 2024 / Cougar Cove 2025 | Zen 4 2022 / Zen 5 2024 | Neoverse V2 2022 | M4 P 核 2024 |
| L1 dTLB | 96 项 6 路（4KB，load）+ 32 项（2MB/4MB）+ 8 项（1GB）+ 16 项 store；Lion Cove 起 4KB load TLB 提到 128 项 | 72 项全相联（Zen 4）→ 96 项全相联（Zen 5） | 48 项全相联 | 160 项（P 核）/ 192 项（E 核） |
| L1 iTLB | 256 项（4KB，Golden Cove）/ 128 项 8 路（Redwood Cove） | 64 项全相联 | 未公开（与数据侧共享 L2） | 未公开 |
| L2 TLB | 2048 项 STLB，指令数据共享 | 3072 项 24 路（Zen 4）→ 4096 项（Zen 5）；L2 iTLB 512 → 2048 | 2048 项 8 路，指令数据共享 | 3072 项（P 核）/ 2048 项（E 核） |
| L2 TLB 附加延迟 | 约 7 周期 | 约 7 周期（Zen 4/5 相同） | 约 6 周期 | 未找到官方数据 |
| 支持页大小 | 4KB / 2MB / 1GB | 4KB / 2MB / 1GB | 4KB / 16KB / 64KB / 2MB / 512MB | 16KB 基本页 |
| 五级页表 | 是（Ice Lake 起，CR4.LA57） | 未找到官方逐代说明 | 不适用：AArch64 靠页粒度改变级数 | 未公开 |

几个可以读出来的分歧：AMD 押的是**堆 L2 TLB 容量**（Zen 5 的 4096 项是四家里最大的）；Intel 的 L2 STLB 还要与指令侧共享，容量上明显吃亏，代价是更容易走到真正的页表遍历；Apple 用的是 16 KB 基本页，**每个表项的覆盖范围是 4 KB 页的 4 倍**——这是"不加项数也能扩大覆盖"的第三条路。

另有一条未获官方网页确认的说法：第三方对 AMD Zen 5 软件优化指南的转述称，Zen 5 的 L1 dTLB 支持把 4 个连续且物理地址对齐的 4 KB 项合并成一个 16 KB 项，等效容量最高可达 384 项。**未找到 AMD 官方网页直接确认，仅作参考。**

### 覆盖范围：真正该看的那张表

| 结构 | 4 KB | 2 MB | 1 GB |
|---|---|---|---|
| 48 项 L1 dTLB（Neoverse V2） | 192 KB | 96 MB | 48 GB |
| 64 项 L1 dTLB | 256 KB | 128 MB | 64 GB |
| 96 项 L1 dTLB（Golden Cove / Zen 5） | 384 KB | 192 MB | 96 GB |
| 160 项 L1 dTLB @16KB 页（Apple M P 核） | 2.5 MB | — | — |
| 2048 项 L2 TLB（Intel / ARM） | 8 MB | 4 GB | 2 TB |
| 3072 项 L2 TLB（Zen 4 / Apple P 核） | 12 MB | 6 GB | 3 TB |
| 4096 项 L2 TLB（Zen 5） | 16 MB | 8 GB | 4 TB |

**2 MB 与 1 GB 两列是理论上限。** 真实核心通常为不同页尺寸准备了独立的小 TLB（Golden Cove 只有 32 项 2MB、8 项 1GB），"96 项全用 1 GB 页"这种好事不会发生。但方向是明确的：页放大一档，覆盖范围跳两个数量级。

---

## 五、延伸知识点

- **512 倍是精确比值，不是修辞。** 每个 TLB 表项覆盖的地址范围放大 512 倍，来自 2MB / 4KB，与 CPU 型号无关。ASPLOS 2008 的原文表述是"2 MB 地址范围用 4 KB 页需要 512 个 TLB 表项"。这是大页收益的全部来源——它让**同一个** TLB 覆盖更多内存，而不是让翻译变快。
- **先量再改。** `perf stat -e dTLB-loads,dTLB-load-misses,iTLB-loads,iTLB-load-misses ./a.out` 是 Linux 上的通用硬件 cache 事件名，多数 x86 与 ARM 机器可用。经验判据：miss 率超过约 1% 就值得考虑大页。注意 STLB 这一级没有统一的通用事件名，Intel 上要用 `perf list` 找对应的原始事件。
- **THP 三档怎么选。** `always` 全系统开（适合大块连续、顺序访问的科学计算与 JVM 堆）；`madvise` 只对显式 `madvise(..., MADV_HUGEPAGE)` 的区域开（通用默认，推荐）；`never` 关闭。内核文档明确提醒：全系统开启时"应用可能 mmap 了一个大区域却只碰了 1 个字节，那样也会分配一个 2M 页"；而 `defrag=always` 会让申请线程**同步做页面回收与内存规整而停顿**。MongoDB 官方文档直接建议关掉 THP，理由正是数据库访问是稀疏而非连续的。
- **`never` 不等于全局禁用。** 内核文档明确指出：把所有 sysfs 控制都设成 never 时，`madvise(..., MADV_COLLAPSE)` 仍会无视这些设置、无条件把区间折叠成 PMD 大小的大页。另外这些设置只影响**未来**的行为，要生效必须重启应用。
- **显式要大页有两条路。** `mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS|MAP_HUGETLB, -1, 0)` 走 hugetlbfs；想要 THP 立刻生效则要求 mmap 区域自然对齐，用 `posix_memalign` 保证（内核文档原文）。
- **容器里的两层转换是真金白银的成本。** 一次 guest TLB miss 最坏 24 次页表引用；若宿主机用 4 KB EPT 页、guest 内部用 2 MB 大页，就会触发 splintering，收益被吃掉。云上开大页必须 guest 与 host 两侧同时对齐，否则可能"开了等于没开"。
- **大页不是万能的。** 随机小块访问时 2 MB 页里只读 64 B，带宽按整页粒度浪费；khugepaged 的扫描与折叠带来周期性 sys 时间尖峰；fork + COW 场景下改 1 字节也要复制 2 MB，这是 Redis 一类负载建议关 THP 的主因之一；1 GB 页在多数核心上只有个位数表项，盲目使用反而更容易冲突失效。

---

## 参考来源

- [Accelerating Two-Dimensional Page Walks for Virtualized Systems（Bhargava et al., ASPLOS 2008, AMD）](https://www.cs.columbia.edu/~cdall/candidacy/pdf/Bhargava2008.pdf) —— 二维遍历 n·m+n+m 公式与 24 次访问、512 倍 TLB 覆盖率、splintering（2008）
- [Intel 5-Level Paging White Paper](https://cdrdv2-public.intel.com/671442/5-level-paging-white-paper.pdf) —— CR4.LA57[bit 12]、57 位线性地址、128 PBytes（2017）
- [x86-64 Page Tables（Linux Kernel Internals）](https://kernel-internals.org/arch/x86/page-tables) —— 四级页表层级与位域、PS 位终止遍历、Linux 4.14 支持 LA57（2024 前后）
- [Linux 内核文档：Transparent Hugepage Support](https://docs.kernel.org/7.1/admin-guide/mm/transhuge.html) —— always/madvise/never 三档、khugepaged、defrag 停顿、内存膨胀、MADV_COLLAPSE 例外（7.1）
- [LWN：Transparent huge pages in 2.6.38](https://lwn.net/Articles/423584/) —— THP 随 Linux 2.6.38 合入（2011）
- [Practical, Transparent Operating System Support for Superpages（Navarro et al., Rice / MPI-SWS）](https://people.mpi-sws.org/~druschel/publications/superpages.pdf) —— TLB 覆盖/主存比值十年下降约 100 倍、TLB miss 致性能下降 30%–60%（2002）
- [General Purpose Operating System Support for Multiple Page Sizes（Ganapathy & Schimmel, USENIX 1998）](https://usenix.org/publications/library/proceedings/usenix98/full_papers/ganapathy/ganapathy_html/ganapathy.html) —— "TLB reach" 的定义与多页尺寸动机（1998）
- [Next Generation "Zen 5" Core（Hot Chips 2024, AMD 官方演讲）](https://hc2024.hotchips.org/assets/program/conference/day2/24_HC2024.AMD.Cohen.Subramony.final.pdf) —— Zen 5 官方 TLB 参数：L1 64 iTLB / 96 dTLB，L2 2K iTLB / 4K dTLB（2024）
- [Chips and Cheese：AMD's Strix Point — Zen 5 Hits Mobile](https://old.chipsandcheese.com/2024/08/10/amds-strix-point-zen-5-hits-mobile/) —— Zen 5 / Zen 4 / Redwood Cove 三级 TLB 逐项对比、L2 TLB +7 周期（2024）
- [Chips and Cheese：Lion Cove — Intel's P-Core Roars](https://chipsandcheese.com/p/lion-cove-intels-p-core-roars) —— Lion Cove 4KB load DTLB 96 → 128 项，其余 TLB 未变（2024）
- [Intel Golden Cove 微架构评测（jiegec / jia.je）](https://jia.je/hardware/2025/01/10/intel-golden-cove/) —— Golden Cove 官方 TLB 结构（96/32/8/16 项）、2048 项 STLB、4 个 page walker、实测 +7 周期（2025）
- [Apple M4 微架构评测（jiegec）](https://jiegec.github.io/hardware/2025/05/21/apple-m4/) —— 转述 Apple Silicon CPU Optimization Guide：M4 P 核 L1 DTLB 160 项、L2 TLB 3072 项、16KB 页，并用指针追逐实测交叉验证（2025）
- [ARM Neoverse V2 微架构评测（jiegec）](https://jiegec.github.io/hardware/2024/11/07/arm-neoverse-v2/) —— 引述 ARM TRM：L1 DTLB 48 项全相联、L2 统一 TLB 2048 项 8 路、+6 周期（2024）
- [Arm Neoverse V2 Core Technical Reference Manual（文档号 102375）](https://developer.arm.com/documentation/102375/latest/) —— Neoverse V2 TLB 结构与支持粒度的官方出处（2023 起）
- [Chips and Cheese：Hot Chips 2023 — Arm's Neoverse V2](https://chipsandcheese.com/i/138977260/backend) —— 48 项 DTLB、192 KB 覆盖（2023）
- [WCCFTech：Intel Panther Lake Deep-Dive（Intel Tech Tour 2025）](https://wccftech.com/intel-panther-lake-deep-dive-18a-compute-tile-cougar-cove-p-cores-darkmont-e-cores/) —— Cougar Cove "TLB 容量为前代 1.5 倍" 的 Intel 官方表述（2025）
- [MongoDB 官方文档：Disable Transparent Huge Pages](https://www.mongodb.com/docs/manual/tutorial/transparent-huge-pages/) —— 数据库因稀疏访问模式建议关闭 THP（2024）
- [Wikipedia：Second Level Address Translation](https://en.wikipedia.org/wiki/Second_Level_Address_Translation) —— AMD RVI 自 Barcelona、Intel EPT 自 Nehalem 的时间线（2024）
- [Linux Kernel Newbies：Linux 4.14](https://kernelnewbies.org/Linux_4.14) —— 五级页表在 4.14 合入、128 PiB 虚拟地址空间（2017）
- [Linux Kernel Internals：Transparent Huge Pages](https://kernel-internals.org/mm/thp) —— khugepaged 扫描折叠机制、madvise 用法、THP 与 hugetlbfs 对比（2024）

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
- [前端与指令缓存：为什么过度展开反而更慢](frontend-and-instruction-cache.html)
- [SIMD 与向量化完全指南：从 SAXPY 到 AVX10 与矩阵扩展](simd-vectorization-complete-guide.html)

所属模块：[TLB 与地址转换](../modules/tlb.html)
