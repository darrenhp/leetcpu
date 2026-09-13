"""三层抽取器：静态 HTML → 前端产物（内嵌数据）→ 渲染后 DOM。

站点是"空壳 HTML + 内嵌数据集 + 前端渲染"的三段式结构，
任一层单独使用都会丢数据，因此三层都实现并在 pipeline 里做交叉校验。
"""
from __future__ import annotations

import html as _html
import json
import re
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin

from .js_literal import JSLiteralParser, iter_array_literals

# ---------------------------------------------------------------- 1. 静态层
def extract_static_meta(html: str, base_url: str) -> dict[str, Any]:
    """从原始 HTML 抽取 SEO / OG / JSON-LD / 资源引用。"""
    out: dict[str, Any] = {
        "title": _first(re.findall(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)),
        "meta": {},
        "og": {},
        "twitter": {},
        "json_ld": [],
        "canonical": None,
        "assets": {"scripts": [], "styles": [], "preload": []},
        "root_marker": 'id="root"' in html or "id='root'" in html,
        "is_spa_shell": False,
    }

    for m in re.finditer(r"<meta\s+([^>]*?)/?>", html, re.I | re.S):
        attrs = _attrs(m.group(1))
        name = attrs.get("name", "").lower()
        prop = attrs.get("property", "").lower()
        content = attrs.get("content", "")
        if not content:
            continue
        if prop.startswith("og:"):
            out["og"][prop[3:]] = content
        elif name.startswith("twitter:"):
            out["twitter"][name[8:]] = content
        elif name:
            out["meta"][name] = content

    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]*>', html, re.I)
    if m:
        out["canonical"] = _attrs(m.group(0)).get("href")

    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         html, re.I | re.S):
        raw = m.group(1).strip()
        try:
            out["json_ld"].append(json.loads(raw))
        except json.JSONDecodeError:
            out["json_ld"].append({"_raw": raw})

    for m in re.finditer(r'<script[^>]*\ssrc=["\']([^"\']+)["\']', html, re.I):
        out["assets"]["scripts"].append(urljoin(base_url, m.group(1)))
    for m in re.finditer(r'<link[^>]+rel=["\']stylesheet["\'][^>]*>', html, re.I):
        href = _attrs(m.group(0)).get("href")
        if href:
            out["assets"]["styles"].append(urljoin(base_url, href))
    for m in re.finditer(r'<link[^>]+rel=["\']modulepreload["\'][^>]*>', html, re.I):
        href = _attrs(m.group(0)).get("href")
        if href:
            out["assets"]["preload"].append(urljoin(base_url, href))

    shell = re.search(r'<div id=["\']root["\']>\s*</div>', html, re.I)
    out["is_spa_shell"] = bool(shell)
    return out


def pick_main_bundle(meta: dict[str, Any]) -> str | None:
    """挑出主 JS 产物（Vite 的 index-<hash>.js）。"""
    scripts = meta["assets"]["scripts"]
    for u in scripts:
        if re.search(r"index[.-][A-Za-z0-9_\-]+\.js$", u):
            return u
    return scripts[0] if scripts else None


# ---------------------------------------------------------------- 2. 产物层
BUNDLE_MARKERS = [
    ("problems", 'slug:"', ("slug", "title")),
    ("curriculum", 'tagline:"', ("tagline", "overview")),
    ("guide_sections", 'champsimTip:"', ("champsimTip", "relatedProblems")),
]

# 形如 `const se={bg:"#1a1a1a",amber:"#ffa116",...}` 的扁平标量常量表
_CONST_OBJ = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\{([^{}\[\]]*)\}")
_SCALAR = (str, int, float, bool, type(None))


def build_constant_table(js: str) -> dict[str, object]:
    """把产物里的扁平常量对象收集成 ``{"se.purple": "#a78bfa", ...}``。"""
    table: dict[str, object] = {}
    for m in _CONST_OBJ.finditer(js):
        name, body = m.group(1), m.group(2)
        try:
            obj = JSLiteralParser("{" + body + "}").parse()
        except ValueError:
            continue
        if isinstance(obj, dict) and obj and all(isinstance(v, _SCALAR) for v in obj.values()):
            for k, v in obj.items():
                table[f"{name}.{k}"] = v
    return table


def resolve_expressions(obj: Any, table: dict[str, object]) -> Any:
    """把 ``<expr:se.purple>`` 这类占位符还原成真实常量值。"""
    if not table:
        return obj
    if isinstance(obj, str):
        m = re.fullmatch(r"<expr:([A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*)>", obj)
        return table.get(m.group(1), obj) if m else obj
    if isinstance(obj, dict):
        return {k: resolve_expressions(v, table) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_expressions(v, table) for v in obj]
    return obj


def extract_bundle_datasets(js: str) -> dict[str, list]:
    """从压缩 JS 里还原内嵌的结构化数据集。

    用"特征字段"定位数组（压缩后变量名每次构建都会变，只有字段语义稳定），
    并要求解析结果完整消费整段字面量，避免把半个数组当结果。
    """
    found: dict[str, list] = {}
    seen: set[tuple] = set()   # 内容签名，避免不同锚点定位到同一个数组
    for name, marker, required in BUNDLE_MARKERS:
        best: list | None = None
        best_sig: tuple | None = None
        for data in iter_array_literals(js, marker):
            if not isinstance(data, list) or not data:
                continue
            if not all(isinstance(x, dict) and all(k in x for k in required) for x in data):
                continue
            sig = (len(data), repr(data[0])[:300])
            if sig in seen:
                continue
            if best is None or len(data) > len(best):
                best, best_sig = data, sig
        if best:
            seen.add(best_sig)
            found[name] = best
    # 常量回填：把 <expr:se.purple> 之类的运行时常量还原为真实值
    table = build_constant_table(js)
    if table:
        found = {k: resolve_expressions(v, table) for k, v in found.items()}
    return found


def count_placeholders(obj: Any, limit: int = 20) -> list[str]:
    """统计降级为 ``<expr:...>`` 的表达式，用于数据完整性自检。"""
    hits: list[str] = []

    def walk(x: Any) -> None:
        if len(hits) >= limit:
            return
        if isinstance(x, str):
            m = re.fullmatch(r"<expr:(.+)>", x)
            if m:
                hits.append(m.group(1))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return hits


# ---------------------------------------------------------------- 3. 渲染层
_BLOCK = {"div", "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "tr",
          "header", "nav", "main", "article", "footer", "pre", "td", "th", "button"}


class _DomParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.blocks: list[tuple[str, str]] = []   # (tag, text)
        self.headings: list[tuple[int, str]] = []
        self.links: list[dict[str, str]] = []
        self.buttons: list[str] = []
        self.code: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._cur_link: dict[str, str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "svg", "noscript"):
            self._skip += 1
        if tag in _BLOCK:
            self._flush()
        if tag == "a":
            self._flush()
            self._cur_link = {"href": a.get("href", "")}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "svg", "noscript") and self._skip:
            self._skip -= 1
        if tag == "a":
            self._flush()
            if self._cur_link is not None:
                if self._cur_link.get("text") or self._cur_link.get("href"):
                    self.links.append(self._cur_link)
            self._cur_link = None
        if tag in _BLOCK:
            self._flush(tag)
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self._skip:
            return
        self._buf.append(data)
        if self._cur_link is not None:
            self._cur_link["text"] = (self._cur_link.get("text", "") + data).strip()

    def _flush(self, tag: str | None = None) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if not text:
            return
        # 位于 <a> 内部的行内文本：归并进链接标题，不单独成块
        if self._cur_link is not None and tag is None:
            self._cur_link["text"] = (self._cur_link.get("text", "") + " " + text).strip()
            return
        if tag and tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self.headings.append((int(tag[1]), text))
        if tag == "button":
            self.buttons.append(text)
        if tag in ("pre", "code") or (self.stack and self.stack[-1] in ("pre", "code")):
            self.code.append(text)
        self.blocks.append((tag or "text", text))


def extract_rendered_page(dom: str) -> dict[str, Any]:
    """把渲染后的 DOM 压成"按序文本块 + 结构化要素"。"""
    p = _DomParser()
    p.feed(dom)
    p._flush()

    lines: list[str] = []
    for _, t in p.blocks:
        if not lines or lines[-1] != t:
            lines.append(t)

    return {
        "title": _first(re.findall(r"<title[^>]*>(.*?)</title>", dom, re.S | re.I)),
        "text_blocks": lines,
        "headings": [{"level": lv, "text": t} for lv, t in p.headings],
        "buttons": _dedupe(p.buttons),
        "links": _dedupe_links(p.links),
        "code_samples": _dedupe(p.code),
        "stats": _extract_stats(lines),
        "text_length": sum(len(x) for x in lines),
    }


_NUMISH = re.compile(r"^[<>=~]?\s*[\d.,]+\s*[a-zA-Z+/%]*$")


_DIGITS = re.compile(r"\d{1,3}")


def _extract_stats(lines: list[str]) -> list[dict[str, str]]:
    """识别"数字 + 说明"指标对（如 22 / Core challenges）。

    页面里"01 阅读概要 / 02 编写代码 …"这类**连续编号的步骤**在形式上和指标一模一样，
    必须按"是否构成递增连续序列"剔除，否则指标区会被流程步骤污染。
    """
    cands: list[dict[str, str]] = []
    for i in range(len(lines) - 1):
        a, b = lines[i], lines[i + 1]
        if len(a) <= 12 and _NUMISH.match(a) and 2 <= len(b) <= 40 and not _NUMISH.match(b):
            cands.append({"value": a, "label": b})

    out: list[dict[str, str]] = []
    i = 0
    while i < len(cands):
        if not re.fullmatch(r"\d{1,3}", cands[i]["value"]):
            out.append(cands[i])
            i += 1
            continue
        j = i
        while j < len(cands) and re.fullmatch(r"\d{1,3}", cands[j]["value"]):
            j += 1
        run = cands[i:j]
        if len(run) >= 3:
            i = j          # 连续编号 => 流程步骤，整体丢弃
            continue
        out.extend(run)
        i = j
    return out


# ---------------------------------------------------------------- 工具
def _attrs(fragment: str) -> dict[str, str]:
    return {k.lower(): v for k, v in
            re.findall(r'([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*["\']([^"\']*)["\']', fragment)}


def _first(xs: Iterable[str]) -> str | None:
    for x in xs:
        if x and x.strip():
            return _html.unescape(x).strip()
    return None


def _dedupe(xs: list[str]) -> list[str]:
    out = []
    for x in xs:
        if x and x not in out:
            out.append(x)
    return out


def _dedupe_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    out, seen = [], set()
    for l in links:
        key = (l.get("href", ""), l.get("text", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(l)
    return out
