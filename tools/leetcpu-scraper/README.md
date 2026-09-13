# LeetCPU 抓取方案

针对 `https://www.leetcpu.com/` 的页面结构勘察、反爬对抗与稳定抽取实现。
核心链路**零第三方依赖**（仅 Python 标准库），已实测跑通：22 道题 + 8 个课程模块完整落盘。

---

## 一、站点勘察结论

先做了实测探测，再据此设计抓取策略，结论如下：

| 维度 | 实测结果 | 对抓取的影响 |
| --- | --- | --- |
| 托管 | Vercel（`server: Vercel`，`x-vercel-cache: HIT`） | 边缘节点有速率敏感性；响应带 `ETag`，可做条件请求 |
| 渲染方式 | **CSR/SPA**。首页 HTML 仅 3.3KB，正文是空的 `<div id="root"></div>` | `curl` 拿不到任何正文，必须渲染或另寻数据源 |
| 前端栈 | Vite + React，产物 `/assets/index-<hash>.js`（599KB，单包） | 产物名含 hash，每次发版会变 → **不能硬编码，必须从 HTML 动态解析** |
| 路由 | **无** react-router、`pushState` 出现 0 次 | 全站只有 1 个 URL，不存在"翻页/列表页"，所有内容靠前端状态切换 |
| 核心数据 | 题库以 JS 字面量**内嵌在产物里**（`const Kp=[{id,slug,title,…,starterCode}]`） | 抓 1 次产物 = 拿到全量结构化题库，比渲染 DOM 更完整、更稳定 |
| 后端 | Supabase（Auth + `problems`/`submissions` 表）+ Railway 上的 ChampSim 仿真服务（`/api/broadcast`、`/api/explain`） | 需登录/提交仿真才有交互；公开数据不必走接口 |
| robots.txt | `User-agent: * / Allow: /`，无 crawl-delay | 允许抓取；已实现合规检查（可关闭） |
| sitemap | 仅 1 条 URL | 无法通过 sitemap 发现页面，印证"单页"判断 |
| 反爬强度 | **未见 WAF / 验证码 / JS 挑战** | 主要风险是频率与指纹，而非主动封禁 |

**关键判断**：这是一个"空壳 HTML + 内嵌数据集 + 前端渲染"的三段式站点。
只渲染 DOM 会丢字段（如 `starterCode`、`targets`），只解析产物会丢页面文案与一致性参照 —— 因此本方案**三层都做并交叉校验**。

---

## 二、反爬对抗策略

| 风险 | 本站实测 | 本方案应对 |
| --- | --- | --- |
| Header 指纹校验 | 未触发，但廉价规则常见 | 完整浏览器请求头：`User-Agent` / `Accept` / `Accept-Language` / `Sec-Fetch-*` / `Sec-CH-UA` / `Referer` |
| Cookie / 会话 | 无强制，但保留 | `http.cookiejar` 会话级 Cookie 自动携带；重定向时补 `Referer` |
| 访问频率限制 | Vercel 边缘节点对突发敏感 | 令牌桶（burst=4）+ 最小间隔 1.2s + 随机抖动，避免节律化请求 |
| 429 / 5xx | 未见 | 指数退避重试（底数 1.5，上限 30s），优先遵循 `Retry-After` |
| 重复抓取浪费配额 | — | `ETag` / `Last-Modified` 条件请求，命中 304 时零字节传输（实测已生效） |
| 产物 hash 变化 | 每次发版必变 | 从 HTML 动态解析 `<script type="module" src>`，不硬编码 |
| 内容突然变小/变空 | 可能是软拦截 | 空壳检测 + 渲染重试（虚拟时间翻倍）+ 一致性校验，全部落到 `warnings` |
| 单响应过大打爆内存 | — | `max_body_bytes` 40MB 上限 |
| robots / 合规 | `Allow: /` | 默认检查 robots，并自动采纳其 `crawl-delay` |

---

## 三、三层抽取架构

```
L1 静态 HTML ──► SEO 元数据 / OG / JSON-LD / 资源清单      快、稳、不依赖浏览器
L2 前端产物   ──► 内嵌题库 & 课程数据集                    字段最全、最结构化  ★核心
L3 渲染 DOM   ──► 可见文案、指标、按钮、标题层级            校验 L2，并兜住非内嵌内容
                          └──► 一致性交叉校验（页面宣称 22 题 vs 解析出 22 题）
```

**为什么 L2 是核心**：页面上的"22 Core challenges"只是个数字，真正的题目正文、约束、指标阈值、起始代码全部只存在于产物里，且前端渲染时也未必一次展示完。解析产物是唯一能拿到**全量字段**的路径。

### 难点：产物是压缩 JS，不是 JSON

内嵌数据集含有模板字符串、`` !0/!1 ``、无引号 key、注释、尾逗号、成员表达式（`color:se.purple`），`json.loads` 直接失败，正则硬抠字段则会在结构变化时静默出错。

因此实现了 `js_literal.py`：一个只覆盖"数据字面量"子集的递归下降解析器 ——
遇到无法静态求值的真实表达式时**降级为 `<expr:...>` 占位符而非抛错**，再用常量表回填，保证"解析不崩 + 结果可校验"。

---

## 四、安装与运行

```bash
cd leetcpu-scraper
python main.py                          # 默认 full 模式（三层全量）
python main.py --mode static            # 不渲染，只抓 HTML + 产物（最快）
python main.py --mode render            # 只渲染 DOM
python main.py --min-interval 3         # 更保守的限速
python main.py --no-cache               # 忽略本地 ETag 缓存，强制回源
python main.py --outdir ./out -v
```

渲染层会按 `Google Chrome → Edge → Chromium` 顺序自动探测浏览器，也可用 `CHROME_PATH` 指定。
作为库使用：

```python
from leetcpu_scraper import scrape
snap = scrape(outdir="./output")
print(len(snap.problems), snap.warnings)
```

---

## 五、输出产物

| 文件 | 内容 |
| --- | --- |
| `output/problems.json` | 22 道题全字段（含 description / constraints / examples / targets / starterCode / solutionCode） |
| `output/problems.csv` | 扁平化题库，可直接用 Excel / pandas 打开 |
| `output/problems.jsonl` | 每行一题，便于流式入库 |
| `output/dataset_curriculum.json` | 8 个知识模块（branch / cache / ilp / memory / rob / frontend / tlb / simd） |
| `output/snapshot.json` | 三层结果 + 告警 + 统计的完整快照 |
| `output/page_text.txt` | 渲染后的可见文案（按序文本块） |
| `output/SUMMARY.md` | 人读摘要：站点形态、指标、题库表格 |
| `output/raw/` | 原始 HTML / 渲染 DOM / JS 产物快照，便于回溯与 diff |

---

## 六、稳定性设计

1. **降级不中断**：渲染失败 → 自动退回静态模式并记 warning；产物下载失败 → 跳过 L2 但 L1/L3 照常。
2. **一致性交叉校验**：页面宣称题数 vs 产物解析题数不一致时告警；解析出 0 题或正文过短时告警。
3. **解析完整性自检**：要求解析器**完整消费**整段字面量，否则判定括号配平失败并换外层候选重试。
4. **占位符监控**：统计 `<expr:...>` 残留，作为"站点改版、字段结构变化"的早期信号。
5. **单元测试**：`tests/test_js_literal.py` 覆盖 12 类压缩语法 + 掩码/定位，无需联网即可回归。

```bash
python tests/test_js_literal.py     # 5/5 passed
```

---

## 七、实测踩坑记录

这些是真实遇到并已修复的问题，也是本方案最有参考价值的部分：

| 问题 | 现象 | 修复 |
| --- | --- | --- |
| Playwright 在 macOS 加载失败 | `greenlet .so` 代码签名不匹配（Team ID 不同） | 改为直接驱动本机 Chrome 的 `--headless=new --dump-dom`，零原生依赖 |
| 解析器偏移错位 | `self.i += m.end()` 把绝对下标当增量累加 | 统一改为 `self.i = m.end()`（共 3 处） |
| 括号配平选错外层 | 从"对象内部"往回扫，首个 `{` 的闭合括号在前方，depth 计算天然失效 | 改为**由内向外枚举候选 + 用"能否完整解析"裁决** |
| 掩码错位几千字符 | 正则字面量 `/[\w!.\*'() …]+$/` 里的单引号被当成字符串起始 | 掩码增加正则识别（依据前一个有效字符判断 `/` 是除号还是正则起始） |
| 成员表达式截断解析 | `color:se.purple` 解析到 `se` 后遇到 `.` 报错 | 增加表达式跳过逻辑，降级为占位符后用常量表回填为 `#a78bfa` |
| 指标区被污染 | "01 阅读概要 / 02 编写代码…"这类步骤编号被当成统计指标 | 识别递增连续序列并整体剔除，只保留真实的 22 / 12 / <10s |

---

## 八、扩展：需要交互时怎么办

当前 `--dump-dom` 模式足以覆盖本站点（内容首屏即渲染完成）。
若将来需要点击"Start Challenge"、滚动加载或等待某个 XHR，可切换为 CDP 通道：

```python
# 1) 启动 Chrome：--remote-debugging-port=9222
# 2) GET http://127.0.0.1:9222/json/version 取 webSocketDebuggerUrl
# 3) 用 websocket-client（纯 Python，无签名问题）发送 CDP 命令：
#    Page.navigate → Runtime.evaluate（等待/点击）→ Runtime.evaluate('document.documentElement.outerHTML')
```

届时可把 `renderer.render()` 的实现替换为 CDP 版本，`pipeline` 无需改动。

---

## 九、合规与边界

- 已遵循 `robots.txt`（`Allow: /`），并支持自动采纳 `crawl-delay`；如需关闭用 `--ignore-robots`。
- 默认 1.2s 最小间隔 + 令牌桶，属低频只读访问，不对站点造成负担。
- 仅采集**公开可见**内容；需要登录才能访问的提交记录、排行榜、个人成绩**不在采集范围内**。
- 题库正文与起始代码属站点内容，抓取结果仅供个人学习/离线分析，请勿二次分发或商用。
