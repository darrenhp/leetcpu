"""解析器单元测试（无需联网，覆盖压缩产物里的各种"脏"语法）。

运行：
    python tests/test_js_literal.py
或：
    python -m pytest tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leetcpu_scraper.js_literal import (  # noqa: E402
    JSLiteralParser,
    find_enclosing_literal,
    iter_array_literals,
    string_mask,
)

CASES = [
    # 基础
    ('[1,2,3]', [1, 2, 3]),
    ('{a:1,b:"x"}', {"a": 1, "b": "x"}),
    ('{"a-b":1,c:[true,false,null]}', {"a-b": 1, "c": [True, False, None]}),
    # 压缩产物常见的布尔写法
    ('[{solved:!1,ok:!0}]', [{"solved": False, "ok": True}]),
    # 尾逗号
    ('[{a:1,},]', [{"a": 1}]),
    # 数字进制
    ('[0x1f,0b101,1.5e3,.5]', [31, 5, 1500.0, 0.5]),
    # 模板字符串 + 转义反引号（题目正文里大量出现）
    ('[`a \\`b\\` c`]', ["a `b` c"]),
    ('[`line1\\nline2`]', ["line1\nline2"]),
    # 注释
    ('[{a:1/*x*/,b:2}]// tail', [{"a": 1, "b": 2}]),
    # 成员表达式降级为占位符（如主题色 se.purple）
    ('[{color:se.purple,n:1}]', [{"color": "<expr:se.purple>", "n": 1}]),
    # 嵌套
    ('[{t:[{k:"ipc",op:"<",v:2}]}]', [{"t": [{"k": "ipc", "op": "<", "v": 2}]}]),
]


def test_parser_cases():
    for src, expect in CASES:
        got = JSLiteralParser(src).parse()
        assert got == expect, f"{src!r} -> {got!r} != {expect!r}"


def test_full_consumption():
    """必须完整消费整段字面量，否则说明括号配平有问题。"""
    for src, _ in CASES:
        p = JSLiteralParser(src)
        p.parse()
        assert p.i == len(src), f"{src!r} 未完整消费：{p.i}/{len(src)}"


def test_string_mask_skips_regex_with_quote():
    """正则里的引号不能被当成字符串起始（否则掩码会错位几千字符）。"""
    js = 'const re=/[\\w!.\'() &$@=;:+,?-]+$/;const kh=[{id:"branch"}];'
    mask = string_mask(js)
    pos = js.find('id:"branch"')
    assert mask[pos] == 0, "正则内的单引号污染了掩码"
    span = find_enclosing_literal(js, pos, "[", mask)
    assert span is not None
    assert js[span[0]] == "[" and js[span[1] - 1] == "]"
    assert JSLiteralParser(js[span[0]:span[1]]).parse() == [{"id": "branch"}]


def test_iter_array_literals_by_marker():
    js = 'var noise=[1,2,3];const Kp=[{id:1,slug:"a",title:"A"}];'
    got = list(iter_array_literals(js, 'slug:"'))
    assert len(got) == 1
    assert got[0][0]["slug"] == "a"


def test_iter_array_literals_rejects_non_data():
    """含真实表达式调用的数组若无法解析，应被跳过而不是抛错。"""
    js = '[foo(1,2),bar()];const Kp=[{slug:"ok"}];'
    got = list(iter_array_literals(js, 'slug:"'))
    assert got and got[0][0]["slug"] == "ok"


if __name__ == "__main__":
    names = [n for n in dir() if n.startswith("test_")]
    failed = 0
    for n in names:
        try:
            globals()[n]()
            print(f"PASS  {n}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {n}: {e}")
    print(f"\n{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
