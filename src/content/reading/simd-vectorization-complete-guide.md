---
title: SIMD 与向量化完全指南：从 SAXPY 到 AVX10 与矩阵扩展
module: simd
date: 2026-09-14
order: 15
summary: 同一份 SAXPY 循环，把向量宽度从 128 位加到 512 位，理论算力涨 4 倍，实测耗时却可能一模一样。本文先用 roofline 把这个反直觉变成可手算的判据，再沿 x86 与 ARM 两条主线梳理从 MMX 到 AVX10、AMX、SME 的取舍，最后对比四家的真实实现宽度。
tags: [SIMD, AVX-512, AVX10, SVE, roofline, 向量化]
---
# SIMD 与向量化完全指南：从 SAXPY 到 AVX10 与矩阵扩展

> 向量宽度翻倍，加速比并不翻倍。这篇文章先用一份 SAXPY 实验把这件事变成可以手算的数字，再讲清楚宽度到底由硬件的哪一级决定，最后沿 x86 与 ARM 两条 ISA 主线走到 2025 年的 AVX10、AMX 与 SME。

---

## 一、一个可复现实验：SAXPY 的四档向量宽度

### 实验设计

SAXPY 是 BLAS 里最简单的一级运算，`y[i] = a*x[i] + y[i]`。它是"流式"（streaming）负载的原型：没有数据复用，每个元素读一次、写一次。站点题目 [SAXPY — 解锁自动向量化](../problems/saxpy-auto-vectorize.html) 给的标量版本是：

```c
void saxpy_scalar(float a, const float *restrict x, float *restrict y, int n) {
    for (int i = 0; i < n; ++i)
        y[i] = a * x[i] + y[i];
}
```

下面用 GCC/Clang 的 target attribute 在同一个文件里编出四档实现：

```c
/* SSE: 128 bit = 4 x float */
__attribute__((target("sse")))
void saxpy_sse(float a, const float *restrict x, float *restrict y, int n) {
    for (int i = 0; i + 4 <= n; i += 4) {
        __m128 xv = _mm_loadu_ps(x + i);
        __m128 yv = _mm_loadu_ps(y + i);
        _mm_storeu_ps(y + i, _mm_add_ps(_mm_mul_ps(_mm_set1_ps(a), xv), yv));
    }                                   /* 尾部处理省略 */
}

/* AVX2: 256 bit = 8 x float，用 FMA 合并乘加 */
__attribute__((target("avx2,fma")))
void saxpy_avx2(float a, const float *restrict x, float *restrict y, int n) {
    __m256 av = _mm256_set1_ps(a);
    for (int i = 0; i + 8 <= n; i += 8) {
        __m256 xv = _mm256_loadu_ps(x + i);
        __m256 yv = _mm256_loadu_ps(y + i);
        _mm256_storeu_ps(y + i, _mm256_fmadd_ps(av, xv, yv));
    }
}

/* AVX-512: 512 bit = 16 x float */
__attribute__((target("avx512f")))
void saxpy_avx512(float a, const float *restrict x, float *restrict y, int n) {
    __m512 av = _mm512_set1_ps(a);
    for (int i = 0; i + 16 <= n; i += 16) {
        __m512 xv = _mm512_loadu_ps(x + i);
        __m512 yv = _mm512_loadu_ps(y + i);
        _mm512_storeu_ps(y + i, _mm512_fmadd_ps(av, xv, yv));
    }
}
```

测量要点：进程绑定核心、频率 governor 设为 performance、n 取两档（下文说明）、每档跑 100 次取中位数。

### 理论加速比：4 → 8 → 16

假设核心有 2 条全宽 FMA 流水线（Intel Sapphire Rapids / Golden Cove、AMD Zen 5 都是这个配置，见第四部分），主频 3.0 GHz，一条 FMA 算 2 次浮点运算：

| 实现 | 向量宽度 | 每指令元素数 | FLOP/指令 | 峰值 FLOP/cycle | 峰值 GFLOP/s @3 GHz |
|---|---|---|---|---|---|
| 标量 | 32 bit | 1 | 2 | 4 | 12 |
| SSE | 128 bit | 4 | 8 | 16 | 48 |
| AVX2 | 256 bit | 8 | 16 | 32 | 96 |
| AVX-512 | 512 bit | 16 | 32 | 64 | 192 |

"教科书答案"是 SSE 4×、AVX2 8×、AVX-512 16×。这是**计算上限**，只有在执行单元真的成为瓶颈时才成立。

### 为什么实测达不到：roofline 与算术强度

roofline 模型把可达性能写成两条线的下包络：

```text
可达性能 = min( 峰值计算吞吐, 算术强度 AI × 可用内存带宽 B )
```

SAXPY 的算术强度可以直接数出来。对每个元素：

- 读 `x[i]`：4 字节（必然 miss，流式）
- 读 `y[i]`：4 字节（与写分配 RFO 是同一条 cache line，只算一次）
- 写回 `y[i]`：4 字节（dirty line 淘汰时落盘）
- 浮点运算：1 次乘 + 1 次加 = 2 FLOP

```text
AI(SAXPY) = 2 FLOP / 12 B = 1/6 ≈ 0.167 FLOP/byte
```

这个数字低得惊人。作为对照，寄存器分块的小矩阵乘可以做到 2–4 FLOP/byte（下文算）。把 AI = 1/6 代回 roofline：

| 可用带宽 B | 带宽上限 AI×B | 标量 | SSE | AVX2 | AVX-512 | 瓶颈 |
|---|---|---|---|---|---|---|
| 25 GB/s | 4.2 | 4.2 | 4.2 | 4.2 | 4.2 | 带宽 |
| 50 GB/s | 8.3 | 8.3 | 8.3 | 8.3 | 8.3 | 带宽 |
| 100 GB/s | 16.7 | 12.0 | 16.7 | 16.7 | 16.7 | 标量算、其余带宽 |
| 200 GB/s | 33.3 | 12.0 | 33.3 | 33.3 | 33.3 | 带宽 |
| 400 GB/s | 66.7 | 12.0 | 48.0 | 66.7 | 66.7 | 带宽 |
| 576 GB/s | 96.0 | 12.0 | 48.0 | 96.0 | 96.0 | AVX2 刚好吃满 |

（每格为 `min(计算峰值, 带宽上限)`，单位 GFLOP/s。）

"脊点"（ridge point）——需要多少带宽才能喂满某个宽度——才是这张表最有价值的输出：

| 向量宽度 | 喂满所需内存带宽 |
|---|---|
| 标量 | 72 GB/s |
| SSE (128b) | 288 GB/s |
| AVX2 (256b) | 576 GB/s |
| AVX-512 (512b) | 1152 GB/s |

结论是硬的：**在 3 GHz / 2×FMA 的核心上，要让 SAXPY 跑满 AVX-512 需要 1152 GB/s 内存带宽。**目前单路 CPU 里最宽的服务器平台——12 通道 DDR5-6000 的 AMD EPYC 9005——理论峰值是 12 × 48 GB/s = 576 GB/s（[SUSE 与 AMD 联合发布的 EPYC 9005 调优文档](https://documentation.suse.com/en-us/sbp/tuning-performance/html/SBP-AMD-EPYC-5-SLES15SP6/index.html)，2025-04），刚好只够喂满 AVX2。也就是说，在 SAXPY 这类流式负载上，AVX-512 多出来的那 512 位数据通路在可见未来都是白花的面积与功耗。

### 大 n：带宽接管之后，512 位白给

取 n = 67 108 864（64 M 个 float）。x、y 各 256 MB，合计 512 MB，明确超出任何桌面/服务器的末级缓存，DRAM 带宽完全接管。

- 浮点运算总量：2 × 6.71e7 = 1.34e8 FLOP
- 内存流量：6.71e7 × 12 B = 8.05e8 B ≈ 805 MB

代入 B = 100 GB/s（典型双通道 DDR5 桌面平台的量级；请读者用 `stream` 或 `likwid-bench` 实测自己机器的数字）：

| 实现 | 计算时间 | 带宽时间 (805 MB ÷ 100 GB/s) | 实测 ≈ max | 加速比 |
|---|---|---|---|---|
| 标量 | 11.2 ms | 8.05 ms | **11.2 ms** | 1.00× |
| SSE | 2.80 ms | 8.05 ms | **8.05 ms** | 1.39× |
| AVX2 | 1.40 ms | 8.05 ms | **8.05 ms** | 1.39× |
| AVX-512 | 0.70 ms | 8.05 ms | **8.05 ms** | 1.39× |

**从 SSE 到 AVX-512，向量宽度翻了 4 倍，耗时一模一样。**加速比被卡在 1.39×——这个数字根本不是"SIMD 效率"，而是标量版计算时间与带宽时间的比值（11.2 / 8.05）：你拿到的 1.39× 不是向量化的功劳，而是"标量太慢，慢到刚好贴近带宽墙"。
→ 完整推导见 [缓存层次完全指南](../reading/cache-hierarchy-complete-guide.html)。

这也解释了为什么很多人"加了 `-mavx512f` 没感觉"：不是编译器没生成 512 位指令，是这个负载根本不在计算侧。

### 小 n：驻留 L1 时，宽度是真的有用

把 n 降到 4096（x、y 各 16 KB，合计 32 KB，能装进 48 KB 的 L1D，如 AMD Zen 5 与 Intel Lion Cove）。此时访存不再走 DRAM，瓶颈回到**访存指令条数**：每个元素需要 2 次 load + 1 次 store，每周期能发射的 load/store 条数基本固定（Zen 5 每周期 2 条 512-bit load + 1 条 512-bit store，见 [TechPowerUp Zen 5 架构解析](https://www.techpowerup.com/review/amd-ryzen-7-9700x/2.html)），于是每周期推进的元素数正比于向量宽度：

| 实现 | 每周期元素数（LSU 上限） | 4096 个元素所需周期 | 相对标量 |
|---|---|---|---|
| 标量 | 1 | 4096 | 1× |
| SSE | 4 | 1024 | 4× |
| AVX2 | 8 | 512 | 8× |
| AVX-512 | 16 | 256 | 16× |

注意前提：**LSU 必须能在一周期内吞下整条 512-bit store**。在 Zen 5 这类完整 512-bit 数据通路的核心上成立；在 Zen 4 那种 "double-pumped 256-bit" 实现上，512-bit store 要拆成两次 256-bit 发射，访存指令条数不再下降，AVX-512 相对 AVX2 就只剩寄存器数量和掩码的收益（第二部分详述）。这一段是**推导**，不是厂商公布的数字。

### 反例：寄存器分块的 GEMM

再看算术强度高的负载：把小矩阵乘的输出块留在向量寄存器里累加。设输出块 mr × nr，A、B 从内存流式读入，C 常驻寄存器：

```text
FLOP = 2 · mr · nr · K
字节 = (mr + nr) · K · 4                      (fp32)
AI   = 2·mr·nr / (4·(mr+nr))   FLOP/byte
```

| 实现 | 可用寄存器 | 典型输出块 | AI (FLOP/B) | AI × 100 GB/s | 计算峰值 |
|---|---|---|---|---|---|
| SSE | 16 × 128 bit | 4 × 4 | 1.0 | 100 GFLOP/s | 48 |
| AVX2 | 16 × 256 bit | 8 × 8 | 2.0 | 200 GFLOP/s | 96 |
| AVX-512 | 32 × 512 bit | 16 × 16 | 4.0 | 400 GFLOP/s | 192 |

**三档全部落在计算受限区**，宽度翻倍真正兑现成 2× 实测加速比。而且这里是双重收益：更宽的向量不仅每指令算得更多，还让寄存器装得下更大的输出块，AI 从 1.0 涨到 4.0，把负载从带宽侧推到计算侧。这正是后面"矩阵扩展"一路走下去的动机。
这就是 ILP 的向量版本：更少的指令、更短的迭代间隔，代价是更高的寄存器压力和更长的指令延迟。→ [ILP 与流水线完全指南](../reading/ilp-and-pipeline-complete-guide.html)

（AI 随 tile 边长线性增长，也是 [矩阵乘法 — 缓存分块](../problems/matrix-multiply-tiling.html) 那道题的答案：分块尺寸本质上就是在买算术强度。）

### 判据小结

判断"该用多宽的向量"只需三步：

1. 数出 AI = 浮点运算次数 ÷ 必须从内存搬运的字节数（**写回也算流量**，这是最容易漏的一项）。
2. 算出 ridge 带宽 = 目标宽度的计算峰值 ÷ AI。
3. 实际可用带宽**远低于** ridge 带宽 → 先优化访存（分块、预取、改布局、减少 RFO），加宽向量没有意义；实际带宽**远高于** ridge 带宽 → 加宽向量直接兑现。

---

## 二、硬件全景：向量宽度到底由哪一级决定

### 寄存器堆：从 xmm 到 zmm，从 V 到 Z

x86 侧是"一代一宽度、下层是上层的低半部分"：xmm(128) ⊂ ymm(256) ⊂ zmm(512)。AVX/AVX2 只有 16 个向量寄存器，AVX-512 扩到 32 个并新增 8 个 64-bit 掩码寄存器 k0–k7——Intel AVX10.2 官方规范把这条写成了架构承诺："32 vector registers and eight mask registers at vector lengths 128, 256, and 512"（[Intel AVX10.2 规范 Rev 5.0，2025-06，§3.1.1](https://cdrdv2-public.intel.com/856721/361050-005-intel-avx10.2-spec-jun2025.pdf)）。

ARM 侧的命名完全不同：NEON 用 V0–V31（32 个 128-bit），SVE/SVE2 用 Z0–Z31（32 个可变长）+ 16 个谓词寄存器 P0–P15 + 一个 FFR（First Fault Register），见 [Arm 官方 SVE 介绍](https://developer.arm.com/architectures/scalable-vector-extensions)。SVE 的向量长度在 128 到 2048 bit 之间、必须是 128 的倍数。

寄存器**数量**和宽度一样重要：AVX-512 的 32 × 512 bit = 2 KB 寄存器堆，是 AVX2 的 16 × 256 bit = 512 B 的四倍，这直接决定了寄存器分块能开多大（见上文 GEMM 表格）。

### 执行端口与数据通路宽度

寄存器宽度是 ISA 概念，**真正决定吞吐的是执行单元的数据通路宽度**，两者经常不一致：

| 微架构 | ISA 支持 | 实际数据通路 | 512-bit FMA 吞吐 | 来源 |
|---|---|---|---|---|
| AMD Zen 4 (2022) | AVX-512 | 2 条 256-bit（double-pumped） | 512-bit 指令拆成两个 256-bit 微操 | [Tom's Hardware Zen 5 解析](https://www.tomshardware.com/pc-components/cpus/amd-deep-dives-zen-5-ryzen-9000-and-strix-point-cpu-rdna-35-gpu-and-xdna-2-architectures/4) |
| AMD Zen 5 (2024) | AVX-512 | 完整 512-bit | 每周期 2 条 512-bit FMA | 同上；[AMD 官方博客](https://www.amd.com/en/blogs/2025/leadership-hpc-performance-with-5th-generation-amd.html) |
| Intel Golden Cove / Sapphire Rapids (2023) | AVX-512 | 2 × 512-bit FMA | port0+port1 两个 256-bit 融合成 1 个 512-bit，port5 再挂 1 个 | [Chips and Cheese: Sapphire Rapids](https://old.chipsandcheese.com/2023/03/12/a-peek-at-sapphire-rapids/) |
| Intel Lion Cove (2024) | AVX-512（产品上禁用） | 4 条 256-bit 流水线 | 512-bit 不启用 | [TechPowerUp Arrow Lake 架构](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html)；[HWCooling 对比分析](https://www.hwcooling.net/en/zen-5-amds-most-innovative-core-since-the-original-zen-analysis/4) |

AMD 官方博客把 Zen 4 / Zen 5 的差别写成了公式（FP64）：Zen 4 是 `256b / 64b × (2 pipes × 2 ops) = 16 FLOP/cycle`，Zen 5 是 `512b / 64b × (2 pipes × 2 ops) = 32 FLOP/cycle`——同一个"每周期 2 条 FMA"，但 Zen 4 的 AVX-512 指令要花 2 个周期。**所以在 Zen 4 上编译 AVX-512 代码，FMA 吞吐相对 AVX2 一分不涨**；AVX-512 在 Zen 4 上的真实收益是 32 个寄存器、掩码、以及 VNNI 等新指令，而不是 FLOPs 翻倍。Zen 5 才第一次拿到"每周期 2 条 512-bit FMA"。

### 频率惩罚：AVX-512 的 license 三档

这是真实的、且被 Intel 官方文档承认的现象。Intel Optimization Reference Manual 把 Skylake Server 的 turbo 频率分成三档（[Table 2-12，文档号 248966-050](https://cdrdv2-public.intel.com/814198/248966-Optimization-Reference-Manual-V1-050.pdf)）：

| Level | 类别 | 最高频率 | 触发的指令类型 |
|---|---|---|---|
| L0 | AVX2 light | 最高 | 标量、SSE、AVX128、不含 FP/INT MUL/FMA 的 AVX2 |
| L1 | AVX2 heavy + AVX-512 light | Max AVX2 | 含 FP/INT MUL/FMA 的 AVX2；不含 FP/INT MUL/FMA 的 AVX-512 |
| L2 | AVX-512 heavy | Max AVX-512 | 含 FP/INT MUL/FMA 的 AVX-512 |

手册还给了几条网上很少被提到的硬细节：

- **PMU 事件**：`CORE_POWER.LVL0_TURBO_LICENSE` / `LVL1_TURBO_LICENSE` / `LVL2_TURBO_LICENSE`，可以直接数出你的程序在每一档各跑了多少周期。
- **升档延迟最高 500 μs**：核心请求更高 license 时，PCU 最多要 500 微秒才批，这期间核心只能跑在更低的峰值能力上。
- **回血定时器约 2 ms**：回到高档要等约 2 毫秒，而任何会请求新 license 的条件都会**重置**这个定时器。这是"混合负载里偶尔来一条 512-bit 指令就拖垮全程"的机制根源——不是那条指令慢，是它把后面整段标量代码锁在了低档。
- **跨档阈值有具体例子**：手册写了 Xeon Platinum 8180 在 **65 个周期**的窗口内执行 110 条 AVX-512 light + 20 条 256-bit heavy，就会被从 license 1 推到 license 2。也就是说，256-bit 与 512-bit 混用不会"取加权平均"，混得够密就直接掉到最低档。

关于"到底降多少"，手册只给了档位名称（P0n / P0n-AVX2 / P0n-AVX-512），**没有任何百分比或 MHz 数字**，因为具体频率是 per-SKU 的。第三方实测可以作为量级参考：Travis Downs 在 SKX W-2104（无 turbo，标称 3.2 GHz）上测到的三档是 **L0 3.2 GHz / L1 2.8 GHz / L2 2.4 GHz**；他还发现一次降档要经历约 9 μs 的 "voltage-only" 过渡（此时频率还没变，但 512-bit 指令的 IPC 已经掉到约 1/4）加约 11 μs 的完全停顿，合计约 20 μs——更惊人的是，**单独一条 `vpor zmm0,zmm0,zmm0` 就足以把频率从 3.2 GHz 打到 2.8 GHz**（[Travis Downs, 2020](https://travisdowns.github.io/blog/2020/01/17/avxfreq1.html)，第三方实测，已核对原文）。这正是"轻量指令也算 light 档、但照样触发降档"的直接证据。

> **两个常见错误**：一是把网传的"L1 ≈ 85%、L2 ≈ 60%"当事实——这两个数字来自 Wikipedia 的 AVX 条目，其中 60% 一档原文就带"存疑"标记；而且它们与上面那颗 W-2104 的实测（100% / 87.5% / 75%）差得很远，正因为档位频率是 per-SKU 的。**不要用任何具体百分比做设计依据。**二是把"三档"当成一直成立的事实。

**这段演进本身比"三档是什么"更有信息量**：Skylake Server 三档 → Ice Lake 只剩两级且幅度大幅收窄（第三方整理为约 97%，且只在单核 boost 生效时触发，多核满载不触发）→ Rocket Lake 完全不因向量指令降频 → Alder Lake 起 AVX-512 因 E-core 被禁用，整件事在客户端又不适用了 → Sapphire Rapids 上 Chips and Cheese 实测 512-bit 向量也能跑在 3.8 GHz，"不像有固化的 AVX-512 频率惩罚"。另外 AMD 一侧没有 Intel 这种 license 分档机制，Zen 5 官方称 AVX-512 负载下仍可维持满载频率。（Ice Lake / Rocket Lake 的两档与不降频为第三方整理，未找到 Intel 官方对应表述。）

最后一层平衡很重要：**降频不等于亏。**同一份手册明确指出，频率受限的深度学习负载若以很高比例跑 AVX-512 heavy 指令，相对同样的 AVX2 版本仍能拿到 **1.3×–1.5×** 性能提升；LINPACK 也是"频率下降但性能仍然上升"的典型（手册 Fig 2-8）。所以判断标准只有一个——**端到端时间**，不是频率计数器。

### 掩码寄存器：可变长度向量的条件执行

传统写法里，"向量循环里的 if" 是个两难：要么退化成标量 + SIMD 混合（尾部与边界要写两套），要么算完再 blend（白算但省分支）。掩码机制把条件变成操作数：

```c
/* AVX-512: k1 里的每一位控制一个 lane 是否参与 */
__mmask16 k1 = _mm512_cmp_ps_mask(xv, _mm512_set1_ps(0.0f), _CMP_GT_OQ);
__m512 r = _mm512_mask_fmadd_ps(av, k1, xv, yv);   /* k1=0 的 lane 保持 yv 原值 */
```

SVE 把这个思路推到了极致——**每条** SVE 指令都带谓词，不活跃的 lane 不加载、不计算、不写回，连越界访问都不会触发缺页。于是"尾部处理"这个固定宽度 SIMD 的固有包袱直接消失：

```c
for (uint64_t i = 0; i < n; i += svcntw()) {         /* svcntw() 运行时才知道 */
    svbool_t pg = svwhilelt_b32(i, n);               /* 生成谓词，自动处理尾部 */
    svfloat32_t xv = svld1_f32(pg, x + i);
    svfloat32_t yv = svld1_f32(pg, y + i);
    svst1_f32(pg, y + i, svmad_f32_x(pg, av, xv, yv));
}
```

这正是 [掩码 SAXPY](../problems/masked-saxpy-branchless.html) 那道题的硬件基础。AVX-512 有 8 个掩码寄存器（k0 常被隐式用作"全 1"，不能做谓词目标），SVE 有 16 个 P 寄存器；Chips and Cheese 在 Sapphire Rapids 上实测出约 152 项掩码寄存器重命名条目，说明这块堆料相当足，不太会成为重命名瓶颈。

### Gather / Scatter：不连续访问的代价

gather（按索引向量收集不连续元素）让不规则数据结构也能向量化，但代价远高于连续访问。Google Highway 的官方 FAQ 给了一个很好用的经验值：

> "Platforms that support it typically process one lane per cycle. This can be far slower than normal Load/Store (which can typically handle two or even three entire vectors per cycle)."（[google/highway FAQ](https://github.com/google/highway/blob/2c2cdd144e9aabe16220682445ee9a7a33c5a851/g3doc/faq.md)）

也就是说，**一条 16 lane 的 AVX-512 gather 大约要 16 个周期**，而同样 16 个 float 用连续 load 只要 0.5 个周期——差了 30 倍。更糟的是不同 uarch 实现差异极大：据 uops.info 数据（Stack Overflow 上的整理），`VPGATHERDD ymm` 在 Skylake 上是 5 个 μops / 逆吞吐 5.0 周期，在 Zen 3 上是 39 个 μops / 逆吞吐 8.0 周期——Zen 3 上的 gather 基本退化到与标量持平。还有一个真实案例：LLVM 上有 issue 报告 clang 在 `-O3 -march=native` 下自动生成 gather/scatter，导致某图像解码程序比 `-O2` 慢一倍（[llvm/llvm-project#87640](https://github.com/llvm/llvm-project/issues/87640)）。

判据：**只有当索引本身是向量化算出来的（比如查表、稀疏结构、直方图）且后续计算量足够大时，gather 才划算**；如果 gather 之后每个元素只做一两次运算，还不如直接标量。详见 [不规则聚集](../problems/irregular-gather-prefetch.html)。

### 在整条流水线上的位置

把向量指令放回"取指→译码→执行→访存→retire"：

- **译码**：512-bit 指令编码更长（EVEX 前缀 4 字节），复杂指令（如 gather、部分 permute）会被拆成多个 μop 甚至走微码 sequencer，直接吃前端带宽。
- **重命名**：向量物理寄存器堆按宽度切分。Golden Cove 做了个有趣的取舍——**只为一部分向量寄存器提供 512-bit 重命名能力**，这样在跑标量/AVX2 代码时能腾出更多重命名项，代价是 512-bit 代码的可用重命名资源变少（Chips and Cheese 实测）。
- **调度/执行**：端口数量与数据通路宽度（见上）。
- **访存**：这是最常被忽略的一级。LSU 每周期能发几条 512-bit load/store，往往比 FMA 数量更早成为天花板——SAXPY 就是典型（第一部分的小 n 实验）。
- **retire**：宽向量指令的 retire 带宽通常不是瓶颈，但混合 256/512-bit 代码会触发数据通路模式切换（Sapphire Rapids 上 port0/port1 的 FMA 单元要整体在"2×256"和"1×512"之间配置），混着用不会比纯 512-bit 更快。

---

## 三、演进史：每一步解决了什么、付出了什么

### x86 主线：MMX → SSE → AVX/AVX2 → AVX-512

| 年份 | 扩展 | 宽度 | 解决了什么 | 引入了什么代价 |
|---|---|---|---|---|
| 1997 | MMX | 64 bit | 首次给 x86 整数 SIMD | 寄存器**别名在 x87 堆栈**上，MMX 与浮点切换要清空状态，代价极高；只支持整数 |
| 1999 | SSE | 128 bit | 独立 xmm 寄存器 + 单精度浮点 | 只支持单精度；需要 OS 支持 XSAVE 才能保存新上下文 |
| 2001 | SSE2 | 128 bit | 双精度 + 128-bit 整数，成为 x86-64 基线 | 寄存器仍只有 8 个（x86-64 扩到 16） |
| 2011 | AVX | 256 bit | ymm 寄存器；VEX 编码带来**三操作数非破坏性**目标 | 更宽的上下文保存；频率惩罚开始成为话题 |
| 2013 | AVX2 | 256 bit | 256-bit 整数、FMA3、首次引入 gather | 执行单元面积与功耗显著上升 |
| 2016/2017 | AVX-512 | 512 bit | zmm × 32、k0–k7 掩码、EVEX 编码、内嵌舍入/异常抑制、scatter | 20 个独立 CPUID feature flag 造成**严重的子集碎片化**；功耗/面积/频率问题 |

注意 AVX-512 首次亮相是 2016 的 Knights Landing（Xeon Phi），真正进入主流服务器是 2017 的 Skylake-SP/Skylake-X。从 2016 到 2023 这七年里，"某个 CPU 支持 AVX-512"这句话几乎没有信息量——它支持的可能是 F/CD/ER/PF 中的任意子集。

### Alder Lake 与 AVX-512 的"启用—禁用"反复

这是近年最值得记的一段历史。2021 年 Alder Lake 引入 Golden Cove P-core + Gracemont E-core 的混合架构：P-core 有完整的 512-bit FMA 硬件，E-core 完全没有。x86 生态长期假设"CPUID 对所有核一致"，于是只能上报交集——AVX2。Intel 的做法是先让 BIOS/微码不枚举 AVX-512，随后在 2022 年初直接**在硅片上熔断（fusing off）**，堵死"关掉 E-core 就能开 AVX-512"这条路（[Tom's Hardware 当时的报道](https://www.tomshardware.com/features/intel-architecture-day-2021-intel-unveils-alder-lake-golden-cove-and-gracemont-cores/2)；[PCGamer 后续报道](https://www.pcgamer.com/intel-kills-alder-lake-avx-512-support-for-good)）。Raptor Lake 同样处理。

到 2024 年的 Arrow Lake / Lunar Lake，Lion Cove P-core 依然保留 512-bit 执行单元，Skymont E-core 的向量引擎是 4 条 128-bit（[TechPowerUp](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html)），AVX-512 仍然因异构而在产品级被禁用。**同一时期 AMD 从 Zen 4（2022）起在所有核心上一致支持 AVX-512**——这是 x86 生态里一次罕见的角色反转。

### AVX10：Intel 的统一方案（2023 起）

2023 年 7 月 Intel 公布 AVX10，目标很直接：**用一个版本号取代 AVX-512 那 20 个 feature flag，并让 P-core 与 E-core 拥有同一套向量 ISA。**

- **AVX10.1**：纯过渡版本，"only enumerates the Intel AVX-512 instruction set at 128, 256, and 512 bits"，首发于 Granite Rapids（Xeon 6 P-core），用于软件提前适配（官方规范 §3.1）。
- **AVX10.2**："the initial, fully-featured version ... available across both client and server product lines"。新增内容按官方规范 §3.1.4 是六类：AI 数据类型与转换（含 **FP8**，E5M2 / E4M3，遵循 OCP OFP8 规范）、媒体加速（VMPSADBW 扩到 512-bit、16-bit VNNI 全符号组合）、IEEE-754-2019 的 min/max、饱和转换、零扩展的部分向量拷贝、FP 标量比较增强。
- **规范版本**：官方《Intel Advanced Vector Extensions 10.2 Architecture Specification》文档号 361050，**Rev 5.0，2025 年 6 月**（PDF 首页明确 "June, 2025 / Revision 5.0"）。
- **枚举方式**：`CPUID.(EAX=07H,ECX=01H):EDX[bit 19]` 表示支持，版本号在 `CPUID.(EAX=24H,ECX=00H):EBX[7:0]`。
- **一次重要反转**：早期规范里有 VL128/VL256/VL512 三个枚举位（即"E-core 只到 256-bit"方案）。Rev 5.0 的脚注明确写着这三个位已改为保留，且"**All processors supporting Intel AVX10 will include support for all vector lengths**"。这与 2025 年 3 月 Intel 更新 AVX10 白皮书 3.0 版时的说法一致：所有平台（含 E-core）都将支持 512-bit，编译器不再需要 `avx10.x-256/512` 选项（[Phoronix 报道，2025-03](https://www.phoronix.com/news/Intel-AVX10-Drops-256-Bit)，引用 Intel 白皮书原文与 GCC 补丁说明）。**这是"被淘汰的思路"的又一个例子：Intel 用了两年才放弃 256-bit-only E-core 方案。**
- **兼容性**：任何枚举 AVX10 的处理器也会同时枚举 AVX / AVX2 / AVX-512，老二进制不受影响；但 AVX-512 ISA 就此冻结，新特性只加在 AVX10 上。

### ARM 主线：NEON → SVE/SVE2 → SME

NEON 是固定 128-bit（Armv7/8），简单高效但**宽度写死在 ISA 里**——想变宽就要换一套指令。SVE（Armv8.2，2016 公布）的破局点是"向量长度由实现决定、由运行时查询"：合法实现为 128/256/512/1024/2048 bit（[Arm 官方](https://developer.arm.com/architectures/scalable-vector-extensions)）。同一个二进制可以在 128-bit 的 Neoverse V2 上跑，也可以在未来 2048-bit 的机器上跑，不需要重编。代价是软件模型大改：编译期不知道宽度，所有循环必须写成"谓词驱动 + `svcntb()` 取运行时长度"。

SVE2（Armv9）在 SVE 之上补齐了 NEON 覆盖的领域（密码、多媒体、压缩/解压），让 NEON 和 SVE 合流。SME（Scalable Matrix Extension，Armv9）在 SVE 之上加了二维的 ZA tile 存储和外积累加指令 `FMOPA`，并引入 streaming SVE 模式（`smstart`/`smstop`）。

### 矩阵扩展：三个都叫 AMX / SME，但完全不是一回事

这是本节最需要读者小心的地方。

| 名称 | 厂商 | 性质 | 关键参数 |
|---|---|---|---|
| **Intel AMX** | Intel | 公开 ISA，文档齐全 | 8 个 tile（TMM0–TMM7），每个 16 行 × 64 字节 = 1 KiB，共 8 KiB；TMUL 单元支持 BF16 / INT8（后续 AMX-FP16、AMX-COMPLEX）；第四代至强单核每周期 1024 次 BF16 或 2048 次 INT8 运算（[Intel 第四代至强 AI 调优指南](https://www.intel.co.za/content/www/us/en/developer/articles/technical/tuning-guide-for-ai-on-the-4th-generation.html)） |
| **ARM SME** | Arm | 公开 ISA（Armv9） | ZA 二维存储 = SVL × SVL 字节；外积 `FMOPA`；可选 streaming 模式；SME2 加多向量指令 |
| **Apple AMX** | Apple | **完全未公开、无文档** | 第三方逆向得到的形态：X/Y/Z 三个寄存器堆，一个 32×32 的 MAC 网格（每个单元做 16-bit MAC，2×2 子网格做 32-bit MAC，4×4 做 64-bit）；单条指令可完成一个 X 寄存器与一个 Y 寄存器的完整外积并累加到 Z（[corsix/amx 逆向工程仓库](https://github.com/corsix/amx)，**第三方实测/逆向，非官方数字**） |

**Apple 的 AMX 与 Intel 的 AMX 同名纯属巧合，架构、寄存器模型、指令编码、编程接口全都不一样**——连上游那个仓库都在 README 里专门写了这句提醒。Apple 的 AMX 只能通过 Accelerate 等高层框架间接使用（或者用逆向出来的内联汇编，风险自负）。

矩阵扩展的共同点是**继续买算术强度**：AVX-512 的 16×16 输出块是 4 FLOP/B，Intel AMX 的一个 16×32×16（BF16）tile 操作是 16384 FLOP / 2048 B = 8 FLOP/B，翻了一倍。SIMD 走到头之后，这就是唯一的路。

---

## 四、真实处理器对比（2024–2026）

### 各家当前实现

| 维度 | Intel | AMD | ARM | Apple |
|---|---|---|---|---|
| 当前代表性的 P-core | Lion Cove（Arrow Lake / Lunar Lake，2024） | Zen 5（2024） | Neoverse V3 / V2 | M4（2024） |
| 最高 SIMD 宽度（可用） | **AVX2（256-bit）**；512-bit 硬件存在但产品级禁用 | **AVX-512（512-bit）**，完整 512-bit 数据通路 | SVE2，实现长度 **128-bit** | NEON 128-bit（+ SME，SVL=512） |
| FP32 峰值（FLOP/cycle/核） | 32（2×256-bit FMA） | **64**（2×512-bit FMA） | 32（4×128-bit） | 32（4×128-bit） |
| 矩阵扩展 | Intel AMX（服务器 Sapphire Rapids / Granite Rapids 起） | 无（消费级/EPYC 通用核均未见公开支持） | ARM SME / SME2（视实现） | Apple AMX（未公开）+ SME（SVL=512） |
| 掩码/谓词 | k0–k7（AVX-512，产品上禁用） | k0–k7 | P0–P15 | 无 SVE 谓词；SME 有 P0–P15 |

数字来源与年份：Intel Lion Cove 执行单元见 [TechPowerUp Arrow Lake 架构页](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html)（2024）；Zen 5 每周期 FP64 32 FLOP 见 [AMD 官方博客](https://www.amd.com/en/blogs/2025/leadership-hpc-performance-with-5th-generation-amd.html)（2025），FP32 为其两倍；Neoverse V2 的"4×128-bit 功能单元"见 [NVIDIA Grace 调优指南](https://docs.nvidia.com/grace-perf-tuning-guide/index.html)；Apple M4 NEON 4×128-bit 与 SME 见 [corsix/amx（逆向，非官方）](https://github.com/corsix/amx) 与 [arXiv 2512.21473（Apple M4 Pro 上 SVL=512、ZA=4096 字节实测）](https://arxiv.org/abs/2512.21473)。

### Intel：哪些型号有 AVX-512，哪些被阉割

| 世代 | P-core | E-core | AVX-512 是否可用 | 说明 |
|---|---|---|---|---|
| Skylake-X / Skylake-SP (2017) | Skylake | 无 | 是 | 2×512-bit FMA |
| Ice Lake (2019) / Tiger Lake (2020) / Rocket Lake (2021) | Sunny Cove / Willow Cove / Cypress Cove | 无 | 是 | 全核一致，无异构问题 |
| Alder Lake (2021) / Raptor Lake (2022) | Golden Cove / Raptor Cove | Gracemont | 否（后期硅片熔断） | E-core 不支持，CPUID 不上报（[Tom's Hardware](https://www.tomshardware.com/features/intel-architecture-day-2021-intel-unveils-alder-lake-golden-cove-and-gracemont-cores/2)） |
| Sapphire Rapids (2023) | Golden Cove | 无 | 是 | 2×512-bit FMA + AMX |
| Granite Rapids (2024) | Redwood Cove | 无 | 是（枚举 AVX10.1/512） | 首个支持 AVX10 的产品 |
| Arrow Lake / Lunar Lake (2024) | Lion Cove | Skymont（4×128-bit） | 否 | 同 Alder Lake 逻辑 |
| Panther Lake (2025–2026) | Cougar Cove | Darkmont | **存疑** | 有第三方依据 Intel《Architecture Instruction Set Extensions Programming Reference》(2025-09) 指出 Panther Lake 不支持 AVX10.1/10.2，但未找到 Intel 官方页面直接确认，此处存疑 |

### AMD：Zen 4 与 Zen 5 的 512-bit 差别

Zen 4（2022）是"用 256-bit 数据通路跑两次来模拟 512-bit"——官方说法是 dual-pumped，AMD 明确这是为了避免 AVX-512 带来的频率大幅波动（[HotHardware Zen 5 解析](https://hothardware.com/reviews/amd-ryzen-ai-zen-5-architecture-overview)）。Zen 5（2024）换成完整 512-bit 数据通路，官方称在 AVX-512 负载下**仍可维持多核满载频率**，与 Intel 的实现形成对比（[Tom's Hardware](https://www.tomshardware.com/pc-components/cpus/amd-deep-dives-zen-5-ryzen-9000-and-strix-point-cpu-rdna-35-gpu-and-xdna-2-architectures/4)）。Zen 5 同时把 FADD 延迟从 3 周期降到 2，L1D 从 32 KB / 8 路扩到 48 KB / 12 路，并把 L1↔FPU 带宽翻倍——这些都是在为 512-bit 通路供数。

注意两点：Zen 5 的向量单元是**模块化**的，APU / 移动端（Strix Point）仍保留 256-bit 版本；Zen 5c 与 Zen 5 共享 ISA。

### ARM：ISA 到 2048 位，实现停在 128 位的落差

这是理解 ARM 服务器最容易踩的坑。SVE2 的 ISA 允许 128–2048 bit，但**主流服务器的实际实现都是 128 位**：

- Neoverse V2（Grace、Graviton4）：Arm 官方 TRM 明写 "The Neoverse V2 core implements a scalable vector length of 128 bits"；NVIDIA 的描述是 "in a 4x128-bit configuration"。
- Neoverse V3：Arm 官方 TRM 同样是 "Implementation of the Scalable Vector Extension (SVE) with a 128-bit vector length"（[Arm Neoverse V3 TRM](https://developer.arm.com/documentation/107734/0002/The-Neoverse--V3--core/Neoverse--V3--core-features?lang=en)）。
- Neoverse V1 是 256-bit（AWS Graviton3），属于少见的宽实现。

**所以"用 SVE2 重写一遍"在 Neoverse V2/V3 上不会带来宽度红利**——位宽和 NEON 一样是 128。SVE2 的真实收益在别处：谓词（免尾部循环）、更丰富的指令（`svdot`、gather/scatter、bitperm、SHA3）、以及"同一个二进制未来能在更宽的机器上运行"的可移植性。这与 AVX-512 的卖点（更宽）完全不同，值得强调。

### Apple：NEON + SME + 那个不公开的 AMX

Apple M 系列的 CPU 侧有三层：

1. **NEON**：128-bit，每周期可发射 4 条（Firestorm/M1 上的实测值，第三方逆向）。就纯 SIMD 宽度而言，Apple 是四家里最窄的——只有 AMD Zen 5 的 1/4。
2. **Apple AMX**：CPU 侧紧耦合的矩阵协处理器，未公开。第三方逆向显示 M1→M2 增加 BF16 支持，M2→M3 给 `ldx`/`ldy`/`matint` 各加一种模式，M3→M4 调整了 `extrh`/`extrv`/`vecfp`/`vecint` 的低位处理。
3. **SME**：M4 实现了 SME/SME2（但**没有实现普通 SVE**），streaming 向量长度 SVL = 512 bit，ZA 存储 4096 字节。第三方在 M4 Pro 上用 Eigen 的 SME2 GEMM kernel 实测到相对 NEON 的 **10–13 倍**加速（[Eigen MR 2128](https://gitlab.com/libeigen/eigen/-/merge_requests/2128)，第三方实测）。

即 Apple 的路线是"窄 SIMD + 大矩阵单元"，而不是把 SIMD 做宽。这与 Intel AMX / ARM SME 是同一个判断：AI 负载要用二维 tile 提高算术强度，而不是继续拉长向量。

---

## 五、延伸知识点

1. **向量宽度翻倍 ≠ 加速比翻倍。**先算 AI，再算 ridge 带宽 = 计算峰值 ÷ AI。SAXPY 的 AI 是 1/6 FLOP/B，需要 576 GB/s 才能让 AVX2 跑满、1152 GB/s 才能让 AVX-512 跑满——后者超出目前任何单路 CPU。**加宽向量只在计算受限区有效。**

2. **判断带宽瓶颈还是计算瓶颈，最快的方法是改工作集大小。**把 n 缩小到数据能进 L1（比如 32 KB）再测一次：如果时间大幅下降，说明原负载在访存侧；如果基本不变，说明在计算侧。配合 `perf stat -e` 看 `CORE_POWER.LVL2_TURBO_LICENSE`（Intel）或 L1/L2 miss 率更直接。
MLP 决定你能否榨干剩余带宽，诊断方法见 [内存层次与延迟隐藏](../reading/memory-hierarchy-latency-hiding.html)。

3. **gather 什么时候还不如标量。**经验法则是"每周期约 1 个 lane"，而连续 load 每周期能吞 2–3 个完整向量，差距可达 30 倍。Zen 3 上 `VPGATHERDD ymm` 是 39 个 μops / 逆吞吐 8 周期，基本和标量持平。判据：gather 之后每个元素如果只做一两次运算，不值得；clang 在 `-O3 -march=native` 下自动生成的 gather 甚至可能让程序慢一倍。

4. **掩码寄存器取代的是"分支 + blend"，而不只是分支。**AVX-512 的 k0–k7 与 SVE 的 P0–P15 把条件变成操作数，SVE 更进一步——不活跃 lane 连内存都不访问，越界也不会缺页，于是尾部循环彻底消失。这是 [掩码 SAXPY](../problems/masked-saxpy-branchless.html) 的硬件根据，也是 SVE 与固定宽度 SIMD 最本质的区别之一。

5. **`-mprefer-vector-width=256` 的真实作用。**它只约束**编译器自动向量化时选用的最大向量宽度**，让 GCC/Clang 把 8×32-bit 的循环用两条 ymm 而不是一条 zmm 来实现。它不影响手写 intrinsic 生成的指令，也不"关闭"CPU 的 512-bit 单元——它只是少生成 512-bit 指令，从而**顺便**避开 L2 license 降频。如果你的负载是混合型的（大部分标量、偶尔一小段向量），256 位往往是更优解：降频是全局的，会拖累同一核上的标量代码。

6. **AVX10 对现有代码的意义。**版本号枚举（一个 CPUID bit + 一个版本号）取代了 AVX-512 那二十来个 feature flag，"支持 AVX10.2"是一句有确定含义的话。对开发者：编译选项从 `-mavx512f -mavx512bw -mavx512vl -mavx512dq ...` 收敛成一个 `avx10.2`；对生态：AVX-512 ISA 冻结、新特性只加在 AVX10 上、且未来所有核心（含 E-core）都是 512-bit——Alder Lake 那场"有没有 AVX-512"的闹剧理论上不会再重演。

7. **动手查你的 CPU 到底支持什么。**Linux：`grep -o -w 'avx512f\|avx10_1\|avx10_2\|amx_bf16\|amx_tile' /proc/cpuinfo | sort -u`；AArch64 用 `getauxval(AT_HWCAP)` 查 `HWCAP_SVE` / `HWCAP2_SME`，SVE 的**实际**向量长度要用 `prctl(PR_SVE_GET_VL)` 读（ISA 允许到 2048，实现可能只有 128，别猜）。macOS / Windows 可以借助跨平台的 `cpuinfo` 库（Python 的 `py-cpuinfo`、C/C++ 的 `pytorch/cpuinfo`）。Apple 的 AMX 与 SME 没有官方查询接口（Apple 不公开 AMX 文档），第三方工具只能靠逆向探测——**这类数字请一律当作"第三方实测，非官方"来引用**。

---

## 参考来源

- [Intel Advanced Vector Extensions 10.2 Architecture Specification, Rev 5.0, 文档号 361050-005（2025-06）](https://cdrdv2-public.intel.com/856721/361050-005-intel-avx10.2-spec-jun2025.pdf) —— AVX10 的收敛目标、32 向量寄存器+8 掩码寄存器、AVX10.1 首发于 Granite Rapids、AVX10.2 新增 FP8 等六类指令、CPUID 枚举方式、"所有 AVX10 处理器支持全部向量长度"（官方规范，一手来源）
- [Intel 64 and IA-32 Architectures Optimization Reference Manual Vol.1, 文档号 248966-050](https://cdrdv2-public.intel.com/814198/248966-Optimization-Reference-Manual-V1-050.pdf) —— AVX-512 频率惩罚的 L0/L1/L2 三档结构、触发指令分类、PMU 事件名、500 μs 升档延迟与 2 ms 回血定时器、Xeon 8180 的 65 周期跨档阈值、以及"重度 AVX-512 的 DL 负载相对 AVX2 仍有 1.3–1.5× 提升"的官方结论（官方手册，Table 2-12 / Fig 2-8）
- [Travis Downs：Gathering Intel on Intel AVX-512 Transitions（2020-01）](https://travisdowns.github.io/blog/2020/01/17/avxfreq1.html) —— SKX W-2104 上实测三档频率 3.2 / 2.8 / 2.4 GHz、一次降档约 9 μs 升压 + 11 μs 停顿（第三方实测）
- [AMD：5th Gen EPYC 的 HPC 性能（2025）](https://www.amd.com/en/blogs/2025/leadership-hpc-performance-with-5th-generation-amd.html) —— Zen 4 = 16 FLOP/cycle（FP64，AVX-512 需 2 周期）、Zen 5 = 32 FLOP/cycle（官方博客，一手来源）
- [Tom's Hardware：AMD Zen 5 架构解析（2024）](https://www.tomshardware.com/pc-components/cpus/amd-deep-dives-zen-5-ryzen-9000-and-strix-point-cpu-rdna-35-gpu-and-xdna-2-architectures/4) —— Zen 4 "double-pumped" 256-bit vs Zen 5 完整 512-bit 数据通路、Zen 5 满载不降频
- [TechPowerUp：AMD Ryzen 7 9700X 架构页（2024）](https://www.techpowerup.com/review/amd-ryzen-7-9700x/2.html) —— Zen 5 每周期 2 条 512-bit load + 1 条 512-bit store、48 KB L1D、2 周期 FADD
- [TechPowerUp：Intel Core Ultra 9 285K 架构页（2024）](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html) —— Lion Cove 向量引擎（4 SIMD ALU + 2 FMA）、Skymont 向量引擎为 4 条 128-bit FPU
- [Chips and Cheese：Sapphire Rapids 微架构实测（2023）](https://old.chipsandcheese.com/2023/03/12/a-peek-at-sapphire-rapids/) —— 2×512-bit FMA 的端口构成、掩码寄存器重命名条目、未见固化 AVX-512 频率惩罚
- [Tom's Hardware：Alder Lake 架构日报道（2021）](https://www.tomshardware.com/features/intel-architecture-day-2021-intel-unveils-alder-lake-golden-cove-and-gracemont-cores/2) —— AVX-512 因 E-core 不支持而被熔断、服务器 Golden Cove 有 2×512-bit FMA
- [Arm：SVE 架构官方介绍](https://developer.arm.com/architectures/scalable-vector-extensions) —— SVE 向量长度 128–2048 bit（128 为粒度）、Z0–Z31 与 P0–P15、免重编译的可移植性
- [Arm Neoverse V3 Core Technical Reference Manual（2023–2024）](https://developer.arm.com/documentation/107734/0002/The-Neoverse--V3--core/Neoverse--V3--core-features?lang=en) —— Neoverse V3 的 SVE 实现向量长度为 128 bit（官方 TRM）
- [NVIDIA Grace Performance Tuning Guide](https://docs.nvidia.com/grace-perf-tuning-guide/index.html) —— Neoverse V2 以 "4x128-bit" 配置实现 SVE2 与 NEON
- [Intel：第四代至强可扩展处理器 AI 调优指南](https://www.intel.co.za/content/www/us/en/developer/articles/technical/tuning-guide-for-ai-on-the-4th-generation.html) —— Intel AMX 的 8 个 tile（16 行 × 64 字节 = 1 KiB）、TMUL 单元、BF16/INT8 支持（官方文档）
- [corsix/amx：Apple AMX 指令集逆向工程仓库](https://github.com/corsix/amx) —— Apple AMX 的 X/Y/Z 寄存器堆与 32×32 MAC 网格、各代差异；**明确指出 Apple AMX 与 Intel AMX 完全不同**（第三方逆向，非官方）
- [arXiv 2512.21473《Demystifying ARM SME to Optimize General Matrix Multiplications》（2025-12）](https://arxiv.org/abs/2512.21473) —— Apple M4 Pro 上 SME 的 SVL = 512 bit、ZA = 4096 字节、FMOPA 外积与 tile 语义
- [Eigen MR 2128：ARM SME2 GEMM backend](https://gitlab.com/libeigen/eigen/-/merge_requests/2128) —— Apple M4 Pro 上 SME2 GEMM 相对 NEON 实测 10–13 倍加速（第三方实测）
- [google/highway FAQ](https://github.com/google/highway/blob/2c2cdd144e9aabe16220682445ee9a7a33c5a851/g3doc/faq.md) —— gather/scatter "每周期约 1 个 lane" 的经验值、早期 Intel 降频的实测幅度
- [LLVM issue #87640](https://github.com/llvm/llvm-project/issues/87640) —— clang 自动生成 gather/scatter 导致程序慢一倍的真实案例
- [SUSE / AMD：Optimizing Linux for AMD EPYC 9005（2025-04）](https://documentation.suse.com/en-us/sbp/tuning-performance/html/SBP-AMD-EPYC-5-SLES15SP6/index.html) —— EPYC 9005 单路 12 内存通道、DDR5-6000、单通道峰值 48 GB/s（即 576 GB/s 理论总带宽）
- [HWCooling：Zen 5 深度解析（2024）](https://www.hwcooling.net/en/zen-5-amds-most-innovative-core-since-the-original-zen-analysis/4) —— 各家 SIMD 流水线宽度横向对比（Lion Cove 4×256-bit、Apple 4×128-bit、Cortex-X925 6×128-bit）
- [Phoronix：Intel AVX10 Drops Optional 512-bit（2025-03-19）](https://www.phoronix.com/news/Intel-AVX10-Drops-256-Bit) —— AVX10 白皮书 3.0 修订放弃 256-bit-only 方案、GCC/LLVM 补丁原文（"all the future platforms will now support 512 bit vector width, including P-core and E-core"）
- [PCGamer：Intel kills Alder Lake AVX-512 support for good（2022）](https://www.pcgamer.com/intel-kills-alder-lake-avx-512-support-for-good) —— Alder Lake 后期批次硅片熔断 AVX-512

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
- [TLB 与地址转换：从一次聚集重排说起](tlb-address-translation-guide.html)

所属模块：[SIMD 与向量化](../modules/simd.html)
