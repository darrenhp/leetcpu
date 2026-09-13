/* LeetCPU 中文站 —— 搜索、主题切换、代码高亮与复制 */
(() => {
  "use strict";

  /* ---------- 主题 ---------- */
  const THEME_KEY = "leetcpu-cn-theme";
  const saved = localStorage.getItem(THEME_KEY);
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = saved || (prefersDark ? "dark" : "light");

  document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem(THEME_KEY, next);
    });
  });

  /* ---------- C 代码高亮 ---------- */
  const KEYWORDS = new Set(["if", "else", "for", "while", "do", "return", "break", "continue",
    "switch", "case", "default", "goto", "sizeof", "typedef", "struct", "union", "enum",
    "const", "static", "inline", "volatile", "restrict", "unsigned", "signed", "register"]);
  const TYPES = new Set(["int", "long", "short", "char", "float", "double", "void", "bool",
    "size_t", "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t",
    "uint64_t", "unsigned int", "unsigned long", "unsigned char", "long long", "Node", "Particle"]);

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function highlight(src) {
    // 一次性扫描：注释 / 字符串 / 预处理 / 数字 / 标识符
    const out = [];
    let i = 0;
    const n = src.length;
    while (i < n) {
      const c = src[i];
      // 行注释
      if (c === "/" && src[i + 1] === "/") {
        let j = src.indexOf("\n", i); if (j < 0) j = n;
        out.push(`<span class="k-com">${esc(src.slice(i, j))}</span>`); i = j; continue;
      }
      // 块注释
      if (c === "/" && src[i + 1] === "*") {
        let j = src.indexOf("*/", i + 2); j = j < 0 ? n : j + 2;
        out.push(`<span class="k-com">${esc(src.slice(i, j))}</span>`); i = j; continue;
      }
      // 预处理
      if (c === "#") {
        let j = i; while (j < n && src[j] !== "\n") j++;
        const line = src.slice(i, j);
        const m = line.match(/^#\s*\w+/);
        if (m) {
          out.push(`<span class="k-pre">${esc(m[0])}</span>` + esc(line.slice(m[0].length)));
        } else out.push(esc(line));
        i = j; continue;
      }
      // 字符串 / 字符
      if (c === '"' || c === "'") {
        let j = i + 1;
        while (j < n && src[j] !== c) { if (src[j] === "\\") j++; j++; }
        j = Math.min(j + 1, n);
        out.push(`<span class="k-str">${esc(src.slice(i, j))}</span>`); i = j; continue;
      }
      // 数字
      if (/[0-9]/.test(c) && !/[A-Za-z_]/.test(src[i - 1] || "")) {
        let j = i; while (j < n && /[0-9a-fA-FxXuUlL.]/.test(src[j])) j++;
        out.push(`<span class="k-num">${esc(src.slice(i, j))}</span>`); i = j; continue;
      }
      // 标识符 / 关键字
      if (/[A-Za-z_]/.test(c)) {
        let j = i; while (j < n && /[A-Za-z0-9_]/.test(src[j])) j++;
        const w = src.slice(i, j);
        // 函数调用：标识符后紧跟 (
        let k = j; while (k < n && src[k] === " ") k++;
        if (KEYWORDS.has(w)) out.push(`<span class="k-key">${esc(w)}</span>`);
        else if (TYPES.has(w)) out.push(`<span class="k-type">${esc(w)}</span>`);
        else if (src[k] === "(") out.push(`<span class="k-fn">${esc(w)}</span>`);
        else out.push(esc(w));
        i = j; continue;
      }
      out.push(esc(c)); i++;
    }
    return out.join("");
  }

  document.querySelectorAll("pre[data-code]").forEach((pre) => {
    pre.innerHTML = "<code>" + highlight(pre.getAttribute("data-code")) + "</code>";
  });

  /* ---------- 复制 ---------- */
  document.querySelectorAll(".copybtn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const pre = btn.closest(".codeblock").querySelector("pre");
      const text = pre.getAttribute("data-code") || pre.innerText;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "已复制";
      } catch (e) {
        btn.textContent = "复制失败";
      }
      setTimeout(() => (btn.textContent = "复制"), 1600);
    });
  });

  /* ---------- 搜索 ---------- */
  const input = document.querySelector("#q");
  const box = document.querySelector("#results");
  let index = null;

  async function loadIndex() {
    if (index) return index;
    // 优先用构建期内嵌的索引（这样直接 file:// 打开也能搜索）
    if (window.__INDEX__) { index = window.__INDEX__; return index; }
    try {
      const root = document.body.dataset.root || "";
      const r = await fetch(root + "assets/search.json");
      index = await r.json();
    } catch (e) { index = []; }
    return index;
  }

  function render(q) {
    const items = index.filter((it) => {
      const hay = (it.title + " " + it.zh + " " + it.mod + " " + it.bottleneck + " " + it.sum).toLowerCase();
      return hay.includes(q);
    }).slice(0, 12);
    if (!items.length) { box.innerHTML = '<div class="empty">没有匹配的内容</div>'; return; }
    box.innerHTML = items.map((it) => `
      <a href="${it.url}">
        <div class="t">${it.zh}</div>
        <div class="d">${it.kind} · ${it.mod || ""} ${it.bottleneck ? "· " + it.bottleneck : ""}</div>
      </a>`).join("");
  }

  if (input && box) {
    let timer = null;
    input.addEventListener("input", async () => {
      const q = input.value.trim().toLowerCase();
      if (!q) { box.classList.remove("show"); return; }
      await loadIndex();
      clearTimeout(timer);
      timer = setTimeout(() => { render(q); box.classList.add("show"); }, 90);
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".searchbox")) box.classList.remove("show");
    });
    input.addEventListener("focus", async () => {
      if (input.value.trim()) { await loadIndex(); render(input.value.trim().toLowerCase()); box.classList.add("show"); }
    });
  }

  /* ---------- 题目列表筛选 ---------- */
  const filterBar = document.querySelector("#filters");
  if (filterBar) {
    filterBar.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-filter]");
      if (!btn) return;
      const f = btn.dataset.filter;
      filterBar.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
      document.querySelectorAll("#plist tr[data-mod]").forEach((tr) => {
        tr.style.display = (f === "all" || tr.dataset.mod === f) ? "" : "none";
      });
    });
  }
})();
