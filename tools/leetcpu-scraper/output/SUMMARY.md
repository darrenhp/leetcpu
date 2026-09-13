# LeetCPU 抓取摘要

- 抓取时间：2026-09-13T15:42:10+00:00
- 目标 URL：https://www.leetcpu.com/
- 抓取模式：full
- 站点形态：SPA 空壳（需渲染）
- 标题：LeetCPU — CPU Performance Training with ChampSim
- 描述：Write C. Understand CPU microarchitecture. Interactive challenges for branch prediction, cache locality, ILP, and memory hierarchy — with real ChampSim simulation feedback.

## 页面指标

| 数值 | 含义 |
| --- | --- |
| 22 | Core challenges |
| 12 | Metrics surfaced |
| <10s | Typical feedback loop |

## 题库（22 题）

| # | Slug | 标题 | 难度 | 分类 | 仿真配置 |
| --- | --- | --- | --- | --- | --- |
| 1 | `stable-partition-branches` | Stable Partition for Predictable Branches | Easy | Branch Prediction | Bimodal BP · 4-wide OoO · 32KB L1 |
| 2 | `matrix-multiply-tiling` | Matrix Multiply — Cache Tiling | Easy | Cache Locality | TAGE BP · 4-wide OoO · 48KB L1 |
| 3 | `reduction-tree-ilp` | Reduction Tree — Break Dependency Chains | Medium | ILP / Pipeline | TAGE · 8-wide OoO · ROB=512 |
| 4 | `interleaved-pointer-chasing` | Pointer Chasing — Recover Memory Parallelism | Medium | Memory Parallelism | IP-stride prefetch · 16MB LLC |
| 5 | `bottleneck-triage` | Bottleneck Triage — Diagnose and Fix | Hard | Diagnosis | Perceptron BP · realistic config |
| 6 | `strided-sum-prefetch` | Strided Sum — Hide Latency with Prefetch | Medium | Memory Parallelism | ChampSim · stride prefetch · 4-wide OoO |
| 7 | `bitset-popcount-throughput` | Bitset Scan — Turn Scalar Loops into Throughput | Easy | ILP / Pipeline | ChampSim · wide integer backend · 8-wide OoO |
| 8 | `particle-struct-repacking` | Particle Score — Repack Structs for Bandwidth | Medium | Cache Locality | ChampSim · 48KB L1D · bandwidth study |
| 9 | `image-blur-tiling` | Image Blur — Tile the Working Set | Medium | Cache Locality | ChampSim · 32KB L1D · stencil trace |
| 10 | `branchless-score-window` | Score Window — Remove Unpredictable Branches | Easy | Branch Prediction | ChampSim · TAGE-SC-L · branch study |
| 11 | `ece-jacobi-stencil-lab` | ECE Lab — 2D Jacobi Stencil Cache Blocking | Medium | Cache Locality | ChampSim · 4-wide OoO · 32KB L1D · classroom trace |
| 12 | `irregular-gather-prefetch` | Irregular Gather — Prefetch Ahead of Random Access | Medium | Memory Parallelism | GShare BP · 4-wide OoO · IP-stride prefetch |
| 13 | `histogram-dependency-chains` | Histogram — Break Write Dependency Chains | Medium | ILP / Pipeline | GShare BP · 4-wide OoO · 32KB L1 |
| 14 | `3d-loop-interchange` | 3D Array — Fix Loop Order for Sequential Access | Easy | Cache Locality | GShare BP · 4-wide OoO · 32KB L1 |
| 15 | `binary-search-eytzinger` | Binary Search — Cache-Friendly Eytzinger Layout | Hard | Cache Locality | GShare BP · 4-wide OoO · 32KB L1 |
| 16 | `streaming-square-sum` | Streaming Computation — Multiple Accumulators for ILP | Easy | ILP / Pipeline | GShare BP · 4-wide OoO · 32KB L1 |
| 17 | `row-major-cache-locality` | 2D Grid Sum — Fix Column-First Access Order | Medium | Cache Locality | ChampSim · 4-wide OoO · cache locality study |
| 18 | `saxpy-auto-vectorize` | SAXPY — Unlock Auto-Vectorization | Easy | ILP / Pipeline | ChampSim · 8-wide OoO · throughput study |
| 19 | `gather-reorder-cache-clustering` | Gather Reordering — Cluster Accesses by Cache Region | Medium | Cache Locality | ChampSim · 4-wide OoO · cache locality study |
| 20 | `masked-saxpy-branchless` | Masked SAXPY — Remove Branches, Unlock Throughput | Medium | Branch Prediction | ChampSim · 8-wide OoO · branch + throughput study |
| 21 | `dot-product-eight-accumulators` | Dot Product — Eight Accumulators for ILP | Easy | ILP / Pipeline | ChampSim · 8-wide OoO · dependency-chain study |
| 22 | `lookup-table-grade-bands` | Grade Bands — Replace Nested Branches with a LUT | Easy | Branch Prediction | ChampSim · TAGE-SC-L · branch study |

## 渲染后标题结构

- Write C. Understand the CPU.
  - Learn CPU behavior by changing real code.
  - How it works
  - CPU Optimization Flow
  - Designed for real performance work.
  - Ready to optimize?

