# LeetCPU 中文站

把 [leetcpu.com](https://www.leetcpu.com/) 的课程与题库整理成中文静态站点：**8 个知识模块 + 22 道题目**，
每题配一份完整的**优化报告**，说明「慢在哪、怎么改、为什么更快、什么时候不该这么改」。
**不做代码提交与在线仿真** —— 所有代码静态展示，可一键复制。

## 生成与预览

```bash
cd leetcpu
python src/build.py                # 生成到仓库根的 ./docs（约 1 秒，仅标准库）
python src/build.py --out ../docs  # 也可以显式指定输出目录
```

直接用浏览器打开 `docs/index.html` 即可（搜索、主题切换、代码高亮在 `file://` 下同样可用）。

## 在线地址

<https://darrenhp.github.io/leetcpu/>

GitHub Pages 从本仓库 `main` 分支的 `/docs` 目录发布，推送后约 1–2 分钟生效。

## 目录结构

仓库分为三层：`src/`（源码与内容）、`docs/`（生成产物）、`tools/`（辅助工具）。

```
leetcpu/
├── src/                        站点生成器与全部内容资产
│   ├── build.py                站点生成器（Python 标准库，无依赖）
│   ├── content/
│   │   ├── modules_zh.py       8 个知识模块的中文讲解 + 模块↔题目映射
│   │   ├── problems_zh_a.py    第 1–11 题的中文题面与优化报告
│   │   ├── problems_zh_b.py    第 12–22 题的中文题面与优化报告
│   │   └── reading/            扩展阅读：Markdown 源文件（front matter + 正文）
│   ├── data/                   源数据快照（仓库自包含，克隆即可重建）
│   │   ├── problems.json
│   │   └── dataset_curriculum.json
│   └── static/
│       ├── style.css           明暗双主题样式
│       └── app.js              搜索 / 主题 / C 代码高亮 / 复制 / 筛选
├── docs/                       生成产物（33 个页面 + 4 个资源 + .nojekyll）
│   ├── index.html              首页：概览、题目总览表、模块卡片
│   ├── modules.html            模块总览与模块↔题目对应表
│   ├── modules/<id>.html       8 个模块页
│   ├── problems.html           22 题列表（按模块筛选）
│   ├── problems/<slug>.html    22 个题目页
│   ├── reading.html            扩展阅读首页
│   ├── reading/<slug>.html     文章页（由 Markdown 渲染，右侧 TOC 自动生成）
│   └── assets/                 style.css / app.js / search.json / search.js
└── tools/
    └── leetcpu-scraper/        上游抓取器源码 + 结构化数据产物
        ├── leetcpu_scraper/    抓取器实现
        ├── main.py
        ├── tests/
        ├── requirements.txt
        └── output/
            ├── problems.json          题面 / 指标 / 代码的原始快照
            ├── problems.csv
            ├── problems.jsonl
            ├── dataset_curriculum.json
            ├── SUMMARY.md             抓取结果概览
            └── page_text.txt
```

## 扩展阅读

比题目页更长、更完整的专题文章，源文件是 `src/content/reading/*.md`（带 front matter 的 Markdown，
正文原样保留，由 `src/build.py` 内的纯标准库渲染器转成 HTML）。新增文章只需往该目录放一个 `.md`：

```
---
title: 文章标题
module: branch        # 可选：关联的模块 id，留空则不挂模块
date: 2026-09-14
summary: 一句话摘要
tags: [标签1, 标签2]
---
```

front matter 里填了 `module` 的文章会自动出现在对应知识模块页底部；反之模块页不显示该区块。

## 题目页结构

| 小节 | 内容 |
| --- | --- |
| 题面 | 中文题面、任务说明、约束、示例（输入/输出/说明） |
| 指标目标 | 每题的达标阈值与指标含义（IPC、MPKI、平均延迟等） |
| 起始代码 | 基线实现（可直接复制） |
| 参考解答 | 优化后实现 |
| 优化报告 | 摘要 → 瓶颈定位 → 优化思路 → 关键改动 → 为什么更快 → 常见误区 |
| 要点速记 / 常见误区 | 可复用的经验与边界条件 |

报告刻意保留了**诚实的取舍说明**：例如第 8 题指出 AoS→SoA 的真正收益有限、第 9 题指出水平 stencil 分块贡献不大、
第 17 题指出纯求和无数据复用时循环交换才是主要收益 —— 避免把「通用套路」包装成万能解。

## 内容来源与版权

题面、约束、指标阈值、起始代码与参考解答来自 leetcpu.com 公开页面（由 `tools/leetcpu-scraper` 抓取），
**中文讲解与优化报告为人工撰写**。题目与代码版权归原站所有，本站仅用于学习交流。

## 数据来源

`src/build.py` 读取源数据，按以下顺序查找（先命中者生效）：

1. `src/data/problems.json`、`src/data/dataset_curriculum.json` —— 仓库自带快照
2. `tools/leetcpu-scraper/output/*.json` —— 上游抓取器的最新产物

因此克隆本仓库后无需运行抓取器即可重建；若本地有更新的抓取结果，会自动优先使用。
更新数据后重跑 `python src/build.py` 并推送 `docs/` 即可同步线上。

抓取器的中间抓取缓存（`output/_cache/`、`output/raw/`、`output/snapshot.json`）不入仓库，需要时可重新抓取。
