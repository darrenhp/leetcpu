"""JS 数据字面量解析器。

为什么需要它
-----------
leetcpu.com 是 Vite + React SPA，首屏 HTML 只有 3.3KB 的空壳 ``<div id="root">``，
页面上的"22 道挑战题"数据全部以 JS 字面量的形式**内嵌在编译产物** ``/assets/index-*.js`` 中。

这类字面量不是合法 JSON（含模板字符串、``!0/!1``、无引号 key、注释、尾逗号），
``json.loads`` 直接失败；而用正则硬抠字段又会在题目结构变化时静默出错。

本模块实现一个只覆盖"数据字面量"子集的小型递归下降解析器：
对象 / 数组 / 字符串 / 模板字符串 / 数字 / 布尔 / null，并在遇到真实表达式时
降级为 ``<expr:...>`` 占位符而不是抛错 —— 保证"解析不崩 + 结果可校验"。
"""
from __future__ import annotations

import re

__all__ = ["JSLiteralParser", "find_enclosing_literal", "iter_array_literals", "parse_js_value"]

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_IDENT = {"true": True, "false": False, "null": None, "undefined": None, "NaN": None, "Infinity": None}
_NUM = re.compile(
    r"[-+]?(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
)
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


class JSLiteralParser:
    """解析单个 JS 数据字面量（对象或数组）。"""

    def __init__(self, text: str) -> None:
        self.s = text
        self.n = len(text)
        self.i = 0
        self.placeholders: list[str] = []
        self._computed = 0

    # ------------------------------------------------------------ 基础
    def _ws(self) -> None:
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n\f\v":
                self.i += 1
            elif c == "/" and self.i + 1 < n:
                nxt = s[self.i + 1]
                if nxt == "/":
                    j = s.find("\n", self.i)
                    self.i = n if j < 0 else j + 1
                elif nxt == "*":
                    j = s.find("*/", self.i + 2)
                    self.i = n if j < 0 else j + 2
                else:
                    return
            else:
                return

    def _err(self, msg: str) -> Exception:
        ctx = self.s[max(0, self.i - 50): self.i + 30].replace("\n", " ")
        return ValueError(f"{msg} @offset {self.i}: ...{ctx}...")

    # ------------------------------------------------------------ 入口
    def parse(self):
        self._ws()
        v = self._value()
        self._ws()
        return v

    def _value(self):
        self._ws()
        if self.i >= self.n:
            raise self._err("unexpected end of literal")
        c = self.s[self.i]
        if c == "{":
            return self._object()
        if c == "[":
            return self._array()
        if c in "\"'":
            return self._string(c)
        if c == "`":
            return self._template()
        if c == "!":
            return self._bang()
        if c == "-" or c == "+" or c.isdigit() or c == ".":
            return self._number()
        if c.isalpha() or c in "_$":
            return self._identifier()
        raise self._err(f"unexpected character {c!r}")

    # ------------------------------------------------------------ 复合
    def _object(self):
        self.i += 1
        out: dict = {}
        while True:
            self._ws()
            if self.i >= self.n:
                raise self._err("unterminated object")
            if self.s[self.i] == "}":
                self.i += 1
                return out
            key = self._key()
            self._ws()
            if self.i >= self.n or self.s[self.i] != ":":
                raise self._err("expected ':'")
            self.i += 1
            out[key] = self._value()
            self._ws()
            if self.i >= self.n:
                raise self._err("unterminated object")
            c = self.s[self.i]
            if c == ",":
                self.i += 1
                continue
            if c == "}":
                self.i += 1
                return out
            raise self._err("expected ',' or '}'")

    def _key(self) -> str:
        c = self.s[self.i]
        if c in "\"'":
            return self._string(c)
        if c == "`":
            return self._template()
        if c == "[":  # 计算属性名（如 [Symbol]）：原样跳过，用唯一占位键
            depth = 0
            while self.i < self.n:
                if self.s[self.i] == "[":
                    depth += 1
                elif self.s[self.i] == "]":
                    depth -= 1
                    if depth == 0:
                        self.i += 1
                        break
                self.i += 1
            self._computed += 1
            return f"__computed_{self._computed}"
        m = _NAME.match(self.s, self.i)
        if not m:
            raise self._err("bad object key")
        self.i = m.end()  # m.end() 是绝对下标，不可累加
        return m.group(0)

    def _array(self):
        self.i += 1
        out: list = []
        while True:
            self._ws()
            if self.i >= self.n:
                raise self._err("unterminated array")
            if self.s[self.i] == "]":
                self.i += 1
                return out
            out.append(self._value())
            self._ws()
            if self.i >= self.n:
                raise self._err("unterminated array")
            c = self.s[self.i]
            if c == ",":
                self.i += 1
                continue
            if c == "]":
                self.i += 1
                return out
            raise self._err("expected ',' or ']'")

    # ------------------------------------------------------------ 标量
    def _string(self, q: str) -> str:
        self.i += 1
        buf: list[str] = []
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c == "\\":
                self.i += 1
                if self.i >= n:
                    break
                e = s[self.i]
                if e == "u":
                    buf.append(chr(int(s[self.i + 1:self.i + 5], 16)))
                    self.i += 5
                    continue
                if e == "x":
                    buf.append(chr(int(s[self.i + 1:self.i + 3], 16)))
                    self.i += 3
                    continue
                if e == "\n":
                    self.i += 1
                    continue
                buf.append(_ESCAPES.get(e, e))
                self.i += 1
                continue
            if c == q:
                self.i += 1
                return "".join(buf)
            buf.append(c)
            self.i += 1
        raise self._err("unterminated string")

    def _template(self) -> str:
        """模板字符串；``${...}`` 无法静态求值，保留原文作为占位。"""
        self.i += 1
        buf: list[str] = []
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c == "\\":
                self.i += 1
                if self.i >= n:
                    break
                e = s[self.i]
                if e == "u":
                    buf.append(chr(int(s[self.i + 1:self.i + 5], 16)))
                    self.i += 5
                    continue
                if e == "x":
                    buf.append(chr(int(s[self.i + 1:self.i + 3], 16)))
                    self.i += 3
                    continue
                if e == "\n":
                    self.i += 1
                    continue
                buf.append(_ESCAPES.get(e, e))
                self.i += 1
                continue
            if c == "`":
                self.i += 1
                return "".join(buf)
            if c == "$" and self.i + 1 < n and s[self.i + 1] == "{":
                depth, start = 0, self.i
                while self.i < n:
                    if s[self.i] == "{":
                        depth += 1
                    elif s[self.i] == "}":
                        depth -= 1
                        if depth == 0:
                            self.i += 1
                            break
                    self.i += 1
                expr = s[start:self.i]
                self.placeholders.append(expr)
                buf.append(expr)
                continue
            buf.append(c)
            self.i += 1
        raise self._err("unterminated template literal")

    def _bang(self):
        """``!0`` -> True，``!1`` -> False，``!!x`` 逐次取反。"""
        start = self.i
        j = self.i
        while j < self.n and self.s[j] == "!":
            j += 1
        neg = (j - start) % 2 == 1
        m = _NUM.match(self.s, j)
        if not m:
            tok = self.s[start:j + 20]
            self.i = j
            self.placeholders.append(tok)
            return f"<expr:{self.s[start:j]}>"
        self.i = m.end()  # m.end() 已是绝对下标
        tok = m.group(0)
        try:
            val = bool(float(tok)) if ("." in tok or "e" in tok.lower()) else bool(int(tok, 0))
        except ValueError:
            val = True
        return (not val) if neg else val

    def _number(self):
        m = _NUM.match(self.s, self.i)
        if not m:
            raise self._err("bad number")
        tok = m.group(0)
        self.i = m.end()  # m.end() 是绝对下标，不可累加
        low = tok.lower()
        if low.startswith("0x"):
            return int(tok, 16)
        if low.startswith("0b"):
            return int(tok, 2)
        if low.startswith("0o"):
            return int(tok, 8)
        if "." in tok or "e" in low:
            return float(tok)
        return int(tok)

    def _identifier(self):
        m = _NAME.match(self.s, self.i)
        tok = m.group(0)
        self.i = m.end()  # m.end() 是绝对下标，不可累加
        if tok in _IDENT:
            return _IDENT[tok]
    def _skip_quoted(self) -> None:
        """跳过任意引号包裹的片段（含模板字符串的 ``${}``）。"""
        q = self.s[self.i]
        self.i += 1
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == q:
                self.i += 1
                return
            if q == "`" and c == "$" and self.i + 1 < self.n and self.s[self.i + 1] == "{":
                d = 0
                while self.i < self.n:
                    if self.s[self.i] == "{":
                        d += 1
                    elif self.s[self.i] == "}":
                        d -= 1
                        if d == 0:
                            self.i += 1
                            break
                    self.i += 1
                continue
            self.i += 1

    def _skip_expression(self, start: int) -> None:
        """跳过一段真实表达式，停在**本层**的 ``,`` / ``}`` / ``]`` 之前。

        数据字面量里会混入运行时常量（如主题色 ``se.purple``），
        必须整段跳过，否则会把 ``.purple`` 误当成新的结构符号。
        """
        depth = 0
        while self.i < self.n:
            c = self.s[self.i]
            if c in "\"'`":
                self._skip_quoted()
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    return
                depth -= 1
            elif c in ",;" and depth == 0:
                return
            self.i += 1

    def _identifier(self):
        m = _NAME.match(self.s, self.i)
        tok = m.group(0)
        start = m.start()
        self.i = m.end()  # m.end() 是绝对下标，不可累加
        if tok in _IDENT:
            return _IDENT[tok]
        nxt = self.s[self.i] if self.i < self.n else ""
        if nxt in ".([":
            self._skip_expression(start)
            expr = self.s[start:self.i]
        else:
            expr = tok
        # 真实表达式（如主题色常量 se.blue）：无法静态求值，降级为占位符
        self.placeholders.append(expr)
        return f"<expr:{expr}>"


# ---------------------------------------------------------------- 定位工具
_OPEN = "([{"
_CLOSE = ")]}"
_QUOTES = "\"'`"
# ``/`` 出现在这些字符之后时，几乎只可能是正则字面量的起始（而非除号）
_REGEX_PREV = set("(,=:[!&|?{};+-*/%~^<>") | {"", "\n", "return", "typeof", "case", "in", "of", "new", "do", "else", "yield", "await"}


def _is_regex_start(text: str, i: int) -> bool:
    """启发式判断 ``text[i] == '/'`` 是否为正则字面量起始。"""
    if i + 1 >= len(text) or text[i + 1] in "/*":
        return False
    j = i - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    if text[j] in _REGEX_PREV:
        return True
    # 关键字后（return /re/ 之类）
    k = j + 1
    while k > 0 and (text[k - 1].isalnum() or text[k - 1] in "_$"):
        k -= 1
    return text[k:j + 1] in _REGEX_PREV


def string_mask(text: str) -> bytearray:
    """标记"位于字符串 / 模板字符串 / 注释 / 正则字面量内部"的位置。

    括号配平必须先排除这些区域。其中**正则字面量的识别是关键**——
    一旦把正则里的引号当成字符串起始，掩码会错位到几千字符之外，
    导致后面的括号配平找到错误的外层容器（这是本文件最隐蔽的一个坑）。
    """
    n = len(text)
    m = bytearray(n)
    i = 0
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                j = text.find("\n", i)
                j = n if j < 0 else j + 1
                m[i:j] = b"\x01" * (j - i)
                i = j
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                m[i:j] = b"\x01" * (j - i)
                i = j
                continue
            if _is_regex_start(text, i):
                j = i + 1
                in_class = False
                while j < n:
                    ch = text[j]
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == "\n":
                        break  # 正则不能跨行，判定失误时尽快止损
                    if in_class:
                        if ch == "]":
                            in_class = False
                    elif ch == "[":
                        in_class = True
                    elif ch == "/":
                        j += 1
                        break
                    j += 1
                j = min(j, n)
                m[i:j] = b"\x01" * (j - i)
                i = j
                continue
        if c in _QUOTES:
            q = c
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == q:
                    j += 1
                    break
                j += 1
            j = min(j, n)
            m[i:j] = b"\x01" * (j - i)
            i = j
            continue
        i += 1
    return m


def _match_forward(text: str, start: int, mask: bytearray) -> tuple[int, int] | None:
    """从 ``start`` 处的开括号出发做配平，返回 (start, end) 或 None。"""
    d = 0
    n = len(text)
    for j in range(start, n):
        if mask[j]:
            continue
        c = text[j]
        if c in _OPEN:
            d += 1
        elif c in _CLOSE:
            d -= 1
            if d == 0:
                return start, j + 1
    return None


def iter_enclosing_literals(text: str, pos: int, open_ch: str = "[",
                            mask: bytearray | None = None,
                            window: int = 400_000,
                            max_candidates: int = 6,
                            max_span: int = 2_000_000):
    """由内向外枚举所有"配平后能覆盖 ``pos``"的字面量区间。

    逐个（而非只取第一个）候选，是因为掩码在极端压缩代码下可能仍有偏差；
    交给上层的"能否被解析器完整消费"来做最终裁决，比在这里赌一次更稳。
    """
    mask = mask if mask is not None else string_mask(text)
    lo = max(0, pos - window)
    i = pos
    found = 0
    while i >= lo and found < max_candidates:
        if mask[i]:
            i -= 1
            continue
        if text[i] == open_ch:
            span = _match_forward(text, i, mask)
            if span and span[0] <= pos < span[1] and (span[1] - span[0]) <= max_span:
                found += 1
                yield span
        i -= 1


def find_enclosing_literal(text: str, pos: int, open_ch: str = "[",
                           mask: bytearray | None = None,
                           window: int = 400_000) -> tuple[int, int] | None:
    for span in iter_enclosing_literals(text, pos, open_ch, mask, window, max_candidates=1):
        return span
    return None


def iter_array_literals(text: str, marker: str, open_ch: str = "[", window: int = 400_000):
    """按特征字段（如 ``slug:"``）定位并解析所有候选数组字面量。

    压缩后变量名每次构建都会变，只有字段语义稳定，因此用字段做锚点。
    只有"完整消费整段字面量"的候选才会被产出，避免半个数组混入结果。
    """
    mask = string_mask(text)
    seen: set[tuple[int, int]] = set()
    covered: list[tuple[int, int]] = []
    for m in re.finditer(re.escape(marker), text):
        pos = m.start()
        if any(a <= pos < b for a, b in covered):
            continue
        for span in iter_enclosing_literals(text, pos, open_ch, mask, window):
            if span in seen:
                continue
            p = JSLiteralParser(text[span[0]:span[1]])
            try:
                data = p.parse()
            except ValueError:
                continue  # 该候选不是纯数据字面量，换外层再试
            if p.i != span[1] - span[0]:
                continue
            seen.add(span)
            covered.append(span)
            yield data
            break


def parse_js_value(text: str):
    return JSLiteralParser(text).parse()
