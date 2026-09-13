"""无头浏览器渲染层（SPA 必需）。

leetcpu.com 的正文全部由 ``/assets/index-*.js`` 在浏览器里生成，
``curl`` 拿到的 HTML 只有一个空的 ``<div id="root">``，因此必须渲染。

实现选择：直接驱动本机 Chrome / Edge / Chromium 的 ``--headless=new --dump-dom``。
相比 Playwright：
  * 零原生扩展依赖（规避 macOS 上 greenlet `.so` 代码签名不匹配问题）；
  * 不需要下载/维护浏览器内核；
  * 对本场景（只读 DOM、无需交互）能力等价。
需要点击、滚动、等待 XHR 等交互时，可开启下面的 CDP 通道。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import Config, RenderConfig


class RenderError(RuntimeError):
    pass


def find_chrome(cfg: RenderConfig | Config) -> str:
    rc = cfg.render if isinstance(cfg, Config) else cfg
    for p in rc.chrome_paths:
        if os.path.isabs(p):
            if Path(p).exists():
                return p
        else:
            found = shutil.which(p)
            if found:
                return found
    raise RenderError(
        "未找到 Chrome/Chromium。请安装 Google Chrome，或设置环境变量 CHROME_PATH，"
        "或在 Config.render.chrome_paths 中补充路径。"
    )


def render(url: str, cfg: Config, *, attempts: int = 2) -> str:
    """渲染 ``url`` 并返回完整 DOM 字符串。"""
    rc = cfg.render
    if not rc.enabled:
        raise RenderError("渲染已禁用（Config.render.enabled=False）")
    env_path = os.environ.get("CHROME_PATH")
    chrome = env_path if env_path and Path(env_path).exists() else find_chrome(cfg)

    last = ""
    for k in range(attempts):
        budget = rc.virtual_time_budget_ms * (k + 1)
        profile = tempfile.mkdtemp(prefix="leetcpu-chrome-")
        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-scrollbars",
            "--window-size=1440,2400",
            f"--user-agent={cfg.user_agent}",
            f"--user-data-dir={profile}",
            f"--virtual-time-budget={budget}",
            "--dump-dom",
            url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=rc.timeout)
            dom = proc.stdout.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            dom = ""
        finally:
            shutil.rmtree(profile, ignore_errors=True)

        if dom.strip():
            last = dom
            # 空壳检测：root 里没有实质内容就加大虚拟时间重试
            if len(dom) > 6000 and _root_has_content(dom):
                if rc.extra_wait_ms:
                    time.sleep(rc.extra_wait_ms / 1000.0)
                return dom
    if last:
        return last
    raise RenderError(f"渲染失败：{url}")


def _root_has_content(dom: str) -> bool:
    i = dom.find('id="root"')
    if i < 0:
        return len(dom) > 6000
    seg = dom[i:i + 4000]
    return len(seg.replace(" ", "")) > 800
