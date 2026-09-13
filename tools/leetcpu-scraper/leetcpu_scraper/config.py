"""全局配置与抓取策略参数。

所有"反爬对抗"相关的旋钮集中在此，便于按站点反馈快速调整。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_URL = "https://www.leetcpu.com/"
ORIGIN = "https://www.leetcpu.com"

# 默认输出根目录（可用环境变量 LEETCPU_OUTDIR 覆盖）
DEFAULT_OUTDIR = os.environ.get(
    "LEETCPU_OUTDIR",
    str(Path(__file__).resolve().parent.parent / "output"),
)

# ---------------------------------------------------------------- 请求头
# 站点托管在 Vercel，未发现 WAF；但仍按真实浏览器补齐请求头，避免被
# 基于 Header 指纹的廉价规则拦截。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
    "DNT": "1",
    "Connection": "keep-alive",
}

# ---------------------------------------------------------------- 限流 / 重试
@dataclass
class RateLimit:
    """令牌桶 + 最小间隔 + 抖动。

    Vercel 边缘节点对单 IP 突发请求敏感；默认 1.2s 最小间隔足够安全，
    且对只有个位数量级请求的本站点几乎无性能损失。
    """

    min_interval: float = 1.2          # 两次请求之间的最小间隔（秒）
    jitter: float = 0.6                # 附加随机抖动上限（秒），避免节律化
    burst: int = 4                     # 令牌桶容量（允许的突发）
    rate: float = 1.0 / 1.2            # 令牌补充速率（个/秒）


@dataclass
class Retry:
    max_attempts: int = 4              # 总尝试次数（含首次）
    backoff_base: float = 1.5          # 指数退避底数
    backoff_cap: float = 30.0          # 单次退避上限（秒）
    retry_status: tuple = (408, 425, 429, 500, 502, 503, 504)
    respect_retry_after: bool = True   # 优先遵循 Retry-After
    retry_after_cap: float = 60.0


@dataclass
class RenderConfig:
    """无头浏览器渲染参数（站点为 Vite SPA，必须渲染才能拿到 DOM）。"""

    enabled: bool = True
    virtual_time_budget_ms: int = 12000   # 等待前端异步渲染的虚拟时间
    timeout: int = 90                     # 子进程总超时（秒）
    extra_wait_ms: int = 1500
    chrome_paths: tuple = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "google-chrome",
        "chromium",
        "chromium-browser",
    )


@dataclass
class Config:
    outdir: str = DEFAULT_OUTDIR
    timeout: float = 25.0
    max_body_bytes: int = 40 * 1024 * 1024   # 单响应体积上限，防内存打爆
    use_cache: bool = True                   # ETag / Last-Modified 条件请求
    obey_robots: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    rate_limit: RateLimit = field(default_factory=RateLimit)
    retry: Retry = field(default_factory=Retry)
    render: RenderConfig = field(default_factory=RenderConfig)
    cache_dir: str = ""
    save_raw: bool = True                    # 保存原始 HTML/JS 快照，便于回溯

    def __post_init__(self) -> None:
        self.outdir = str(Path(self.outdir).expanduser())
        self.cache_dir = self.cache_dir or str(Path(self.outdir) / "_cache")
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
        Path(self.outdir).mkdir(parents=True, exist_ok=True)

    def headers(self, referer: str | None = None, json_api: bool = False) -> dict[str, str]:
        h = dict(DEFAULT_HEADERS)
        h["User-Agent"] = self.user_agent
        if referer:
            h["Referer"] = referer
            h["Sec-Fetch-Site"] = "same-origin"
        if json_api:
            h["Accept"] = "application/json,text/plain,*/*"
            h["Sec-Fetch-Dest"] = "empty"
            h["Sec-Fetch-Mode"] = "cors"
        return h
