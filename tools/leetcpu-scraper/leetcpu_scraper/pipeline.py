"""抓取主流程编排。

三层互补：
  L1 静态 HTML   → SEO 元数据 + 资源清单（快、稳、不依赖浏览器）
  L2 前端产物     → 内嵌的题库 / 课程数据集（结构化、字段最全）
  L3 渲染 DOM     → 真实可见文案、指标、链接（校验 L2，并兜住非内嵌内容）

L2 与 L3 的数量一致性会被显式交叉校验，任一层失效都会产生 warning 而不是静默丢数据。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import extractors as ex
from . import robots as rb
from .config import BASE_URL, Config
from .http_client import HttpClient, ScrapeError
from .models import Problem, Snapshot
from .renderer import RenderError, render
from .storage import (render_markdown, write_csv, write_json, write_jsonl,
                      write_text)

log = logging.getLogger("leetcpu")


class LeetCPUScraper:
    def __init__(self, cfg: Config | None = None, **overrides) -> None:
        self.cfg = cfg or Config(**overrides)
        self.client = HttpClient(self.cfg)
        self.warnings: list[str] = []
        self.snapshot: Snapshot | None = None

    # ---------------------------------------------------------------- 入口
    def run(self, url: str = BASE_URL, mode: str = "full") -> Snapshot:
        """mode: ``static`` | ``render`` | ``full``"""
        t0 = time.time()
        snap = Snapshot(
            url=url,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            mode=mode,
        )
        self._check_robots(url)

        # ---------- L1 静态层 ----------
        resp = self.client.get(url)
        html = resp.text
        if self.cfg.save_raw:
            write_text(Path(self.cfg.outdir) / "raw" / "index.html", html)
        snap.static_meta = ex.extract_static_meta(html, url)
        log.info("静态层：%d 字节，SPA 空壳=%s", len(html), snap.static_meta["is_spa_shell"])
        if not snap.static_meta["is_spa_shell"]:
            self.warnings.append("首页不再是 SPA 空壳，站点结构可能已改版，请复核抽取规则。")

        # ---------- L3 渲染层 ----------
        if mode in ("render", "full"):
            try:
                dom = render(url, self.cfg)
                if self.cfg.save_raw:
                    write_text(Path(self.cfg.outdir) / "raw" / "rendered.html", dom)
                snap.rendered = ex.extract_rendered_page(dom)
                log.info("渲染层：文本块 %d 个，指标 %d 项",
                         len(snap.rendered["text_blocks"]), len(snap.rendered["stats"]))
            except RenderError as e:
                self.warnings.append(f"渲染失败，已降级为静态模式：{e}")
                log.warning("渲染失败：%s", e)

        # ---------- L2 产物层 ----------
        if mode in ("static", "full"):
            self._harvest_bundle(snap, url)

        snap.problems = [Problem.from_raw(p) for p in snap.datasets.get("problems", [])]
        self._cross_check(snap)

        snap.warnings = self.warnings
        snap.stats = {
            "http": dict(self.client.stats),
            "elapsed_sec": round(time.time() - t0, 2),
            "n_problems": len(snap.problems),
            "n_dataset_kinds": len(snap.datasets),
            "rendered_text_length": snap.rendered.get("text_length", 0),
        }
        self.snapshot = snap
        return snap

    # ---------------------------------------------------------------- 步骤
    def _check_robots(self, url: str) -> None:
        if not self.cfg.obey_robots:
            return
        try:
            r = self.client.get(rb.robots_url(url), allow_404=True)
            if r.status != 200:
                self.warnings.append("robots.txt 不可达，按默认允许处理。")
                return
            rules = rb.parse_robots(r.text, self.cfg.user_agent)
            path = "/" + (url.split("://", 1)[-1].split("/", 1)[-1] if "/" in url.split("://", 1)[-1] else "")
            if not rules.can_fetch(path or "/"):
                raise ScrapeError(f"robots.txt 禁止抓取：{path}")
            if rules.crawl_delay:
                self.cfg.rate_limit.min_interval = max(
                    self.cfg.rate_limit.min_interval, float(rules.crawl_delay))
                log.info("遵循 robots.txt crawl-delay=%.1fs", rules.crawl_delay)
        except ScrapeError:
            raise
        except Exception as e:  # robots 不应阻断主流程
            self.warnings.append(f"robots.txt 解析异常（忽略）：{e}")

    def _harvest_bundle(self, snap: Snapshot, url: str) -> None:
        bundle = ex.pick_main_bundle(snap.static_meta)
        if not bundle:
            self.warnings.append("未能从 HTML 中定位主 JS 产物，跳过内嵌数据集抽取。")
            return
        try:
            js = self.client.get(bundle, referer=url).text
        except ScrapeError as e:
            self.warnings.append(f"下载 JS 产物失败：{e}")
            return
        if self.cfg.save_raw:
            write_text(Path(self.cfg.outdir) / "raw" / Path(bundle).name, js)
        snap.datasets = ex.extract_bundle_datasets(js)
        snap.static_meta["main_bundle"] = bundle
        log.info("产物层：%s（%d 字节），数据集 %s",
                 Path(bundle).name, len(js), {k: len(v) for k, v in snap.datasets.items()})
        ph = ex.count_placeholders(snap.datasets)
        if ph:
            self.warnings.append(
                f"数据集中有 {len(ph)} 处表达式无法静态求值（如 {ph[:3]}），已降级为占位符。")

    def _cross_check(self, snap: Snapshot) -> None:
        """渲染层指标 vs 内嵌数据条数的一致性校验。"""
        declared = None
        for s in snap.rendered.get("stats", []):
            if "challenge" in s["label"].lower():
                try:
                    declared = int(s["value"])
                except ValueError:
                    pass
        actual = len(snap.problems)
        if declared is not None and declared != actual:
            self.warnings.append(
                f"一致性校验不一致：页面宣称 {declared} 题，实际解析出 {actual} 题。")
        if actual == 0:
            self.warnings.append("未解析到任何题目数据，抓取结果不完整。")
        if snap.rendered and snap.rendered.get("text_length", 0) < 500:
            self.warnings.append("渲染后正文过短，可能未等到前端渲染完成。")

    # ---------------------------------------------------------------- 落盘
    def save(self, snap: Snapshot | None = None) -> dict[str, Path]:
        snap = snap or self.snapshot
        assert snap is not None, "尚未执行 run()"
        out = Path(self.cfg.outdir)
        paths: dict[str, Path] = {}
        paths["snapshot"] = write_json(out / "snapshot.json", snap.to_dict())
        paths["problems"] = write_json(out / "problems.json", [p.raw for p in snap.problems])
        paths["problems_csv"] = write_csv(
            out / "problems.csv", [p.flat() for p in snap.problems])
        paths["problems_jsonl"] = write_jsonl(
            out / "problems.jsonl", [p.raw for p in snap.problems])
        if snap.rendered:
            paths["page_text"] = write_text(
                out / "page_text.txt", "\n".join(snap.rendered["text_blocks"]))
        for kind, rows in snap.datasets.items():
            if kind != "problems" and rows:
                paths[f"dataset_{kind}"] = write_json(out / f"dataset_{kind}.json", rows)
        paths["summary"] = write_text(out / "SUMMARY.md", render_markdown(snap))
        return paths


# ---------------------------------------------------------------- 便捷函数
def scrape(url: str = BASE_URL, mode: str = "full", outdir: str | None = None, **kw) -> Snapshot:
    cfg = Config(outdir=outdir) if outdir else Config(**kw)
    s = LeetCPUScraper(cfg)
    snap = s.run(url=url, mode=mode)
    s.save(snap)
    return snap
