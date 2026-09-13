"""命令行入口。

用法示例：
    python -m leetcpu_scraper.cli --mode full --outdir ./output
    python -m leetcpu_scraper.cli --mode static --no-render
    python -m leetcpu_scraper.cli --min-interval 3 --print-summary
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import BASE_URL, Config
from .pipeline import LeetCPUScraper


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leetcpu-scraper",
        description="抓取 https://www.leetcpu.com/ （Vite SPA）的核心数据",
    )
    p.add_argument("--url", default=BASE_URL, help="起始 URL")
    p.add_argument("--mode", choices=["static", "render", "full"], default="full",
                   help="static=仅 HTML+产物；render=仅渲染；full=三层全量（默认）")
    p.add_argument("--outdir", default=None, help="输出目录")
    p.add_argument("--min-interval", type=float, default=None,
                   help="请求最小间隔（秒），越大越安全，默认 1.2")
    p.add_argument("--timeout", type=float, default=None, help="单请求超时（秒）")
    p.add_argument("--user-agent", default=None, help="自定义 UA")
    p.add_argument("--no-cache", action="store_true", help="禁用 ETag 条件请求缓存")
    p.add_argument("--no-render", action="store_true", help="禁用无头浏览器渲染")
    p.add_argument("--ignore-robots", action="store_true", help="不检查 robots.txt")
    p.add_argument("--virtual-time", type=int, default=None,
                   help="渲染虚拟时间预算（毫秒），页面复杂时调大")
    p.add_argument("--print-summary", action="store_true", help="终端打印摘要")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config(outdir=args.outdir) if args.outdir else Config()
    if args.min_interval is not None:
        cfg.rate_limit.min_interval = args.min_interval
    if args.timeout is not None:
        cfg.timeout = args.timeout
    if args.user_agent:
        cfg.user_agent = args.user_agent
    if args.no_cache:
        cfg.use_cache = False
    if args.no_render:
        cfg.render.enabled = False
    if args.ignore_robots:
        cfg.obey_robots = False
    if args.virtual_time is not None:
        cfg.render.virtual_time_budget_ms = args.virtual_time

    mode = args.mode
    if cfg.render.enabled is False and mode == "full":
        mode = "static"

    scraper = LeetCPUScraper(cfg)
    try:
        snap = scraper.run(url=args.url, mode=mode)
    except Exception as e:  # noqa: BLE001
        logging.error("抓取失败：%s", e)
        return 2

    paths = scraper.save(snap)
    print("\n=== 抓取完成 ===")
    print(f"题目数：{len(snap.problems)}    数据集：{list(snap.datasets)}")
    print(f"HTTP：{snap.stats['http']}    耗时：{snap.stats['elapsed_sec']}s")
    for k, v in paths.items():
        print(f"  {k:16s} -> {v}")
    if snap.warnings:
        print("\n告警：")
        for w in snap.warnings:
            print(f"  ⚠️ {w}")
    if args.print_summary:
        print("\n" + json.dumps(
            {k: snap.static_meta.get(k) for k in ("title", "meta", "og")},
            ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
