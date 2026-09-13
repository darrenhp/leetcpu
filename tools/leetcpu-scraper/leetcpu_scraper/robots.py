"""robots.txt 解析与合规判定（最小可用实现）。"""
from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field


@dataclass
class RobotsRules:
    allows: list[str] = field(default_factory=list)
    disallows: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    fetched: bool = False

    def can_fetch(self, path: str, user_agent: str = "*") -> bool:
        if not self.fetched:
            return True  # 拿不到 robots 时不阻断（ Beer 标准做法：视为允许）
        q = path or "/"
        best: tuple[int, bool] = (0, True)  # (匹配长度, 是否允许)
        for rule in self.disallows:
            if rule and q.startswith(rule):
                if len(rule) > best[0]:
                    best = (len(rule), False)
        for rule in self.allows:
            if rule and q.startswith(rule):
                if len(rule) > best[0]:
                    best = (len(rule), True)
        return best[1]


def parse_robots(text: str, user_agent: str = "*") -> RobotsRules:
    rules = RobotsRules(fetched=True)
    applies = False
    lines = [l.strip() for l in text.splitlines()]
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            applies = (v == "*") or (user_agent.lower().startswith(v.lower()))
        elif not applies:
            continue
        elif k == "allow":
            rules.allows.append(v)
        elif k == "disallow":
            rules.disallows.append(v)
        elif k == "crawl-delay":
            try:
                rules.crawl_delay = float(v)
            except ValueError:
                pass
        elif k == "sitemap":
            rules.sitemaps.append(v)
    # Disallow: "" 等价于允许全部
    rules.disallows = [d for d in rules.disallows if d]
    return rules


def robots_url(site: str) -> str:
    p = urllib.parse.urlsplit(site)
    return f"{p.scheme}://{p.netloc}/robots.txt"
