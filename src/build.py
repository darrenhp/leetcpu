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

# 扩展阅读：Markdown 源文件目录（新增文章只需往这里丢 .md）
READING_DIR = HERE / "content" / "reading"

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
    """支持 `code`、`**bold**`、`*italic*` 与 [citation:N] 来源标注。

    行内代码先被摘出暂存，避免代码块里的 `*` / `[...]`（如 `float *A`）
    被后续的斜体或角标规则误伤。
    """
    out = html.escape(str(s), quote=False)

    codes: list[str] = []

    def _stash(m: "re.Match[str]") -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    out = re.sub(r"`([^`]+)`", _stash, out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", out)
    # 斜体：定界 * 必须紧邻非单词字符，且内容首尾不留空白。
    # 这样 `arr[i*n*n + j*n + k]` 这类乘法表达式不会被误判成斜体。
    out = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"\[citation:(\d+)\]", r'<sup class="cite">[\1]</sup>', out)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: f"<code>{codes[int(m.group(1))]}</code>", out)


def slug_anchor(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "sec"


def heading_anchor(text: str, seq: int, used: set) -> str:
    """生成非空且不重复的标题锚点。

    slug_anchor() 会把中文整段吞掉并退化成 "sec"，多个中文标题会撞车；
    这里退化为 "sec-<序号>"，保证任意语言的标题都有唯一锚点。
    """
    base = slug_anchor(text)
    if not base or base == "sec" or base in used:
        base = f"sec-{seq}"
    while base in used:                       # 极端兜底：仍冲突则递增序号
        seq += 1
        base = f"sec-{seq}"
    used.add(base)
    return base


def codeblock(label: str, code: str, note: str = "") -> str:
    return f"""<div class="codeblock">
  <div class="codebar"><span class="label">{esc(label)}</span><button class="copybtn">复制</button></div>
  <pre data-code="{html.escape(code, quote=True)}"></pre>
</div>""" + (f'<p class="sub">{md_inline(note)}</p>' if note else "")


# ---------------------------------------------------------------- Markdown
# 极简 Markdown 渲染器（纯标准库）。只覆盖本站文章实际用到的语法，
# 目标是可预期、容错（孤立 ">"、未闭合围栏、缩进列表都不抛异常）。
RE_FENCE = re.compile(r"^```")
RE_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
RE_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
RE_ULI = re.compile(r"^[-*+]\s+")
RE_OLI = re.compile(r"^\d+[.)]\s+")
RE_BQ = re.compile(r"^\s*>\s?")
RE_TABLE_SEP = re.compile(r"^\|?[\s:|-]*-[\s:|-]*$")


def _split_cells(line: str) -> list:
    """切分 GFM 管道表格的一行。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _render_table(header: list, rows: list) -> str:
    """渲染表格；行数不足表头时补空单元格，保证 HTML 结构完整。"""
    th = "".join(f"<th>{md_inline(c)}</th>" for c in header)
    trs = []
    for r in rows:
        cells = list(r) + [""] * (len(header) - len(r))
        trs.append("<tr>" + "".join(f"<td>{md_inline(c)}</td>" for c in cells) + "</tr>")
    return (f'<table><thead><tr>{th}</tr></thead><tbody>'
            f'{"".join(trs)}</tbody></table>')


def _md_blocks(lines: list, ctx: dict) -> str:
    """把 Markdown 行序列渲染成 HTML 片段。"""
    out: list = []
    i, n = 0, len(lines)

    def _is_block_start(s: str) -> bool:
        return bool(s and (RE_FENCE.match(s) or RE_HEADING.match(s) or RE_HR.match(s)
                           or s.startswith("|") or s.startswith(">")
                           or RE_ULI.match(s) or RE_OLI.match(s)))

    while i < n:
        line = lines[i]
        s = line.strip()

        if not s:                                          # 空行
            i += 1
            continue

        if RE_FENCE.match(s):                              # 围栏代码块
            lang = s[3:].strip() or "text"
            buf = []
            i += 1
            while i < n and not RE_FENCE.match(lines[i].strip()):
                buf.append(lines[i])
                i += 1
            i += 1                                         # 跳过收尾围栏（缺失也容错）
            out.append(codeblock(lang, "\n".join(buf)))
            continue

        m = RE_HEADING.match(s)                            # ATX 标题
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            ctx["seq"] += 1
            anchor = heading_anchor(text, ctx["seq"], ctx["used"])
            out.append(f'<h{level} id="{anchor}">{md_inline(text)}</h{level}>')
            if level in (2, 3):
                ctx["toc"].append((level, text, anchor))
            i += 1
            continue

        if RE_HR.match(s):                                 # 水平分隔线
            out.append("<hr>")
            i += 1
            continue

        # GFM 管道表格：当前行以 | 开头，且下一行是 |---| 分隔行
        if (s.startswith("|") and i + 1 < n
                and lines[i + 1].strip().startswith("|")
                and RE_TABLE_SEP.match(lines[i + 1].strip())):
            header = _split_cells(s)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_split_cells(lines[i]))
                i += 1
            out.append(_render_table(header, rows))
            continue

        if s.startswith(">"):                              # 引用块：连续行合并
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(RE_BQ.sub("", lines[i], count=1))
                i += 1
            out.append("<blockquote>" + _md_blocks(buf, ctx) + "</blockquote>")
            continue

        if RE_ULI.match(s) or RE_OLI.match(s):             # 列表
            ordered = bool(RE_OLI.match(s))
            pat = RE_OLI if ordered else RE_ULI
            items = []
            while i < n:
                ls = lines[i].strip()
                if pat.match(ls):
                    items.append(pat.sub("", ls, count=1))
                    i += 1
                elif not ls:
                    # 松散列表：项之间夹着空行时，若空行后仍是同类列表项则继续合并，
                    # 否则拆成多段 <ol>/<ul> 会让编号从 1 重新开始。
                    j = i
                    while j < n and not lines[j].strip():
                        j += 1
                    if j < n and pat.match(lines[j].strip()):
                        i = j
                    else:
                        break
                elif items and not _is_block_start(ls):
                    items[-1] += " " + ls                  # 续行并入上一项
                    i += 1
                else:
                    break
            tag = "ol" if ordered else "ul"
            out.append(f'<{tag}>' + "".join(f"<li>{md_inline(x)}</li>" for x in items)
                       + f"</{tag}>")
            continue

        buf = []                                           # 段落
        while i < n:
            ls = lines[i].strip()
            if not ls or _is_block_start(ls):
                break
            buf.append(ls)
            i += 1
        if buf:
            out.append("<p>" + md_inline(" ".join(buf)) + "</p>")
            continue

        i += 1

    return "\n".join(out)


def md_render(text: str, toc: list = None) -> str:
    """把 Markdown 文本渲染成 HTML。

    Args:
        text: Markdown 正文（不含 front matter）。
        toc: 可选的可变列表；传入时会收集 (层级, 标题, 锚点) 用于生成目录。

    Returns:
        渲染后的 HTML 字符串。
    """
    ctx: dict = {"seq": 0, "used": set(), "toc": toc if toc is not None else []}
    return _md_blocks(text.splitlines(), ctx)


def parse_front_matter(text: str) -> tuple:
    """解析文件头部由 --- 包裹的 `key: value` front matter。

    Returns:
        (meta 字典, 去掉 front matter 后的正文)。没有 front matter 时返回 ({}, 原文)。
    """
    lines = text.splitlines()
    meta: dict = {}
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                for raw in lines[1:i]:
                    if ":" in raw:
                        k, v = raw.split(":", 1)
                        meta[k.strip()] = v.strip()
                return meta, "\n".join(lines[i + 1:]).lstrip("\n")
    return meta, text


def _fm_tags(value: str) -> list:
    """把 front matter 里的 `[a, b, c]` 解析成标签列表。"""
    v = (value or "").strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [x.strip() for x in v.split(",") if x.strip()]


def _fm_order(value: str) -> "int | None":
    """front matter 里的 order 字段；缺失或非法时返回 None（走 date 兜底）。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def load_reading() -> list:
    """读取 src/content/reading/*.md，返回排好序的文章列表。

    排序规则：有 order 的按 order 升序在前；无 order 的排在其后、按 date 降序；
    同分时再用 slug 兜底，保证顺序与文件系统遍历顺序无关、完全确定。

    每篇文章形如 {"slug","title","module","date","order","summary","tags","html","toc"}。
    新增文章只要往该目录丢一个带 front matter 的 .md 即可，无需改代码。
    """
    articles: list = []
    if not READING_DIR.is_dir():
        return articles
    for path in sorted(READING_DIR.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        toc: list = []
        articles.append({
            "slug": path.stem,
            "title": meta.get("title") or path.stem,
            "module": meta.get("module", "").strip(),
            "date": meta.get("date", "").strip(),
            "order": _fm_order(meta.get("order")),
            "summary": meta.get("summary", "").strip(),
            "tags": _fm_tags(meta.get("tags", "")),
            "html": md_render(body, toc),
            "toc": toc,
        })
    # 稳定排序：从最次要的键开始排，最后按 (有无 order, order) 排
    articles.sort(key=lambda a: a["slug"])
    articles.sort(key=lambda a: a["date"], reverse=True)
    articles.sort(key=lambda a: (a["order"] is None,
                                 a["order"] if a["order"] is not None else 0))
    return articles


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
             f'<a href="{root}reading.html" class="{_on(active, "reading")}">扩展阅读</a>',
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
        ("reading", "扩展阅读", f"{root}reading.html"),
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
    <input id="q" type="search" placeholder="搜索题目 / 模块 / 文章…" autocomplete="off">
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
def _mod_badge(root: str, module_id: str, link: bool = True) -> str:
    """渲染所属模块徽章；module 为空或未知时返回空串（容错）。"""
    m = MODULES.get(module_id or "")
    if not m:
        return ""
    style = f' style="background:{m["color"]}"'
    if not link:
        return f'<span class="badge mod"{style}>{esc(m["zh"])}</span>'
    return (f'<a class="badge mod"{style} href="{root}modules/{module_id}.html">'
            f'{esc(m["zh"])}</a>')


def _tag_html(tags: list) -> str:
    return "".join(f'<span class="tag">{esc(t)}</span>' for t in tags)


def _reading_cards(root: str, articles: list) -> str:
    """渲染扩展阅读卡片列表（首页与扩展阅读页共用）。"""
    if not articles:
        return '<p class="sub">暂无扩展阅读文章。</p>'
    cards = []
    for a in articles:
        tags = _tag_html(a["tags"])
        meta = esc(a["date"]) + (f' · {tags}' if tags else "")
        cards.append(
            f'<a class="card" href="{root}reading/{a["slug"]}.html">'
            f'{_mod_badge(root, a["module"], link=False)}'
            f'<h3>{esc(a["title"])}</h3>'
            f'<p>{esc(a["summary"])}</p>'
            f'<p class="sub" style="margin-top:8px">{meta}</p></a>')
    return f'<div class="grid g2">{"".join(cards)}</div>'


def page_index(root, problems, mod_of, articles) -> str:
    stats = "".join(
        f'<div class="stat"><b>{v}</b><span>{t}</span></div>'
        for v, t in [("22", "道实战题目"), ("8", "个知识模块"),
                     ("12", "项性能指标"), (str(len(articles)), "篇扩展阅读")])
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

<h2 id="reading">扩展阅读</h2>
<p class="sub">比题目页更长、更完整的专题文章，讲透一个主题来龙去脉。</p>
{_reading_cards(root, articles)}
"""
    toc = "".join(f'<a href="#{i}">{t}</a>' for i, t in
                  [("how", "怎么用这个站"), ("overview", "题目总览"),
                   ("modules", "知识模块"), ("reading", "扩展阅读")])
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


def _module_reading(root: str, mid: str, articles: list) -> str:
    """知识模块页底部的「扩展阅读」区块；无关联文章时返回空串。"""
    subs = [a for a in articles if a["module"] == mid]
    if not subs:
        return ""
    rows = "".join(
        f'<tr><td><a href="{root}reading/{a["slug"]}.html">{esc(a["title"])}</a></td>'
        f'<td class="sub">{esc(a["date"])}</td>'
        f'<td>{esc(a["summary"])}</td></tr>' for a in subs)
    return (f'<h2 id="reading">扩展阅读</h2>'
            f'<p class="sub">与本模块相关的专题长文。</p>'
            f'<table><thead><tr><th style="width:240px">文章</th>'
            f'<th style="width:96px">日期</th><th>摘要</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


def page_module(root, mid, problems, mod_of, articles) -> str:
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

{_module_reading(root, mid, articles)}
"""
    toc = (f'<a href="#concepts">概念讲解</a>'
           + "".join(f'<a href="#{a}">{esc(h)}</a>' for a, h in anchors)
           + '<a href="#metrics">关键指标</a><a href="#problems">配套题目</a>'
           + ('<a href="#reading">扩展阅读</a>' if any(a["module"] == mid for a in articles) else ""))
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


def page_reading(root, articles, problems, mod_of) -> str:
    body = f"""
<h1>扩展阅读</h1>
<p class="lead">比题目页更长、更完整的专题文章：把一个主题的来龙去脉、算法演进与各家实现一次讲透。</p>
<p class="sub">共 {len(articles)} 篇。</p>
{_reading_cards(root, articles)}
"""
    return layout(root=root, title="扩展阅读", active="reading", body=body, toc="",
                  problems=problems, mod_of=mod_of,
                  desc="扩展阅读：分支预测、缓存层次等 CPU 微架构专题长文。")


def page_article(root, art, problems, mod_of) -> str:
    """文章页。标题由正文的 H1 提供，这里不再重复输出一个 H1。"""
    m = MODULES.get(art["module"] or "")
    tags = _tag_html(art["tags"])
    toc = "".join(f'<a href="#{a}" class="lv{lv}">{esc(t)}</a>'
                  for lv, t, a in art["toc"])
    body = f"""
<div class="meta">
  <span class="badge">扩展阅读</span>
  {_mod_badge(root, art["module"])}
  <span class="sub">{esc(art["date"])}</span>
</div>
{art["html"]}
{f'<p class="tags">{tags}</p>' if tags else ""}
<div class="pager">
  <a href="{root}reading.html"><span class="k">← 返回</span>扩展阅读</a>
  {f'<a class="next" href="{root}modules/{art["module"]}.html"><span class="k">所属模块 →</span>{esc(m["zh"])}</a>' if m else "<span></span>"}
</div>
"""
    return layout(root=root, title=art["title"], active=f"art:{art['slug']}",
                  body=body, toc=toc, problems=problems, mod_of=mod_of,
                  desc=art["summary"])


# ---------------------------------------------------------------- 构建
def build(out: Path) -> None:
    problems, by_slug, mod_of = load()
    articles = load_reading()
    # 只清理本生成器的已知产物，绝不 rmtree 整个 out：
    # docs/ 处于 git 版本控制下，误删整个目录会有丢数据风险。
    for name in ("modules", "problems", "reading", "assets"):
        d = out / name
        if d.exists():
            shutil.rmtree(d)
    for name in ("index.html", "modules.html", "problems.html", "reading.html"):
        f = out / name
        if f.exists():
            f.unlink()
    (out / "modules").mkdir(parents=True)
    (out / "problems").mkdir(parents=True)
    (out / "reading").mkdir(parents=True)
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
    for a in articles:
        m = MODULES.get(a["module"] or "")
        index.append({"kind": "文章", "mod": m["zh"] if m else "", "zh": a["title"],
                      "title": a["title"], "bottleneck": "", "sum": a["summary"],
                      "url": f"reading/{a['slug']}.html"})
    (out / "assets" / "search.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "assets" / "search.js").write_text(
        "window.__INDEX__=" + json.dumps(index, ensure_ascii=False) + ";", encoding="utf-8")

    pages = {
        out / "index.html": page_index("", problems, mod_of, articles),
        out / "modules.html": page_modules("", problems, mod_of),
        out / "problems.html": page_problems("", problems, mod_of),
        out / "reading.html": page_reading("", articles, problems, mod_of),
    }
    for mid in MODULES:
        pages[out / "modules" / f"{mid}.html"] = page_module(
            "../", mid, problems, mod_of, articles)
    for i, p in enumerate(problems):
        prev_p = problems[i - 1] if i > 0 else None
        next_p = problems[i + 1] if i + 1 < len(problems) else None
        pages[out / "problems" / f"{p['slug']}.html"] = page_problem(
            "../", p, prev_p, next_p, problems, mod_of)
    for a in articles:
        pages[out / "reading" / f"{a['slug']}.html"] = page_article(
            "../", a, problems, mod_of)

    for path, html_text in pages.items():
        path.write_text(html_text, encoding="utf-8")

    print(f"生成完成：{out}")
    print(f"  页面 {len(pages)} 个：首页 1 + 模块总览 1 + 题目总览 1 + 扩展阅读 1 "
          f"+ 模块 {len(MODULES)} + 题目 {len(problems)} + 文章 {len(articles)}")
    print(f"  资源：assets/style.css, assets/app.js, assets/search.json, assets/search.js")
    return pages


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    # 默认输出到仓库根的 docs/ —— GitHub Pages 原生支持从 main 分支的 /docs 部署
    ap.add_argument("--out", default=str(HERE.parent / "docs"))
    a = ap.parse_args()
    build(Path(a.out).resolve())
