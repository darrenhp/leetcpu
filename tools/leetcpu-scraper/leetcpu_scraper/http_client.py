"""HTTP 传输层：Cookie 会话 + 限流 + 指数退避重试 + 条件请求缓存 + 软拦截识别。

只用标准库实现，避免 greenlet / 原生扩展在部分 macOS 环境下的签名问题。
"""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

CACHE_VERSION = "v1"


class ScrapeError(RuntimeError):
    """抓取失败（含反爬拦截）。"""


class RobotsDenied(ScrapeError):
    pass


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    @property
    def text(self) -> str:
        enc = "utf-8"
        m = re.search(r'charset=([\w\-]+)', self.headers.get("content-type", ""), re.I)
        if m:
            enc = m.group(1)
        return self.body.decode(enc, errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.body)
        return p


class TokenBucket:
    """令牌桶 + 最小间隔 + 抖动，抗频率限制。"""

    def __init__(self, cfg: Config):
        rl = cfg.rate_limit
        self.min_interval = rl.min_interval
        self.jitter = rl.jitter
        self.capacity = rl.burst
        self.rate = rl.rate
        self._tokens = float(rl.burst)
        self._last = 0.0  # 上次请求时间

    def acquire(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)
            now = time.monotonic()
            self._tokens = 1.0
        gap = time.monotonic() - self._last
        if self._last and gap < self.min_interval:
            time.sleep(self.min_interval - gap + random.uniform(0, self.jitter))
        self._tokens -= 1.0
        self._last = time.monotonic()


class HttpClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bucket = TokenBucket(cfg)
        self.cookiejar = http.cookiejar.CookieJar()
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0, "bytes": 0}
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar),
            _NoRedirectIfNeeded(),
        )

    # ------------------------------------------------------------ 缓存
    def _meta_path(self, url: str) -> Path:
        key = re.sub(r'[^A-Za-z0-9]+', '_', url)[-120:]
        return Path(self.cfg.cache_dir) / f"{key}.{CACHE_VERSION}.meta.json"

    def _body_path(self, url: str) -> Path:
        key = re.sub(r'[^A-Za-z0-9]+', '_', url)[-120:]
        return Path(self.cfg.cache_dir) / f"{key}.{CACHE_VERSION}.body"

    def _load_meta(self, url: str) -> dict:
        p = self._meta_path(url)
        return json.loads(p.read_text()) if p.exists() else {}

    def _store_meta(self, url: str, meta: dict) -> None:
        self._meta_path(url).write_text(json.dumps(meta))

    # ------------------------------------------------------------ 核心
    def get(self, url: str, *, referer: str | None = None,
            json_api: bool = False, allow_404: bool = False) -> Response:
        last_err: Exception | None = None
        for attempt in range(1, self.cfg.retry.max_attempts + 1):
            self.bucket.acquire()
            headers = self.cfg.headers(referer=referer, json_api=json_api)
            meta = self._load_meta(url) if self.cfg.use_cache else {}
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

            req = urllib.request.Request(url, headers=headers, method="GET")
            self.stats["requests"] += 1
            try:
                with self._opener.open(req, timeout=self.cfg.timeout) as resp:
                    raw = resp.read(self.cfg.max_body_bytes)
                    hdrs = {k.lower(): v for k, v in resp.getheaders()}
                    status = resp.status
                    body = _decode_body(raw, hdrs.get("content-encoding", ""))
                    final_url = resp.geturl()
            except urllib.error.HTTPError as e:
                hdrs = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
                status = e.code
                raw = e.read(self.cfg.max_body_bytes)
                body = _decode_body(raw, hdrs.get("content-encoding", ""))
                final_url = url
                if status == 304 and meta.get("body"):
                    self.stats["cache_hits"] += 1
                    return Response(url, 200, Path(meta["body"]).read_bytes(),
                                    meta.get("headers", {}), from_cache=True)
                if not self._should_retry(status) or attempt >= self.cfg.retry.max_attempts:
                    if allow_404 and status in (403, 404):
                        return Response(final_url, status, body, hdrs)
                    raise ScrapeError(_describe_block(url, status, hdrs, body))
                self._sleep_backoff(attempt, hdrs.get("retry-after"))
                last_err = e
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt >= self.cfg.retry.max_attempts:
                    raise ScrapeError(f"网络错误 {url}: {e}") from e
                self._sleep_backoff(attempt, None)
                last_err = e
                continue

            if status >= 400:
                if not self._should_retry(status) or attempt >= self.cfg.retry.max_attempts:
                    if allow_404 and status in (403, 404):
                        return Response(final_url, status, body, hdrs)
                    raise ScrapeError(_describe_block(url, status, hdrs, body))
                self._sleep_backoff(attempt, hdrs.get("retry-after"))
                continue

            self.stats["bytes"] += len(body)
            if self.cfg.use_cache:
                bp = self._body_path(url)
                bp.write_bytes(body)
                self._store_meta(url, {
                    "etag": hdrs.get("etag"),
                    "last_modified": hdrs.get("last-modified"),
                    "headers": hdrs, "body": str(bp), "url": url,
                })
            return Response(final_url, status, body, hdrs)

        raise ScrapeError(f"请求失败 {url}: {last_err}")

    # ------------------------------------------------------------ 辅助
    def _should_retry(self, status: int) -> bool:
        return status in self.cfg.retry.retry_status

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        self.stats["retries"] += 1
        delay = min(self.cfg.retry.backoff_cap,
                    self.cfg.retry.backoff_base ** attempt)
        if self.cfg.retry.respect_retry_after and retry_after:
            try:
                delay = max(delay, min(float(retry_after), self.cfg.retry.retry_after_cap))
            except ValueError:
                pass
        time.sleep(delay + random.uniform(0, 0.4))

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.cookiejar)


class _NoRedirectIfNeeded(urllib.request.HTTPRedirectHandler):
    """保留默认重定向行为，仅补充 referer 传递。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.add_header("Referer", req.full_url)
        return new


def _decode_body(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if "br" in enc:
            try:
                import brotli  # type: ignore
                return brotli.decompress(raw)
            except Exception:
                return raw
    except Exception:
        return raw
    return raw


def _describe_block(url: str, status: int, hdrs: dict, body: bytes) -> str:
    """把常见的软/硬拦截翻译成可读原因。"""
    server = hdrs.get("server", "")
    snippet = body[:200].decode("utf-8", "replace").replace("\n", " ")
    if status == 429:
        return f"429 触发频率限制 {url}（server={server}）。建议调大 rate_limit.min_interval。"
    if status in (401, 403):
        return f"{status} 被拒绝 {url}（server={server}）。可能被 WAF/风控拦截：{snippet}"
    if status == 404:
        return f"404 {url}"
    return f"HTTP {status} {url}（server={server}）: {snippet}"
