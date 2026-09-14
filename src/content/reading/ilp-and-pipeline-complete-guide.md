---
title: ILP 与流水线完全指南：从八累加器点积说起
module: ilp
date: 2026-09-14
order: 11
summary: 把一个累加器拆成八个，理论上是 8 倍加速，实测往往只有 2 到 3 倍。本文从这道点积题出发，推导流水线宽度、端口数、物理寄存器如何一层层压住指令级并行，再横向对比 Intel、AMD、ARM、Apple 当代核心的真实参数。
tags: [ILP, 流水线, 超标量, 寄存器重命名, Tomasulo, 依赖链]
---
# ILP 与流水线完全指南：从八累加器点积说起

> 依赖链决定了 IPC 的下界，端口和宽度决定了上界。本文推导这三条答案在同一个算法上如何互相打架，并把流水线这半个主题补全：IF/ID/EX/MEM/WB 之后，现代核心到底加了几级、每级在干什么。

---

## 一、现象：八个累加器为什么跑不满八倍

### 1.1 单累加器：一条依赖链就锁死了下界

以本站 [点积 — 八累加器](../problems/dot-product-eight-accumulators.html) 为例，最朴素的写法是：

```c
double dot(const double *a, const double *b, int n) {
    double s = 0.0;
    for (int i = 0; i < n; i++)
        s += a[i] * b[i];       // ← s → s → s，一条串行链
    return s;
}
```

关键点在于：乘法的结果不在这条链上（`a[i]*b[i]` 可以提前算好），真正构成循环迭代间依赖的只有那一次浮点加法。设一次 FP 加法的延迟为 **L** 个周期，那么下一次加法必须等到上一次加法的结果，迭代间的**循环进位依赖（loop-carried dependency）**长度就是 L：

```text
每元素耗时 = L 周期        加法 IPC = 1 / L
```

这个 L 不是常数，随微架构变化。下面是真实测量值（数据来自 [uops.info](https://uops.info/html-instr/ADDSD_XMM_XMM.html) 的 `ADDSD xmm, xmm` 条目）：

| 微架构 | FP 加法延迟 L（周期） | 倒数吞吐 TP（周期/条） | 可同时执行的 FP 加端口数 P = 1/TP |
|---|---|---|---|
| Intel Skylake | 4 | 0.50 | 2 |
| Intel Golden Cove | 2 | 0.50 | 2 |
| AMD Zen 4 | 3 | 0.50 | 2 |
| AMD Zen 5 | 2 | 0.50 | 2 |

取 Skylake 的 L = 4 —— 这是教科书里那个"浮点加法四周期"的经典数字 —— 单累加器版本对 n 个元素的耗时就是 **4n 个周期**，加法 IPC 只有 0.25。这就是典型的"延迟受限（latency bound）"：流水线里同时只有一条链在被推进，其余所有功能单元都在空转。

### 1.2 k 条链：理论 k× 加速和它的三条硬上限

拆成 k 个累加器之后，链条变成 k 条互不相关的链：

```c
double s0 = 0, s1 = 0, s2 = 0, s3 = 0, s4 = 0, s5 = 0, s6 = 0, s7 = 0;
for (int i = 0; i < n; i += 8) {
    s0 += a[i+0] * b[i+0];
    s1 += a[i+1] * b[i+1];
    /* ... s2 ~ s7 ... */
}
return (s0 + s1) + ((s2 + s3) + (s4 + s5)) + s7;   // 尾部归约
```

现在每次迭代产生 k 个独立的加法，它们可以同时占用不同的流水线通路的"执行"阶段。此时的加法吞吐上界是三层 constraints 的最小值：

```text
加法 IPC ≤ min( k / L ,  P ,  W )
            ↑         ↑    ↑
        依赖链上界   端口上界  宽度上界
```

三层分别对应：

- **依赖链上界 k / L**：k 条链各自每 L 周期吐出一个结果。
- **端口上界 P**：多数核心只有 2 到 3 个能干 FP 加法的端口，每周期最多完成 P 条。
- **宽度上界 W**：每周期能重命名/发射的总 uop 数（现代大核 6 到 10 不等）。

于是"收益拐点"可以直接算出来：当 `k / L ≥ P` 之后再增加 k 毫无意义，即

```text
k* = L × P
```

代入上表的实测数字：

| 微架构 | L | P | 收益拐点 k* | 说明 |
|---|---|---|---|---|
| Intel Skylake | 4 | 2 | **8** | 站点用八个累加器，正好对应这一代 4 周期加法 |
| AMD Zen 4 | 3 | 2 | **6** | 六条链之后再加没有收益 |
| Intel Golden Cove | 2 | 2 | **4** | 加法延迟砍到 2，四个累加器就饱和 |
| AMD Zen 5 | 2 | 2 | **4** | AMD 官方确认桌面版 Zen 5 的 FADD 从 Zen 4 的 3 周期降到 2 周期（[Tom's Hardware 引 AMD Hot Chips 2024](https://www.tomshardware.com/pc-components/cpus/amd-deep-dives-zen-5-ryzen-9000-and-strix-point-cpu-rdna-3-5-gpu-and-xdna-2-architectures/4)） |

Skylake 上 k = 8 时 `k / L = 8/4 = 2.0`，恰好等于端口上界 P = 2 —— 这就是"八累加器时 IPC 上限 2.0"这句话的完整含义：**它是"每周期完成的浮点加法数"的上限，不是总指令 IPC，而且它成立的前提是 L = 4。**

如果换到 Zen 5，同样的八累加器代码 `8/2 = 4` 远超 P = 2，多出来的四条链纯粹是浪费；只需要 4 个累加器就能喂满两个 FP 加端口。手写展开的"魔法常数"是跟着实现跑的，这一点后面还会再提。

### 1.3 为什么实测还是到不了 2.0

把上面那条公式套到真实循环里还不够，因为一个元素除了加法还有别的活。标量 `double` 点积在每元素上大致需要：

| 每元素的 uop | 要占用的资源 |
|---|---|
| 2 × load | load AGU 端口 + load 队列 + L1D 读口 |
| 1 × mul | FMA 端口（Skylake 上与 add 共用 p0/p1） |
| 1 × add | FP add 端口 |
| 约 0.25 ~ 0.5 × 循环控制（展开后摊薄） | ALU / 分支端口 |

在 Skylake 上，mul 和 add 抢的是同一组端口 p0/p1，每元素共 2 个 FP uop ÷ 2 个端口 = **1 周期/元素**；而 2 次 load 除以 2 个 load 端口也是 **1 周期/元素**。两座资源同时把天花板按在 1 周期/元素上，于是：

```text
每元素耗时 = max( L/k , 资源上限 , 前端供给 )
           = max( 4/k , 1      , ≈1     )   [Skylake 标量 double]
```

k = 1 时耗时 4 周期/元素；k = 4 时降到 1 周期/元素；k = 8 时**仍然是 1 周期/元素** —— 收益在 k = 4 就已经吃完了，后面四个累加器换来的只是"调度器更从容"，而不是理论上的第二倍加速。这解释了为什么实测常常只有 2 到 3 倍：8× 是依赖链单独作用的结果，一旦把 load、mul、循环开销也算进同一份端口预算，天花板早就压在别处了。

顺带一提，从 Golden Cove 起 Intel 在向量引擎里加了一颗效率更高、延迟更低的专用快速加法器；到 Lion Cove 已经明确把 FMA 与 FADD 分到**不同的向量端口** —— 四条浮点/向量管线里，两条负责乘法与 FMA，另两条专门做浮点加法（端口布局见 [Intel 官方 Lion Cove 架构幻灯片](https://cdrdv2-public.intel.com/824430/2024_Intel_Tech%20Tour%20TW_Next%20Gen%20P-core%20The%20Lion%20Cove%20Architecture-4.pdf)，分工描述见 [WikiChip 整理的 Lion Cove 对比表](https://www.wikiwand.com/en/articles/Lion_Cove)）。这样一来 mul 和 add 不再互抢端口，代价是多一组执行单元的面积与对应的前递连线。

### 1.4 超过拐点还会变慢：寄存器压力

k 继续增大不但没收益，还可能负收益，原因有两层：

第一层是**物理寄存器耗尽**。k 个累加器意味着 k 个值必须同时保持 live。当 k 超过可用物理寄存器数，编译器会把其中一部分 spill 到栈上 —— 于是每周期多了 store/load，直接打到 load/store 端口和 L1D 上，通常是灾难性的。这在 SMT 打开时更严重（见延伸知识点第 5 条）。

第二层是**尾部归约**。k 个部分和在循环结束后要合成一个。用二叉树归约需要 log₂k 层加法，每层有依赖；如果对精度敏感还得按顺序加，那就是 k-1 次串行加法。这部分开销是 O(k) 或者 O(log k) 但每次条一级延迟，随着 k 增大而放大。

所以工程上的经验值是：先把 k 取到 `L × P`（Skylake 类取 8，Zen 5 / Golden Cove 类取 4），然后实测微调。类似的"多份中间状态"思路在 [多累加器流式和](../problems/streaming-square-sum.html) 里也是同一个道理。

### 1.5 用 perf 验证而不是猜

Intel 平台上端口占用是可以直接读出来的：

```bash
perf stat -e cycles,instructions,\
EXE_ACTIVITY.EXE_PORT_0,EXE_ACTIVITY.EXE_PORT_1,\
EXE_ACTIVITY.BOUND_ON_PORTS,\
UOPS_DISPATCHED_PORT.PORT_0,UOPS_DISPATCHED_PORT.PORT_1 ./bench

# 或者用现成的 top-down 度量组（Linux perf 的 vendor events）
perf stat -M tma_ports_utilized_0    # 每周期 0 uop 派发的周期占比
perf stat -M tma_ports_utilized_3m   # 每周期 ≥3 uop 的周期占比
```

`EXE_ACTIVITY.EXE_PORT_n` 计数的是"第 n 号端口被占用的周期数"，除以 `cycles` 就得到该端口利用率；`EXE_ACTIVITY.BOUND_ON_PORTS` 直接给出"因为拿不到端口而停顿"的周期占比。两套事件的编码在各代微架构上并不一致 —— Linux perf 用 vendor events 的 JSON 描述表自动处理这个差异，见 [perf vendor events 补丁邮件列表](https://lkml.kernel.org/lkml/20240314055919.1979781-9-irogers@google.com)。perf 内置的 `lpm_ports` 度量组会把每个 `UOPS_DISPATCHED.PORT_*` 除以 cycles，直接给出每个端口的利用率百分比。

判据很直接：**如果 BOUND_ON_PORTS 高而 k 还在增大**,已经到拐点了；如果 `cycles` 高但所有端口利用率都很低，瓶颈在依赖链（加大 k 有用）；如果 `UOPS_EXECUTED` 低但前端 idq_uops_not_delivered 高，那是前端跟不上，跟 k 无关。

---

## 二、硬件全景：一条指令要过的九道关卡

### 2.1 从五级到二十级：为什么深流水线换高频

教科书上的经典五级流水线只有 IF / ID / EX / MEM / WB：取指、译码、执行、访存、写回。它的约束很朴素 —— **一条指令的完整工作在五个"传送带节拍"内做完**，所以时钟周期长度必须容忍最慢那一级（通常是访存或 ALU）的传播延迟。

现代核心的做法是把每级再切细。以 Intel P6（Pentium Pro，1995）为例，它就把结构明确拆成了 **8 级顺序前端 + 3 级乱序核心 + 3 级退休**，共 14 级（[Clemson 的 P6 案例分析 / Colwell 讲义](https://people.cs.clemson.edu/~mark/330/colwell/case_p6.html)）。当代深流水线普遍在 **12 到 20+ 级**量级（厂商多已不再公布确切级数，此处为综合公开资料的量级估计，**非官方数字**）。

深流水线的收益很直接：级间组合逻辑变少 → 每级的传播延迟变短 → **主频可以更高**。代价是两条：

- **分支误预测惩罚 ≈ 流水线深度**：猜错一次要冲掉整条管道里的所有气泡。Intel 官方报价：P6 误预测惩罚 10 到 15 个周期。这也是为什么深流水线必须配一个极强的前端预测器 —— 预测算法本身（TAGE、感知机、BTB 组织）请看 [分支预测完全指南](../reading/branch-prediction-guide.html)，本文不重复。
- **锁存器与时钟树开销**：每一级边界都要一组触发器，级数翻倍意味着锁存开销和功耗线性上升，而实际有用的工作并没有变多。

### 2.2 宽度从来不一致：所以要有解耦缓冲

一个常见的误解是"这是一个几发射的核心"。实际上一条指令要连续穿越多个宽度不同的瓶颈：

| 阶段 | 它在做什么 | 典型宽度（代表下一节的核心） |
|---|---|---|
| 取指 | 从 L1I 读字节、预译码找指令边界 | 16 到 32 B/周期 |
| 译码 | x86 → 定长 uop（ARM 这一步轻得多） | 4 到 10 条/周期 |
| uop cache | 缓存已译码 uop，跳过译码 | 8 到 12 uop/周期（结构细节见 [前端与指令缓存](../reading/frontend-and-instruction-cache.html) 篇） |
| 重命名/分配 | 消除 WAW/WAR、分配 ROB 与物理寄存器 | 6 到 8 条/周期 |
| 发射/派发 | 就绪的 uop 送到执行端口 | 等于端口数（12 到 26 不等） |
| 退休 | 按序提交架构状态 | 8 到 16 条/周期 |

前端产能是**突发**的（译码 x86 变长指令很吃力，uop cache 命中时又快又省电），后端执行是**不规则**的（依赖、cache miss 会让某些周期几乎全空）。两者之间必须插一层**解耦缓冲（IDQ / uop queue）**，让前端短暂停顿时后端还有活干，反之亦然。这也是为什么"前端比后端宽"或"后端比前端宽"都不致命，但没有缓冲就一定致命。

注意**重命名阶段天生是串行的**：每条指令的目标寄存器必须按程序顺序映射到物理寄存器，否则无法恢复。这条约束在第三节还会回来限制"双译码集群"的设计。

### 2.3 寄存器重命名：只有 RAW 是真相关

三条数据相关里：

- **RAW**（先写后读）：真相关，无法消除，只能用前递缩短。
- **WAR**（先读后写）与 **WAW**（写后写）：**假相关**，只是程序员/编译器复用了同一个架构寄存器名造成的。

重命名把架构寄存器（ISA 可见的 16 个 GPR / 32 个 FP 寄存器）映射到一大池**物理寄存器（PRF）**，每条写操作拿一个新的物理寄存器，于是两条写同一个 `xmm0` 的指令再无瓜葛 —— WAR 和 WAW 直接消失，只剩 RAW 需要等待。

PRF 与 ROB 共同决定了**乱序窗口**能开多大：窗口 = 乱序引擎能同时看到多少条还没退休的指令，而其中能有多少条正在挂着等待结果，取决于 PRF 还有多少空闲条目。窗口开得大，理论上能越过更多 cache miss 延迟；代价是 ROB、PRF、调度队列都是多端口结构，**面积与功耗随条目数超线性增长**，而且访问延迟会随容量上升，反过来压主频。

当 PRF 里的空闲条目用完时，重命名阶段就会**停顿**（stall at rename/alloc），整条前端堵住。这就是"寄存器重命名用完"这个说法的技术含义 —— 它不是一个错误，而是一个常规的性能瓶颈，perf 里表现为 `resource_stalls` 类事件升高。

### 2.4 执行端口与调度队列：资源冲突的物理位置

重命名之后的 uop 进入**调度队列 / 保留站（Reservation Station, RS）**，等待源操作数就绪。就绪后被"挑选（pick）"并送往某个执行**端口**。端口是物理概念：一条 64 字节宽的布线和一个功能单元接在上面，同一周期只能接一条 uop。

典型的端口绑定约束（以 Intel Lion Cove 官方幻灯片为准）：V0 和 V2 是 FMA，V1 和 V3 是 FADD，只有 P6/P7 这类端口才带 MUL，**只有部分 port 带 fdiv/fsqrt**。因此一个纯浮点除法密集的循环，就算有再多的独立除法，也只能按除法单元的倒数吞吐排队 —— 这与前面讲的 FP 加是同一类约束，只是 P 从 2 降到了 1。

调度队列有两种流派：

- **集中式（统一调度队列）**：一个队接所有端口。好处是任意端口空闲都能补给，不会出现"某条队列排满但别的队列连接的功能单元在空转"的假延迟。代价是挑选逻辑要比较所有条目， wakeup/select 复杂度是 O(N)。
- **分布式（端口专属队列）**：每个/每组端口一个短队。挑选逻辑简单，按老优先（oldest-first）也更容易实现，天然接近"最老的先挑"这个好策略。代价是跨队列的负载无法互相调剂。

AMD 和 Qualcomm 在 Hot Chips 2024 上恰好给出了相反的答案。Zen 5 把整数侧的多个队列**统一**成一个对称结构，AMD 的理由是这样不会出现"某条队列积压、而另一条队列连接的功能单元在空转"的假延迟（[Chips and Cheese, Hot Chips 2024 现场记录](https://chipsandcheese.com/p/discussing-amds-zen-5-at-hot-chips-2024)）；代价是必须显式实现 age-aware 的挑选逻辑才能做到"最老的先挑"。Qualcomm 的 Gerard Williams 则主张分布式：队列越分散、每队条目越少，"多条同时就绪"的概率就越低，行为自然就近于 oldest-first。这不是非黑即白的问题 —— 业界至今没有共识。

### 2.5 数据前递：把写回 stage 提前一级

写回再加读寄存器的路径会让 RAW 串行的长度 = Latency(EX) + 1（写回）+ 1（读）。**前递网络（bypass / forwarding network）**在 EX 输出端直接拉一根线到所有功能单元的输入端，让下一条指令在同一个周期就能拿到上一条的结果。

代价是**布线面积与功耗极其昂贵**：一个有 N 个功能单元、每个单元 2 个源操作数的核心，前递网络近似是 N × N 的多路选择连线，且必须在关键路径上跑满一个时钟周期。这也是为什么"加一个执行单元"从来不是线性成本 —— 加它会让前递网络长出 N 条新连线，并让所有已有的连线负载电容变大、延迟变长。**这是"更宽"的隐性上限**。

### 2.6 顺序退休：只讲它在流水线里的位置

乱序执行出去的结果不能乱序提交出去，否则异常无法精确定位、也无法回滚。**重排序缓冲（ROB）**就是干这个的：指令按程序顺序进 ROB，乱序执行完毕后在里面标记"已完成"，然后按程序顺序离开 ROB 时才写回架构状态。

注意一个关键演进：早期设计（如 P6）**结果就存在 ROB 里**（40 条目 ROB 兼作 PRF），现代设计改成了 **PRF-based**：ROB 只保存状态和顺序信息，数据存在独立的物理寄存器堆里，退休只是"解除映射"。这大大减少了写回的数据搬运。ROB 的内部结构、它是怎么做异常恢复的，请看本站 [ROB 篇](../reading/rob-out-of-order-complete-guide.html)，本文不重复。

---

## 三、演进史：每一步解决了什么问题，又引入了什么代价

### 3.1 CDC 6600（1964）：scoreboard，动态调度的雏形

Seymour Cray 和 James Thornton 设计的 CDC 6600 有 10 个独立功能单元（多个加法器、两个乘法器、除法器、移位、布尔、增量器），每 100 ns 发射一条指令，而功能单元要 300 到 400 ns 才完成。为了不让一条慢除法堵住后面所有指令，它引入了一块叫做 **scoreboard（记分牌）** 的硬件来跟踪"哪个寄存器正被谁写、哪个功能单元忙"（[Thornton, Design of a Computer: The Control Data 6600, 1970；爱丁堡大学 HASE 6600 模型说明](https://www.icsa.inf.ed.ac.uk/research/groups/hase/models/6600/index.html)）。

- **解决**：顺序发射但**乱序完成**，慢操作不再阻塞无关指令。实测性能提升 FORTRAN 程序约 1.7 倍、手编汇编约 2.5 倍。
- **代价**：没有寄存器重命名，遇到 WAR 要阻塞写回、遇到 WAW 要阻塞发射；而且因为发射是顺序的，**一条卡住的指令会堵住后面所有指令**（因为它背后的发射通路只有一条）。

### 3.2 IBM System/360 Model 91（1967）：Tomasulo 算法

Robert Tomasulo 在 1967 年发表《An Efficient Algorithm for Exploiting Multiple Arithmetic Units》（IBM Journal of R&D, 11(1): 25-33），在 Model 91 的浮点单元上首次实现。三个原创构件：**寄存器重命名**（tag 替代值）、**每个功能单元前的保留站**、以及 **公共数据总线 CDB**（结果广播给所有在等的保留站）（[Tomasulo 算法综述](https://prod.wikiwand.com/en/articles/Imprecise_exception)）。Tomasulo 因此获得 1997 年 Eckert-Mauchly 奖。

- **解决**：假相关（WAR、WAW）被彻底消除；依赖通过" tag 匹配 + CDB 广播"解决，是一种受限的数据流执行。
- **代价**：两条。其一，**非精确异常（imprecise exception）** —— 乱序执行时无法确定是哪条指令触发的异常，无法重启；其二，**CDB 是全局广播总线**，面积和功耗随功能单元数增长，成为缩放瓶颈（现代设计改用点对点/分段的 wakeup 网络）。

### 3.3 超标量顺序执行：被假相关卡住的一代

CDC 6600 和 Model 91 都是每周期发射一条指令。RISC 出现后，1980 年代末到 1990 年代初涌现了一批多发射的**顺序**处理器：SuperSPARC、早期的 Pentium（U/V 双流水）等。它们能同时跑两三条指令，但一旦某条卡住，**后面全部跟着卡**。

问题在于，当时的编译器不敢做激进的寄存器分配（循环展开会人为造成大量 WAW/WAR），于是硬件需要重命名才能拉开 ILP —— 而顺序机没有。**这一代的天花板是"顺序"本身，不是宽度**。

### 3.4 P6 / Pentium Pro（1995）：ROB 把 Tomasulo 的思路做成可量产的通用核心

Intel P6 把"解耦的超标量"落地了（[Microprocessor Report 1995](https://www.cl.cam.ac.uk/teaching/0203/CompArch/Gwennap95-P6_Microarchitecture.pdf)、[Colwell 的 P6 讲义](https://people.cs.clemson.edu/~mark/330/colwell/case_p6.html)）：

- 顺序前端 8 级，每周期译码最多 3 条 x86 指令（只有一条能是复杂指令），每条展开成 1 到 4 个 uop；
- **40 条目 ROB**，既做顺序退休又当物理寄存器堆；
- **20 条目保留站**，每周期最多向 5 个执行单元派发 5 个 uop；
- 退休 3 个 uop/周期；FP 加法 3 周期；误预测惩罚 10 到 15 周期。

- **解决**：把 x86 翻译成类 RISC 的定长 uop 之后做动态调度，既保留 ISA 兼容，又拿到了 ILP；并第一次把**精确异常 + 顺序退休**做进产品（这就是 ROB 诞生的动因，结构细节见 [ROB 篇](../reading/rob-out-of-order-complete-guide.html)）。
- **代价**：x86 译码器复杂且能耗高；只有 FP 寄存器被重命名（Model 91 的遗留限制）；40 条目窗口在今天的尺度里小得可怜。

### 3.5 现代 PRF-based 设计（1990 年代末至今）

P6 之后的两大变化：

1. **ROB 不再存数据**。数据搬到独立的 PRF，ROB 只留状态。这让同样的 ROB 条目数下可以支持更大的窗口，也让写回路径短了不少。
2. **调度队列从单一集中走向分布/分组**。见 2.4 节的流派之争。

同时宽度持续扩张：重命名/发射宽度从 P6 的 3 uop/clk 一路走到今天的 8 uop/clk —— Intel 侧 Haswell/Skylake 是 4-wide、Golden Cove 6-wide、Lion Cove 8-wide；AMD 侧 Zen 4 是 6-wide、Zen 5 提到 8-wide（[Chips and Cheese](https://chipsandcheese.com/p/discussing-amds-zen-5-at-hot-chips-2024)）。具体对照见第四节的表。

到这里，"宽"这件事开始遇到三个物理上限：**前递网络的 N² 代价**、**多端口寄存器堆的访问延迟与面积**、以及**程序本身可用的 ILP 有限**。业界给出的答案开始分化。

### 3.6 2024 这一代的"分裂":双译码集群与拆分 OoO 引擎

2024 年两家同时动了前端：

**AMD Zen 5 的双译码集群**：两个独立的 4-wide 译码通道，取指宽度从 Zen 4 的 16 B/周期翻倍到 32 B/周期，汇入统一的 uop queue，再进 8-wide 重命名/发射/退休（[AMD, Hot Chips 2024；Tom's Hardware 报道](https://www.tomshardware.com/pc-components/cpus/amd-deep-dives-zen-5-ryzen-9000-and-strix-point-cpu-rdna-3-5-gpu-and-xdna-2-architectures/4)）。

这里有个非常值得记住的反直觉细节：**单线程模式下 Zen 5 只用其中一个译码集群**。AMD 工程师在 Hot Chips 场边对 Chips and Cheese 解释的原因是，两集群出来的 uop 流要在 uop queue 处重新按序缝合，而 uop queue 必须是顺序的 —— 因为它要喂给重命名器，而重命名本质上串行（[Chips and Cheese 现场记录](https://chipsandcheese.com/p/discussing-amds-zen-5-at-hot-chips-2024)）。所以双集群主要是**为 SMT 服务的**：两个线程各自占一个集群。AMD 也坦承 5.7 GHz 的频率目标让"两个集群同时服务单线程"比在低电压小核上做要难得多。

**Intel Lion Cove 的拆分引擎**：Lion Cove 没有做双译码集群，而是走另一条路 —— 单条 **8-wide 译码**（Redwood Cove 是 6-wide），但把乱序引擎**拆成整数域和向量域**，两者各带独立的重命名器、独立访问 uop queue、独立调度器（[Intel 官方 Lion Cove 幻灯片](https://cdrdv2-public.intel.com/824430/2024_Intel_Tech%20Tour%20TW_Next%20Gen%20P-core%20The%20Lion%20Cove%20Architecture-4.pdf)、[TechPowerUp Arrow Lake 架构篇](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html)）。同时 12-wide 退休、8-wide 分配、576 条目窗口、18 个执行端口。

- **解决**：两边的数据通路、寄存器堆、调度器都不共享，**省掉了另一侧的布线代价**；Intel 明确说这是为了让两个域可以独立演进（改整数侧不用重做向量侧）。
- **代价**：资源不能跨域调剂 —— 一个纯整数的负载无法借用向量域空闲的重命名带宽。

### 3.7 被淘汰的两条路：EPIC 和超长流水线

**VLIW / EPIC（Itanium, IA-64）**：Intel 与 HP 1994 年起合作，试图让**编译器**在编译期就把并行性写好（每条 bundle 里放 3 条指令 + 模板位），从而删掉硬件那套昂贵的动态调度逻辑。

失败原因是结构性的，不止一个：

1. **编译器看不到 cache miss**。静态调度必须预知运行时的访存延迟，而这不可能是编译期常量。这是根本性的信息不对称 —— 乱序硬件有编译器永远拿不到的运行时信息。Donald Knuth 的评价是：Itanium 需要的那种编译器"基本上不可能写出来"。
2. **指令宽度被固化进指令集**。如果一条 bundle 固定装 3 条指令，下一代芯片能做到 6 宽度时，两个 bundle 之间可能存在依赖 —— 无法并行。结果只有两个选项：要么新一代芯片只跑一半的硬件能力，要么每一代都要重新编译发布。软件发布方不可能为每个宽度发一个版本。
3. **代码膨胀**：找不到并行时编译器必须塞 nop，实测 VLIW bundle 让代码体积膨胀 3 到 6 倍（[Weaver, ICCD 2009](http://web.eece.maine.edu/~vweaver/papers/iccd09/iccd09_density.pdf)），反过来吃掉宝贵的取指带宽。
4. **生态时机**：AMD 2003 年推出向后兼容的 x86-64（Opteron），客户不用重写就有 64 位。Intel 一年后自己把这套扩展克隆成 EM64T。Itanium 的 x86 兼容模式只有奔腾级性能。

最终 Intel 于 2020 年 1 月停止接受新订单，2021 年 7 月交付最后一批（[Itanium 失败复盘](https://philosophersston.ee/knowledge/why-itanium-failed)）。**教训**：把调度责任下放给编译器这件事，只在你能保证所有信息都是编译期常量时成立 —— 而 cache 恰恰不是。

**NetBurst 超长流水线（Pentium 4）**：Intel 2000 年的 NetBurst 架构把流水线拉到极长以追求主频，Willamette 20 级，2004 年的 Prescott 加到 **31 级**（[AnandTech Prescott 架构分析](https://www.anandtech.com/show/1230/3)、[Tom's Hardware Prescott 评测](https://www.tomshardware.com/reviews/intel,751-5.html)）。

- **代价**：31 级流水线意味着误预测一次要冲掉整条管道；同频 IPC 显著低于更短的竞争对手；90 nm Prescott 的热量密度使得 4 GHz、乃至原定 10 GHz 的目标在功耗现实面前失败，"PresHot" 的绰号由此而来。Intel 2006 年转向基于 P6 血统改造的 Core 微架构，NetBurst 产品线 2007 年底停产。

这两次失败划出了深流水线和静态调度的实际边界，此后二十年所有人都在同一条路上前进：**动态调度 + 适度深的流水线 + 极强的前端预测**。

---

## 四、真实处理器对比（2024–2026）

下面三项分表列出。**所有数字后面标注来源与年份；凡是厂商未公开、只能靠第三方测量的，一律显式标注。**

### 4.1 前端宽度

| 核心 | 译码宽度 | uop cache | 重命名/分配 | 退休 | 乱序窗口（条目） |
|---|---|---|---|---|---|
| Intel Golden Cove (2021) | 6 uop/clk | 4096 条 8-way | 6-wide | 8-wide | 512（[WikiChip](https://en.wikichip.org/w/index.php?title=intel/microarchitectures/golden_cove&direction=prev&oldid=99739)） |
| Intel Redwood Cove (2023) | 6-way | 4096 条 8-way | 6-wide | 8-wide | 512 |
| Intel Lion Cove (2024) | **8-way**（官方幻灯片） | 5250 条 12-way | **8-wide** | **12-wide** | **576** |
| AMD Zen 4 (2022) | 4 instr/clk | 6.75K 条 | 6-wide | 6-wide | 320（[TechPowerUp](https://www.techpowerup.com/review/amd-ryzen-7-9700x/2.html)） |
| AMD Zen 5 (2024) | **2 × 4 instr/clk**（双集群，单线程只开一侧） | 6K 条 16-way | **8-wide** | 8-wide | 448 |
| ARM Neoverse V2 (2023) | 8-wide（带 MOP cache） | 有 MOP cache | 未找到官方数据 | 未找到官方数据 | 320 MOP（第三方实测） |
| ARM Neoverse V3 (2024–25) | **10-wide**（官方 SOG） | **无**（V3 删除了 MOP cache） | 派发 10 MOP / 20 uOP/clk | 未找到官方数据 | 384 MOP（第三方实测） |
| ARM Cortex-X925 (2024) | 10-wide | 无 | 未找到官方数据 | 未找到官方数据 | 768（第三方，见下文） |
| Apple M4 P-core (2024) | ≥10（第三方推断） | 未公开 | 未找到官方数据 | 未找到官方数据 | 约 3184 uop / 313 融合组（第三方实测） |

Neoverse V3 与 Cortex-X925 的实测数据来自 [杰哥的 Neoverse V3 微架构评测（2026）](https://jia.je/hardware/2026/06/13/arm-neoverse-v3)，Apple M4 数据来自 [杰哥的 Apple M4 微架构评测（2025）](https://jia.je/hardware/2025/05/21/apple-m4/)，**两者均为第三方黑盒测量而非厂商公布**。X925 的 768 条在飞窗口由 Chips and Cheese 分析给出。

### 4.2 Apple 为什么能做到 8–10 uop/clk：不只是"ARM 指令规整"

常见的说法是"AArch64 指令定长所以译码便宜"。这只讲了一半。真正让 Apple 堆到这个宽度的还有两条：

第一条是**持续 uop 吞吐目标就是这么定的**。Apple 官方的《Apple Silicon CPU Optimization Guide》明写 M 系列家族 P-core 每周期持续交付的 uop 数为：M1 = 8、M3 = 9、**M4 = 10**（E-core 为 M1 = 4、M4 = 5），而 jia.je 的实测 IPC 曲线也确实在 M4 P-core 上跑到了 10 —— 这说明宽度不是某个局部结构的名字，而是整条通路协同的结果。

第二条是**激进的指令融合**。第三方逆向出的 Firestorm 融合表里，`cmp/cmtst + b.cc`、`add/sub + cbz/cbnz`、`aese + aesmc`、`pmull + eor` 等组合在满足寄存器条件的相邻位置会被合成一个 uop（[dougallj/applecpu, Firestorm 文档](https://dougallj.github.io/applecpu/firestorm.html)）。也就是说，"10-wide"实际消化的原始指令远多于 10 条。**注意：uop cache 的内部结构属前端范畴，本文不展开，见 [前端与指令缓存](../reading/frontend-and-instruction-cache.html) 篇。**

还有一个容易忽略的点：Apple 的宽不是白来的 —— M4 P-core 的 ROB 容限被第三方测到约 **3184 个 uop / 313 个融合组**，是 x86 大核的 5 到 6 倍量级（同样为**第三方逆向估计，Apple 未公布**）。没有这么大的窗口，10-wide 的前端毫无意义。

### 4.3 后端：端口、寄存器、调度队列

| 维度 | Intel Lion Cove (2024) | AMD Zen 5 (2024) | ARM Neoverse V3 (2024–25) | Apple M4 P-core (2024) |
|---|---|---|---|---|
| 执行端口数 | **18**（Redwood Cove 12） | 6 ALU + 4 AGU + FP 侧的多个 pipe | — | 16 个执行单元（第三方推断） |
| 整数 ALU | 6 | 6（Zen 4 为 4） | 官方标称 8（实测 int add 只到 6 IPC） | 8（第三方推断） |
| 分支单元 | 3 | 3 | 3 | — |
| FP / SIMD | 2×FMA + 2×独立 FADD + 2×div + 4 SIMD ALU | 4 条 512-bit FP 管线；桌面版 FADD 延迟 2 周期 | 4 × 128-bit SIMD | 4（第三方推断） |
| 访存 | 多个 AGU，48 KB L0D + 192 KB L1D | 4 load + 2 store/clk；48 KB 12-way L1D | 1 LS + 2 LD + 1 ST pipe | 3 load + 2 sta + 2 std（突发） |
| 整数物理寄存器 | 未找到官方数据 | 240（Zen 4 为 224） | 约 400 个 64-bit 物理寄存器，拆成两半可当 32-bit 用（第三方实测推算） | 360（64-bit）／720（32-bit，同一份寄存器堆的拆半用法）（第三方实测） |
| 向量物理寄存器 | 未找到官方数据 | 384 × 512-bit（Zen 4 为 192） | 未找到官方数据 | 未观察到明显拐点，"与 M1 相近"（第三方） |
| 调度队列 | INT/VEC 各自独立 | ALU 调度器 88 条、AGU 调度器 56 条（统一化） | 未找到官方数据 | 未找到官方数据 |

Lion Cove 与 Zen 5 的数字来自前面引用的 [Intel 官方幻灯片](https://cdrdv2-public.intel.com/824430/2024_Intel_Tech%20Tour%20TW_Next%20Gen%20P-core%20The%20Lion%20Cove%20Architecture-4.pdf) 和 [TechPowerUp Zen 5 架构篇](https://www.techpowerup.com/review/amd-ryzen-7-9700x/2.html)。注意 Zen 5 的向量寄存器全部是完整 512 位宽（Zen 4 是 256 位双泵），这是 Zen 5 FP 侧在 SPEC FP 项目上大幅领先的关键之一。

### 4.4 同一代的两个答案

把 Lion Cove 和 Zen 5 放在一起看，它们是同一道题的两个不同答案：

- **Zen 5 赌宽度**：8-wide rename/发射/退休、6 个 ALU、4 个 AGU、448 窗口、240/384 的物理寄存器。代价是把 ALU/AGU 调度器统一之后，"老优先"挑选必须显式实现（age-aware scheduler 的复杂度）。
- **Lion Cove 赌深度+分域**：576 窗口（比 Zen 5 大 28%）、12-wide 退休、8-wide 分配，但把 INT/VEC 一刀切开，各自成人只用一半資源。此外 Intel 这一代彻底移除了 SMT，拿回来的面积和功耗预算全投进了前端和分域引擎。

没有哪一边明显更优，因为答案取决于负载里到底有多少 ILP —— 而这正是第一节那条公式在起作用：**如果程序本身就是一条 4 周期的链，两边再宽再深都没用。**

---

## 五、延伸知识点

1. **IPC 上限是三层取小，不是一个因素**。准确形式是 `IPC ≤ min(k/L, P, W)`：依赖链决定的 `k/L`、端口决定的 `P`、宽度决定的 `W`。三者里取最小的那个才是瓶颈。只算"延迟 × 并行度"会高估，只算端口会忽略链的影响 —— 这也是为什么"听说多累加器会变快"这种定性描述没意义，必须算出 k* 在哪。

2. **累加器收益拐点的算法**：`k* = L × P`，其中 L 是依赖操作的延迟、P 是每周期能执行该操作的端口数。算例：Skylake `4 × 2 = 8`；Zen 4 `3 × 2 = 6`；Golden Cove / Zen 5 `2 × 2 = 4`。再往上加累加器不但没有收益，还会因为 live value 变多而增加寄存器压力，一旦 spill 几乎是净亏。

3. **-O3 的展开倍数不是越大越好**。GCC/Clang 的自动展开（`unrollN`、unroll-and-jam）会根据代价模型选倍数，但如果你手动 `#pragma GCC optimize("unroll-loops")` 或写 `__attribute__((optimize("O3","unroll-loops")))` 把倍数顶到很大，会有三个退化：一是超过 k* 后前端只是白发射；二是 live value 变多导致 spill；三是代码体积涨大，超出 L1I（典型 32 到 192 KB）之后每次循环都要重新取指。判断是否过了头的最快办法是比较 `perf stat` 里的 `instructions` 有没有变化：如果不变量操作没变而 `uops_issued` 涨了，那就是白轮。

4. **看端口占用的两个事件**：`EXE_ACTIVITY.EXE_PORT_n` 给每个端口的占用周期数（除以 `cycles` 得利用率），`EXE_ACTIVITY.BOUND_ON_PORTS` 直接给"因端口冲突而停顿"的周期占比。Linux perf 的 `-M tma_ports_utilized_0/1/2/3m` 已把这些打包成可读的"每周期 0/1/2/≥3 个 uop"分布，非常适合快速判断是不是命中了 `P` 那一层。

5. **SMT 会抢物理寄存器，这是它最主要的隐式代价**。两个硬件线程共享同一个 PRF 和同一个 ROB（多数实现是静态分区或限制单线程上限，详情见 [ROB 篇](../reading/rob-out-of-order-complete-guide.html)）。所以你那条精心调到 k = 4 依赖链的代码，在 SMT 打开时可能因为物理寄存器被邻居分走而触发 spill。反过来看，这也解释了 Zen 5 为什么要做双译码集群：SMT 场景下 op cache 的命中率更低，两个线程会同时依赖真实的译码通路，才需要成倍地拉开前端带宽（[Chips and Cheese](https://chipsandcheese.com/p/discussing-amds-zen-5-at-hot-chips-2024)）。如果你的单线程延迟敏感负载跑在 SMT 机器上，测出来的数字和"关掉 SMT"的版本不是一个数。

6. **IPC 高不等于快**。这篇文章通篇在讲 IPC，但完成时间是 `指令数 × CPI / 主频`。三个因子都不是 IPC 单独能决定的：同样一段 IPC = 2.5 的代码，跑在 5.7 GHz 的核心上比跑在 3.8 GHz 的核心上快约 50%；反过来 Arm Cortex-X925 虽然达到了较高的 IPC，但因为要执行更多条指令（部分 SPEC CPU 项目上 ARM 的指令数比 x86 多出一倍以上）与主频劣势，总耗时并不占优。评价性能必须同时看这三样，`perf stat` 里就是 `instructions`、`cycles`、`task-clock` 的组合。

7. **再精妙的魔法常数也会过期**。同一份手写八累加器代码，在 Skylake 上正好卡住拐点，在 Zen 5 上多了一半，在未来某个把 FADD 降到 1 周期的核心上多余更多。所以写这类优化时，把展开倍数做成**宏或模板参数**，并在 README 里写明"这个值是在 L=?、P=? 的机器上调出来的"。依赖链分析的结论（"要先拆依赖"）是稳定的，具体数字不是。

---

## 参考来源

- [uops.info `ADDSD xmm, xmm` 指令表](https://uops.info/html-instr/ADDSD_XMM_XMM.html) —— 支撑本文所有 FP 加法延迟与吞吐数字（Skylake 4 / Zen 4 3 / Zen 5 2 / Golden Cove 2 周期），是 k* 公式的输入（2024–2025 持续更新）
- [Intel Lion Cove 官方架构幻灯片（2024 Intel Tech Tour TW）](https://cdrdv2-public.intel.com/824430/2024_Intel_Tech%20Tour%20TW_Next%20Gen%20P-core%20The%20Lion%20Cove%20Architecture-4.pdf) —— 一手来源，支撑 8-wide 译码、12-wide µop cache、8-wide 分配重命名、端口绑定（V0/V2 FMA、V1/V3 FADD）、INT/VEC 分域（2024）
- [TechPowerUp Arrow Lake 架构评测](https://www.techpowerup.com/review/intel-core-ultra-9-285k/3.html) —— 支撑 Lion Cove 的 12-wide 退休、576 窗口、18 端口，以及 Skymont 的 9-wide 译码 / 16-wide 退休 / 416 窗口（2024）
- [TechPowerUp Zen 5 架构评测（Ryzen 7 9700X）](https://www.techpowerup.com/review/amd-ryzen-7-9700x/2.html) —— 支撑 Zen 5 的 8-wide 派发/退休、6 ALU + 4 AGU、240/384 物理寄存器、448 窗口、Zen 4 对照值（2024）
- [Chips and Cheese: Discussing AMD's Zen 5 at Hot Chips 2024](https://chipsandcheese.com/p/discussing-amds-zen-5-at-hot-chips-2024) —— 支撑"双译码集群单线程只用一侧""统一 vs 分布式调度队列之争""8-wide renamer"（2024）
- [Tom's Hardware 引 AMD Hot Chips 2024](https://www.tomshardware.com/pc-components/cpus/amd-deep-dives-zen-5-ryzen-9000-and-strix-point-cpu-rdna-3-5-gpu-and-xdna-2-architectures/4) —— 支撑桌面 Zen 5 的 FADD 延迟由 3 降到 2 周期、双 4-wide 译码派设计（2024）
- [杰哥的 Apple M4 微架构评测](https://jia.je/hardware/2025/05/21/apple-m4/) —— 第三方实测，支撑 M4 P-core 持续 10 uop/clk、约 3184 uop ROB、360 个 64-bit 物理整数寄存器、16 个执行单元（2025，非 Apple 官方数字）
- [dougallj/applecpu: Firestorm 文档](https://dougallj.github.io/applecpu/firestorm.html) —— 第三方逆向，支撑 Apple 的指令融合清单与 8 uop/clk 管线宽度限制（2021 起持续更新）
- [杰哥的 ARM Neoverse V3 微架构评测](https://jia.je/hardware/2026/06/13/arm-neoverse-v3) —— 第三方实测 + 官方手册引述，支撑 10-wide 译码、派发 10 MOP/20 uOP、384 MOP ROB、8 ALU / 3 分支 / 4 SIMD、1LS+2LD+1ST（2026）
- [WikiChip Fuse: Arm Launches Next-Gen Flagship Cortex-X925](https://fuse.wikichip.org/news/7761/arm-launches-next-gen-flagship-cortex-x925/) —— 支撑 Cortex-X925 的 10-wide 译码派发与翻倍指令窗口（2024）
- [WikiChip 整理的 Lion Cove 对比表](https://www.wikiwand.com/en/articles/Lion_Cove) —— 支撑 Lion Cove 的 8-way 译码、8-way 分配重命名、12-wide 退休、576 窗口、18 端口、向量域"两条乘法/FMA + 两条浮点加"的分工，以及 Redwood Cove 对照值（2024）
- [WikiChip Golden Cove](https://en.wikichip.org/w/index.php?title=intel/microarchitectures/golden_cove&direction=prev&oldid=99739) —— 支撑 Golden Cove 的 ROB 512 条目、12 执行端口（2021）
- [Microprocessor Report 1995: Intel's P6 Uses Decoupled Superscalar Design（剑桥存档）](https://www.cl.cam.ac.uk/teaching/0203/CompArch/Gwennap95-P6_Microarchitecture.pdf) —— 支撑 P6 的 40 条目 ROB、20 条目保留站、每周期 5 uop 派发、3 uop 退休（1995）
- [Clemson / Colwell 的 Intel P6 案例分析](https://people.cs.clemson.edu/~mark/330/colwell/case_p6.html) —— 支撑 P6 的 8+3+3 级流水分段与 10–15 周期误预测惩罚（1990s）
- [爱丁堡大学 HASE CDC 6600 模型与设计说明](https://www.icsa.inf.ed.ac.uk/research/groups/hase/models/6600/index.html) —— 支撑 CDC 6600 的 scoreboard、10 个功能单元、100 ns 发射 / 300–400 ns 完成（基于 Thornton 1970 原著）
- [Tomasulo 算法综述（Wikiwand）](https://prod.wikiwand.com/en/articles/Imprecise_exception) —— 支撑 1967 年论文出处、三个原创构件、非精确异常与 CDB 缩放代价（引用自 Tomasulo, IBM J. R&D 11(1):25–33, 1967）
- [Itanium 失败复盘（Philosopher's Stone）](https://philosophersston.ee/knowledge/why-itanium-failed) 与 [Weaver, ICCD 2009 的代码密度论文](http://web.eece.maine.edu/~vweaver/papers/iccd09/iccd09_density.pdf) —— 支撑 EPIC 的失败原因清单与 VLIW 代码体积膨胀 3–6 倍（2026 / 2009）
- [AnandTech Prescott 架构分析](https://www.anandtech.com/show/1230/3) 与 [Tom's Hardware Prescott 评测](https://www.tomshardware.com/reviews/intel,751-5.html) —— 支撑 NetBurst 流水线由 20 级（Willamette）拉长到 31 级（Prescott）及其后果（2004）
- [Linux perf vendor events: ports metric group（LKML）](https://lkml.kernel.org/lkml/20240314055919.1979781-9-irogers@google.com) —— 支撑 `UOPS_DISPATCHED.PORT_*` / `EXE_ACTIVITY.*` 的用法与 `lpm_ports` 度量组定义（2024）

---

## 系列导航：处理器微架构深度指南

本文是《处理器微架构深度指南》系列之一。

**同系列其他文章**：

- [分支预测完全指南：从一次数据重排优化说起](branch-prediction-guide.html)
- [编译器如何自动优化分支预测](compiler-branch-optimization.html)
- [缓存层次完全指南：从一次矩阵乘法分块说起](cache-hierarchy-complete-guide.html)
- [内存层次与延迟隐藏：从指针追逐说起](memory-hierarchy-latency-hiding.html)
- [ROB 与乱序执行完全指南：一次瓶颈会诊的完整拆解](rob-out-of-order-complete-guide.html)
- [前端与指令缓存：为什么过度展开反而更慢](frontend-and-instruction-cache.html)
- [SIMD 与向量化完全指南：从 SAXPY 到 AVX10 与矩阵扩展](simd-vectorization-complete-guide.html)
- [TLB 与地址转换：从一次聚集重排说起](tlb-address-translation-guide.html)

所属模块：[ILP 与流水线](../modules/ilp.html)
