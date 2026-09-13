#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LeetCPU 中文站生成器（仅标准库）。

用法：
    python src/build.py                # 生成到仓库根的 ./docs
    python src/build.py --out ../docs  # 指定输出目录
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "content"))

from modules_zh import MODULES, MODULE_PROBLEMS          # noqa: E402
from problems_zh_a import PROBLEMS_ZH as ZH_A            # noqa: E402
from problems_zh_b import PROBLEMS_ZH as ZH_B            # noqa: E402

ZH: dict = {**ZH_A, **ZH_B}
def _data(name: str) -> Path:
    """定位源数据：优先仓库内 data/ 快照，回退到抓取器输出目录。

    仓库自带快照，因此克隆后无需跑抓取器即可重建站点；
    若本地有更新的抓取产物，则自动优先使用它。
    """
    local = HERE / "data" / name
    if local.exists():
        return local
    return HERE.parent / "tools" / "leetcpu-scraper" / "output" / name


PROBLEMS_JSON = _data("problems.json")
CURRICULUM_JSON = _data("dataset_curriculum.json")

DIFF = {"Easy": "简单", "Medium": "中等", "Hard": "困难"}
CAT = {
    "Branch Prediction": "分支预测",
    "Cache Locality": "缓存局部性",
    "ILP / Pipeline": "ILP / 流水线",
    "Memory Parallelism": "内存并行",
    "Diagnosis": "综合诊断",
}
METRIC = {
    "ipc": ("IPC", "每周期退休指令数"),
    "branch_mpki": ("Branch MPKI", "每千条指令的分支预测失败次数"),
    "l1_mpki": ("L1 MPKI", "每千条指令的 L1 数据缓存失效次数"),
    "l2_mpki": ("L2 MPKI", "每千条指令的 L2 缓存失效次数"),
    "llc_mpki": ("LLC MPKI", "每千条指令的最后一级缓存失效次数"),
    "l1d_avg_latency": ("L1D 平均延迟", "L1D 访问的平均延迟（周期）"),
    "cycles": ("总周期数", "完成计算所消耗的 CPU 周期总数"),
}

SITE_TITLE = "LeetCPU 中文站"
SITE_DESC = "CPU 性能优化题库：8 个知识模块、22 道实战题目，每道附完整优化报告。"


# ---------------------------------------------------------------- 工具
def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def md_inline(s: str) -> str:
    """支持 `code` 与 **bold** 两种行内标记。"""
    out = html.escape(str(s), quote=False)
    out = re.sub(r"`([^`]+)`", r'<code>\1</code>', out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def slug_anchor(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "sec"


def codeblock(label: str, code: str, note: str = "") -> str:
    return f"""<div class="codeblock">
  <div class="codebar"><span class="label">{esc(label)}</span><button class="copybtn">复制</button></div>
  <pre data-code="{html.escape(code, quote=True)}"></pre>
</div>""" + (f'<p class="sub">{md_inline(note)}</p>' if note else "")


# ---------------------------------------------------------------- 数据
def load():
    problems = json.loads(PROBLEMS_JSON.read_text(encoding="utf-8"))
    problems.sort(key=lambda p: p["id"])
    by_slug = {p["slug"]: p for p in problems}
    mod_of: dict[str, str] = {}
    for mid, slugs in MODULE_PROBLEMS.items():
        for s in slugs:
            mod_of.setdefault(s, mid)
    for p in problems:                      # 兜底：未显式映射的按分类归到首个匹配模块
        if p["slug"] not in mod_of:
            for mid, m in MODULES.items():
                if m["en"] == p["category"]:
                    mod_of[p["slug"]] = mid
                    break
            else:
                mod_of[p["slug"]] = "cache"
    return problems, by_slug, mod_of


# ---------------------------------------------------------------- 布局
def sidebar(root: str, active: str, problems, mod_of) -> str:
    parts = ['<h4>开始</h4>',
             f'<a href="{root}index.html" class="{_on(active, "index")}">概览</a>',
             f'<a href="{root}modules.html" class="{_on(active, "modules")}">知识模块</a>',
             f'<a href="{root}problems.html" class="{_on(active, "problems")}">全部题目</a>',
             '<h4>知识模块</h4>']
    for mid, m in MODULES.items():
        cls = "on" if active == f"mod:{mid}" else ""
        parts.append(f'<a href="{root}modules/{mid}.html" class="{cls}">{esc(m["zh"])}</a>')
    parts.append('<h4>题目</h4>')
    for mid, m in MODULES.items():
        subs = [p for p in problems if mod_of[p["slug"]] == mid]
        if not subs:
            continue
        parts.append(f'<h4 style="margin-top:12px">{esc(m["zh"])}</h4>')
        for p in subs:
            z = ZH[p["slug"]]
            cls = "on" if active == f"prob:{p['slug']}" else ""
            parts.append(
                f'<a href="{root}problems/{p["slug"]}.html" class="{cls}">'
                f'<span class="num">{p["id"]:02d}</span>{esc(z["zh"])}</a>')
    return "\n".join(parts)


def _on(active: str, key: str) -> str:
    return "on" if active == key else ""


def layout(*, root: str, title: str, active: str, body: str, toc: str = "",
           problems=None, mod_of=None, desc: str = "") -> str:
    nav = [
        ("index", "概览", f"{root}index.html"),
        ("modules", "知识模块", f"{root}modules.html"),
        ("problems", "全部题目", f"{root}problems.html"),
    ]
    navhtml = "".join(
        f'<a href="{u}" class="{_on(active, k)}">{t}</a>' for k, t, u in nav)
    side = sidebar(root, active, problems or [], mod_of or {})
    toc_html = f'<aside class="toc"><h4>本页目录</h4>{toc}</aside>' if toc else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · {SITE_TITLE}</title>
<meta name="description" content="{esc(desc or SITE_DESC)}">
<link rel="stylesheet" href="{root}assets/style.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%232f6feb'/><text x='16' y='23' font-size='19' font-family='monospace' fill='white' text-anchor='middle'>C</text></svg>">
</head>
<body data-root="{root}">
<header class="topbar">
  <a class="brand" href="{root}index.html"><span class="dot">C</span>LeetCPU 中文站</a>
  <nav class="topnav">{navhtml}</nav>
  <div class="searchbox">
    <input id="q" type="search" placeholder="搜索题目 / 模块…" autocomplete="off">
    <div id="results"></div>
  </div>
  <button class="iconbtn" data-theme-toggle title="切换明暗主题">◐</button>
</header>
<div class="wrap">
  <nav class="sidebar">{side}</nav>
  <main>{body}</main>
  {toc_html}
</div>
<footer>
  本站为 <a href="https://www.leetcpu.com/">leetcpu.com</a> 课程与题目的中文整理版，
  仅用于学习交流；题目、代码版权归原站所有。内容由程序从公开页面整理并人工撰写优化报告。
</footer>
<script src="{root}assets/search.js"></script>
<script src="{root}assets/app.js"></script>
</body>
</html>
"""


# ---------------------------------------------------------------- 页面
def page_index(root, problems, mod_of) -> str:
    stats = "".join(
        f'<div class="stat"><b>{v}</b><span>{t}</span></div>'
        for v, t in [("22", "道实战题目"), ("8", "个知识模块"),
                     ("12", "项性能指标"), ("22", "份优化报告")])
    cards = "".join(
        f'<a class="card" href="{root}modules/{mid}.html">'
        f'<span class="badge mod" style="background:{m["color"]}">{esc(m["icon"])}</span> '
        f'<h3>{esc(m["zh"])}</h3><p>{esc(m["tagline"])}</p>'
        f'<p class="sub" style="margin-top:8px">{len([1 for p in problems if mod_of[p["slug"]] == mid])} 道相关题目</p></a>'
        for mid, m in MODULES.items())

    rows = "".join(
        f'<tr data-mod="{mod_of[p["slug"]]}">'
        f'<td>{p["id"]:02d}</td>'
        f'<td><a href="{root}problems/{p["slug"]}.html">{esc(ZH[p["slug"]]["zh"])}</a></td>'
        f'<td><span class="badge {p["difficulty"].lower()}">{DIFF.get(p["difficulty"], p["difficulty"])}</span></td>'
        f'<td>{esc(CAT.get(p["category"], p["category"]))}</td>'
        f'<td>{esc(ZH[p["slug"]]["bottleneck"])}</td></tr>'
        for p in problems)

    body = f"""
<h1>LeetCPU 中文站</h1>
<p class="lead">面向 CPU 微架构性能优化的中文题库：{len(problems)} 道实战题目、{len(MODULES)} 个知识模块，
每道题都附带一份<strong>完整的优化报告</strong>，讲清楚“慢在哪里、怎么改、为什么更快、什么时候不该这么改”。</p>
<div class="grid g3" style="margin:22px 0 30px">{stats}</div>

<h2 id="how">怎么用这个站</h2>
<ol>
  <li>先按<strong>知识模块</strong>补齐概念：分支预测、缓存层次、ILP、内存并行、ROB、前端、TLB、SIMD。</li>
  <li>再按模块进入对应的<strong>题目</strong>：先读题面与约束，自己想一想瓶颈在哪。</li>
  <li>最后对照<strong>优化报告</strong>：先看“瓶颈定位”，再看“关键改动”，最后看“常见误区”。</li>
</ol>
<div class="callout">本站<strong>不提供代码提交与仿真</strong>。所有代码均为静态展示，可一键复制到你本地的
ChampSim / perf 环境中实测。想跑在线仿真请访问 <a href="https://www.leetcpu.com/">leetcpu.com</a>。</div>

<h2 id="overview">题目总览</h2>
<p class="sub">共 {len(problems)} 题。点击标题进入题目页与优化报告。</p>
<table><thead><tr><th style="width:44px">#</th><th>题目</th><th style="width:64px">难度</th>
<th style="width:110px">分类</th><th>核心瓶颈</th></tr></thead><tbody>
{rows}
</tbody></table>

<h2 id="modules">知识模块</h2>
<div class="grid g2">{cards}</div>
"""
    toc = "".join(f'<a href="#{i}">{t}</a>' for i, t in
                  [("how", "怎么用这个站"), ("overview", "题目总览"), ("modules", "知识模块")])
    return layout(root=root, title="概览", active="index", body=body, toc=toc,
                  problems=problems, mod_of=mod_of)


def page_modules(root, problems, mod_of) -> str:
    cards = "".join(
        f'<a class="card" href="{root}modules/{mid}.html">'
        f'<span class="badge mod" style="background:{m["color"]}">{esc(m["icon"])}</span> '
        f'<h3>{esc(m["zh"])} <span class="sub">{esc(m["en"])}</span></h3>'
        f'<p>{esc(m["tagline"])}</p>'
        f'<p class="sub" style="margin-top:8px">'
        f'{len([1 for p in problems if mod_of[p["slug"]] == mid])} 道题目 · {len(m["sections"])} 节讲解 · {len(m["metrics"])} 个关键指标</p></a>'
        for mid, m in MODULES.items())
    body = f"""
<h1>知识模块</h1>
<p class="lead">每个模块讲解一个 CPU 微架构主题：它是什么、为什么会成为瓶颈、怎么修、该看哪些计数器。</p>
<div class="grid g2">{cards}</div>
<h2 id="map">模块与题目的对应关系</h2>
<table><thead><tr><th style="width:130px">模块</th><th>相关题目</th></tr></thead><tbody>
{"".join(f'<tr><td><a href="{root}modules/{mid}.html">{esc(m["zh"])}</a></td><td>'
         + "、".join(f'<a href="{root}problems/{s}.html">{esc(ZH[s]["zh"])}</a>'
                     for s in MODULE_PROBLEMS.get(mid, []))
         + "</td></tr>" for mid, m in MODULES.items())}
</tbody></table>
"""
    return layout(root=root, title="知识模块", active="modules", body=body,
                  toc='<a href="#map">模块与题目的对应关系</a>',
                  problems=problems, mod_of=mod_of)


def page_module(root, mid, problems, mod_of) -> str:
    m = MODULES[mid]
    subs = [p for p in problems if mod_of[p["slug"]] == mid]
    secs = []
    anchors = []
    for i, s in enumerate(m["sections"], 1):
        # 中文标题无法转成 ASCII slug，用序号生成稳定且唯一的锚点
        a = f"c{i}"
        anchors.append((a, s["h"]))
        paras = "".join(f"<p>{md_inline(x)}</p>" for x in s["p"])
        secs.append(f'<h3 id="{a}">{esc(s["h"])}</h3>{paras}')
    metrics = "".join(
        f'<tr><td><b>{esc(x["k"])}</b></td><td>{md_inline(x["tip"])}</td></tr>' for x in m["metrics"])
    rel = "".join(
        f'<tr><td><a href="{root}problems/{p["slug"]}.html">{p["id"]:02d} · {esc(ZH[p["slug"]]["zh"])}</a></td>'
        f'<td><span class="badge {p["difficulty"].lower()}">{DIFF.get(p["difficulty"], p["difficulty"])}</span></td>'
        f'<td>{esc(ZH[p["slug"]]["one_liner"])}</td></tr>' for p in subs)

    body = f"""
<div class="meta">
  <span class="badge mod" style="background:{m['color']}">{esc(m['icon'])}</span>
  <span class="sub">{esc(m['en'])}</span>
</div>
<h1>{esc(m["zh"])}</h1>
<p class="lead">{md_inline(m["tagline"])}</p>
{"".join(f"<p>{md_inline(x)}</p>" for x in m["overview"])}

<h2 id="concepts">概念讲解</h2>
{"".join(secs)}

<h2 id="metrics">关键指标</h2>
<table><thead><tr><th style="width:170px">指标</th><th>怎么读</th></tr></thead><tbody>{metrics}</tbody></table>

<div class="callout"><b>动手提示</b><br>{md_inline(m["tip"])}</div>

<h2 id="problems">配套题目</h2>
{"<table><thead><tr><th>题目</th><th style='width:64px'>难度</th><th>一句话思路</th></tr></thead><tbody>" + rel + "</tbody></table>" if rel else "<p class='sub'>该模块暂无直接配套题目，可先阅读概念讲解。</p>"}
"""
    toc = (f'<a href="#concepts">概念讲解</a>'
           + "".join(f'<a href="#{a}">{esc(h)}</a>' for a, h in anchors)
           + '<a href="#metrics">关键指标</a><a href="#problems">配套题目</a>')
    return layout(root=root, title=m["zh"], active=f"mod:{mid}", body=body, toc=toc,
                  problems=problems, mod_of=mod_of, desc=m["tagline"])


def page_problems(root, problems, mod_of) -> str:
    btns = ['<button class="badge" data-filter="all" style="cursor:pointer">全部</button>']
    for mid, m in MODULES.items():
        n = len([1 for p in problems if mod_of[p["slug"]] == mid])
        if n:
            btns.append(f'<button class="badge" data-filter="{mid}" style="cursor:pointer">{esc(m["zh"])} ({n})</button>')
    rows = "".join(
        f'<tr data-mod="{mod_of[p["slug"]]}">'
        f'<td>{p["id"]:02d}</td>'
        f'<td><a href="{root}problems/{p["slug"]}.html">{esc(ZH[p["slug"]]["zh"])}</a>'
        f'<div class="sub">{esc(ZH[p["slug"]]["one_liner"])}</div></td>'
        f'<td><span class="badge {p["difficulty"].lower()}">{DIFF.get(p["difficulty"], p["difficulty"])}</span></td>'
        f'<td><a href="{root}modules/{mod_of[p["slug"]]}.html">{esc(MODULES[mod_of[p["slug"]]]["zh"])}</a></td>'
        f'<td>{esc(ZH[p["slug"]]["bottleneck"])}</td>'
        f'<td class="sub">{esc(p["simConfig"])}</td></tr>'
        for p in problems)
    body = f"""
<h1>全部题目</h1>
<p class="lead">共 {len(problems)} 题，每题含中文题面、约束、指标目标、起始代码、参考解答与优化报告。</p>
<div id="filters" style="display:flex;flex-wrap:wrap;gap:6px;margin:16px 0 18px">{"".join(btns)}</div>
<table><thead><tr><th style="width:44px">#</th><th>题目 / 一句话思路</th><th style="width:64px">难度</th>
<th style="width:100px">模块</th><th style="width:150px">核心瓶颈</th><th style="width:190px">仿真配置</th></tr></thead>
<tbody id="plist">{rows}</tbody></table>
"""
    return layout(root=root, title="全部题目", active="problems", body=body, toc="",
                  problems=problems, mod_of=mod_of)


def page_problem(root, p, prev_p, next_p, problems, mod_of) -> str:
    z = ZH[p["slug"]]
    mid = mod_of[p["slug"]]
    m = MODULES[mid]
    rep = z["report"]

    targets = "".join(
        f'<tr><td><b>{esc(METRIC.get(t.get("key", ""), (t.get("label", ""), ""))[0])}</b></td>'
        f'<td><code>{esc(t.get("op", ""))} {esc(t.get("value", ""))}{esc(t.get("unit", ""))}</code></td>'
        f'<td class="sub">{esc(METRIC.get(t.get("key", ""), ("", ""))[1] or t.get("label", ""))}</td></tr>'
        for t in p["targets"])

    ex = "".join(
        f'<div class="kv"><div><b>输入</b>{md_inline(e["input"])}</div>'
        f'<div><b>输出</b>{md_inline(e["output"])}</div>'
        f'<div><b>说明</b>{md_inline(e["explanation"])}</div></div>'
        for e in z["examples"])

    secs, anchors = [], []
    for i, s in enumerate(rep["sections"], 1):
        a = f"r{i}"          # 中文标题无法转 ASCII slug，用序号保证锚点唯一
        anchors.append((a, s["h"]))
        paras = "".join(f"<p>{md_inline(x)}</p>" for x in s["p"])
        secs.append(f'<h3 id="{a}">{esc(s["h"])}</h3>{paras}')

    kp = "".join(f"<li>{md_inline(x)}</li>" for x in rep["keypoints"])
    pf = "".join(f"<li>{md_inline(x)}</li>" for x in rep["pitfalls"])

    body = f"""
<div class="meta">
  <span class="badge">{p["id"]:02d}</span>
  <span class="badge {p["difficulty"].lower()}">{DIFF.get(p["difficulty"], p["difficulty"])}</span>
  <span class="badge mod" style="background:{m["color"]}">{esc(m["zh"])}</span>
  <span class="sub">{esc(CAT.get(p["category"], p["category"]))} · {esc(p["simConfig"])}</span>
</div>
<h1>{esc(z["zh"])}</h1>
<p class="lead">{md_inline(z["one_liner"])}</p>
<p class="sub">原始英文标题：{esc(p["title"])}</p>

<h2 id="statement">题面</h2>
{"".join(f"<p>{md_inline(x)}</p>" for x in z["desc"])}
<div class="callout"><b>任务</b><br>{md_inline(z["prompt"])}</div>

<h3 id="constraints">约束</h3>
<ul>{"".join(f"<li>{md_inline(c)}</li>" for c in z["constraints"])}</ul>

<h3 id="examples">示例</h3>
{ex}

<h2 id="targets">指标目标</h2>
<table><thead><tr><th style="width:170px">指标</th><th style="width:130px">目标</th><th>含义</th></tr></thead>
<tbody>{targets}</tbody></table>

<h2 id="starter">起始代码</h2>
{codeblock("baseline.c", p["starterCode"])}

<h2 id="solution">参考解答</h2>
{codeblock("optimized.c", p["solutionCode"])}

<h2 id="report">优化报告</h2>
<table><thead><tr><th style="width:120px">维度</th><th>内容</th></tr></thead><tbody>
<tr><td><b>核心瓶颈</b></td><td>{esc(z["bottleneck"])}</td></tr>
<tr><td><b>一句话思路</b></td><td>{md_inline(z["one_liner"])}</td></tr>
<tr><td><b>报告摘要</b></td><td>{md_inline(rep["summary"])}</td></tr>
</tbody></table>

{"".join(secs)}

<div class="callout"><b>要点速记</b><ul style="margin:6px 0 0">{kp}</ul></div>
<div class="callout warn"><b>常见误区与边界条件</b><ul style="margin:6px 0 0">{pf}</ul></div>

<div class="pager">
  {f'<a href="{root}problems/{prev_p["slug"]}.html"><span class="k">← 上一题</span>{esc(ZH[prev_p["slug"]]["zh"])}</a>' if prev_p else "<span></span>"}
  {f'<a class="next" href="{root}problems/{next_p["slug"]}.html"><span class="k">下一题 →</span>{esc(ZH[next_p["slug"]]["zh"])}</a>' if next_p else "<span></span>"}
</div>
"""
    toc = ('<a href="#statement">题面</a><a href="#constraints">约束</a><a href="#examples">示例</a>'
           '<a href="#targets">指标目标</a><a href="#starter">起始代码</a><a href="#solution">参考解答</a>'
           '<a href="#report">优化报告</a>'
           + "".join(f'<a href="#{a}">· {esc(h)}</a>' for a, h in anchors))
    return layout(root=root, title=z["zh"], active=f"prob:{p['slug']}", body=body, toc=toc,
                  problems=problems, mod_of=mod_of, desc=z["one_liner"])


# ---------------------------------------------------------------- 构建
def build(out: Path) -> None:
    problems, by_slug, mod_of = load()
    # 只清理本生成器的已知产物，绝不 rmtree 整个 out：
    # docs/ 处于 git 版本控制下，误删整个目录会有丢数据风险。
    for name in ("modules", "problems", "assets"):
        d = out / name
        if d.exists():
            shutil.rmtree(d)
    for name in ("index.html", "modules.html", "problems.html"):
        f = out / name
        if f.exists():
            f.unlink()
    (out / "modules").mkdir(parents=True)
    (out / "problems").mkdir(parents=True)
    (out / "assets").mkdir(parents=True)
    # 禁用 Jekyll：否则 Pages 会忽略下划线开头的文件并拖慢构建
    (out / ".nojekyll").write_text("", encoding="utf-8")

    for f in ("style.css", "app.js"):
        shutil.copy(HERE / "static" / f, out / "assets" / f)

    # 搜索索引（同时内嵌为 JS，保证 file:// 直接打开也能用）
    index = []
    for mid, m in MODULES.items():
        index.append({"kind": "模块", "mod": m["zh"], "zh": m["zh"], "title": m["en"],
                      "bottleneck": "", "sum": m["tagline"], "url": f"modules/{mid}.html"})
    for p in problems:
        z = ZH[p["slug"]]
        index.append({"kind": "题目", "mod": MODULES[mod_of[p["slug"]]]["zh"],
                      "zh": z["zh"], "title": p["title"], "bottleneck": z["bottleneck"],
                      "sum": z["report"]["summary"], "url": f"problems/{p['slug']}.html"})
    (out / "assets" / "search.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "assets" / "search.js").write_text(
        "window.__INDEX__=" + json.dumps(index, ensure_ascii=False) + ";", encoding="utf-8")

    pages = {
        out / "index.html": page_index("", problems, mod_of),
        out / "modules.html": page_modules("", problems, mod_of),
        out / "problems.html": page_problems("", problems, mod_of),
    }
    for mid in MODULES:
        pages[out / "modules" / f"{mid}.html"] = page_module("../", mid, problems, mod_of)
    for i, p in enumerate(problems):
        prev_p = problems[i - 1] if i > 0 else None
        next_p = problems[i + 1] if i + 1 < len(problems) else None
        pages[out / "problems" / f"{p['slug']}.html"] = page_problem(
            "../", p, prev_p, next_p, problems, mod_of)

    for path, html_text in pages.items():
        path.write_text(html_text, encoding="utf-8")

    print(f"生成完成：{out}")
    print(f"  页面 {len(pages)} 个：首页 1 + 模块总览 1 + 题目总览 1 + 模块 {len(MODULES)} + 题目 {len(problems)}")
    print(f"  资源：assets/style.css, assets/app.js, assets/search.json, assets/search.js")
    return pages


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    # 默认输出到仓库根的 docs/ —— GitHub Pages 原生支持从 main 分支的 /docs 部署
    ap.add_argument("--out", default=str(HERE.parent / "docs"))
    a = ap.parse_args()
    build(Path(a.out).resolve())
