"""LeetCPU 抓取器。

    from leetcpu_scraper import scrape
    snap = scrape(outdir="./output")
    print(len(snap.problems))
"""
from __future__ import annotations

from .config import BASE_URL, Config
from .models import Problem, Snapshot
from .pipeline import LeetCPUScraper, scrape

__version__ = "1.0.0"
__all__ = ["scrape", "LeetCPUScraper", "Config", "Snapshot", "Problem", "BASE_URL"]
