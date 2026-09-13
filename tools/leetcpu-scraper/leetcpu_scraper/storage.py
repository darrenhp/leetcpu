"""结果落盘：JSON / JSONL / CSV / Markdown 摘要。"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable


def _ensure(p: str | Path) -> Path:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def write_json(path: str | Path, obj: Any) -> Path:
    p = _ensure(path)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def write_jsonl(path: str | Path, rows: Iterable[dict]) -> Path:
    p = _ensure(path)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return p


def write_csv(path: str | Path, rows: list[dict], fieldnames: list[str] | None = None) -> Path:
    p = _ensure(path)
    if not rows:
        p.write_text("", encoding="utf-8")
        return p
    cols = fieldnames or list(rows[0].keys())
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


def write_text(path: str | Path, text: str) -> Path:
    p = _ensure(path)
    p.write_text(text, encoding="utf-8")
    return p


def render_markdown(snapshot) -> str:
    """生成人读的抓取摘要。"""
    meta = snapshot.static_meta
    rd = snapshot.rendered
    L = []
    L.append(f"# LeetCPU 抓取摘要\n")
    L.append(f"- 抓取时间：{snapshot.fetched_at}")
    L.append(f"- 目标 URL：{snapshot.url}")
    L.append(f"- 抓取模式：{snapshot.mode}")
    L.append(f"- 站点形态：{'SPA 空壳（需渲染）' if meta.get('is_spa_shell') else '静态 HTML'}")
    L.append(f"- 标题：{meta.get('title')}")
    L.append(f"- 描述：{meta.get('meta', {}).get('description')}\n")

    if rd.get("stats"):
        L.append("## 页面指标\n")
        L.append("| 数值 | 含义 |")
        L.append("| --- | --- |")
        for s in rd["stats"]:
            L.append(f"| {s['value']} | {s['label']} |")
        L.append("")

    L.append(f"## 题库（{len(snapshot.problems)} 题）\n")
    L.append("| # | Slug | 标题 | 难度 | 分类 | 仿真配置 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for p in snapshot.problems:
        L.append(f"| {p.id} | `{p.slug}` | {p.title} | {p.difficulty} | {p.category} | {p.sim_config} |")
    L.append("")

    if rd.get("headings"):
        L.append("## 渲染后标题结构\n")
        for h in rd["headings"]:
            L.append(f"{'  ' * (h['level'] - 1)}- {h['text']}")
        L.append("")

    if snapshot.warnings:
        L.append("## 告警\n")
        for w in snapshot.warnings:
            L.append(f"- ⚠️ {w}")
    return "\n".join(L) + "\n"
