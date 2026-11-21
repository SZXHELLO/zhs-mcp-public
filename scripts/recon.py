"""阶段0 侦察脚本（只读：不点选项、不点「下一题」、不点暂存/提交）。

用法（先用 scripts\\start_edge.bat 启动 Edge、登录并打开章节测试页）：
    uv run python scripts/recon.py                # reload 抓接口 + 保存 DOM + 逐题切换保存
    uv run python scripts/recon.py --nos 3,12     # 只切换到指定题号
    uv run python scripts/recon.py --no-walk      # 不切题

产物写入 runtime/recon/：
    xhr/NN_<name>.json        接口 url/method/请求体/响应体（已脱敏）
    page.html / page.png      整页
    questions/qNN.html/.png   每题题目区（切换后）
    walk_xhr/                 切题期间产生的请求（用于判断切题是否有副作用）
    summary.json              每题的题型文字、选项数、公式元素统计、候选选择器线索
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runtime" / "recon"

SKIP_BODY = ("getLoginUserInfo",)
SENSITIVE_KEYS = {
    "uid", "userid", "username", "realname", "studentno", "studentcode", "studentid",
    "phone", "mobile", "email", "token", "cookie", "headpic", "headpicurl", "avatar",
    "nickname", "idcard", "schoolname", "classname", "loginname", "secretstr", "jsessionid",
    "examusername", "stuname", "studentname", "name_", "truename",
}


def is_sensitive(key: str) -> bool:
    k = key.lower().replace("_", "")
    return k in SENSITIVE_KEYS


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("<REDACTED>" if is_sensitive(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    q = [(k, "<REDACTED>" if is_sensitive(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(q)))


def redact_text(text: str | None) -> str | None:
    if text is None:
        return None
    try:
        return json.dumps(redact(json.loads(text)), ensure_ascii=False, indent=1)
    except Exception:
        try:  # form-encoded
            pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
            return urlencode([(k, "<REDACTED>" if is_sensitive(k) else v) for k, v in pairs])
        except Exception:
            return text


def short_name(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return re.sub(r"[^\w\-]", "_", path.rsplit("/", 1)[-1] or "root")[:60]


class XhrRecorder:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.n = 0
        self.items: list[dict] = []
        out_dir.mkdir(parents=True, exist_ok=True)

    def on_response(self, resp):
        req = resp.request
        if req.resource_type not in ("xhr", "fetch"):
            return
        self.n += 1
        name = short_name(req.url)
        rec = {
            "url": redact_url(req.url),
            "method": req.method,
            "status": resp.status,
            "request_body": redact_text(req.post_data),
            "content_type": resp.headers.get("content-type"),
        }
        if any(s in req.url for s in SKIP_BODY):
            rec["response_body"] = "<SKIPPED: personal info>"
        else:
            try:
                rec["response_body"] = redact_text(resp.text())
            except Exception as e:  # noqa: BLE001
                rec["response_body"] = f"<unavailable: {e}>"
        fn = self.out_dir / f"{self.n:02d}_{name}.json"
        fn.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        self.items.append({"n": self.n, "name": name, "method": req.method, "status": resp.status,
                           "size": len(rec["response_body"] or "")})


def find_exam_page(browser):
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "onlineexamh5new.zhihuishu.com" in p.url:
                return p
    return None


# 在页面中找到右侧答题卡上文字恰为 n 的可见叶子元素（启发式，侦察用）
JS_FIND_CARD = r"""
(n) => {
  const els = [...document.querySelectorAll('body *')].filter(e =>
     e.children.length === 0 && e.textContent.trim() === String(n));
  const vis = els.filter(e => { const r = e.getBoundingClientRect();
     return r.width > 0 && r.height > 0 && r.left > window.innerWidth * 0.55; });
  if (!vis.length) return null;
  const e = vis[0];
  e.setAttribute('data-recon-card', String(n));
  const chain = []; let c = e;
  for (let i = 0; i < 5 && c; i++, c = c.parentElement)
     chain.push(c.tagName.toLowerCase() + (c.className && typeof c.className === 'string' ? '.' + c.className.trim().replace(/\s+/g, '.') : ''));
  return chain;
}
"""

# 找到题目区：包含 "N.【xx题】" 头部的最小祖先，且该祖先同时包含「下一题」之前的选项
JS_QUESTION_AREA = r"""
() => {
  const re = /^\s*(\d+)\s*[.．、]\s*【([^】]+)】/;
  const heads = [...document.querySelectorAll('body *')].filter(e => {
     const t = (e.innerText || '').trim();
     return re.test(t) && t.length < 40 && e.getBoundingClientRect().width > 0;
  });
  if (!heads.length) return null;
  const head = heads[heads.length - 1];
  let area = head;
  while (area.parentElement && !/[A-H]\s*[.．、]/.test(area.innerText.replace(head.innerText, '')))
     area = area.parentElement;
  if (area.parentElement) area = area.parentElement;
  const desc = e => e.tagName.toLowerCase() + (typeof e.className === 'string' && e.className ? '.' + e.className.trim().replace(/\s+/g, '.') : '');
  const chain = []; let c = head;
  for (let i = 0; i < 6 && c; i++, c = c.parentElement) chain.push(desc(c));
  const q = sel => area.querySelectorAll(sel).length;
  return {
    header_text: head.innerText.trim(),
    header_chain: chain,
    area_desc: desc(area),
    html: area.outerHTML,
    formula: {
      mathjax_script: q('script[type^="math/tex"]'),
      mathjax_rendered: q('.MathJax, .MathJax_Preview, mjx-container'),
      annotation_tex: q('annotation[encoding="application/x-tex"]'),
      katex: q('.katex'),
      mathml: q('math'),
      data_latex: q('[data-latex]'),
      img: q('img'),
      sup_sub: q('sup, sub'),
    },
    inputs: [...area.querySelectorAll('input')].map(i => ({type: i.type, checked: i.checked, name: i.name, value: i.value})),
  };
}
"""

JS_BUTTONS = r"""
() => [...document.querySelectorAll('button, a, [role=button], .el-button, span, div')]
  .filter(e => e.children.length <= 1 && /暂存|提交|下一题|上一题/.test((e.innerText || '').trim()) && (e.innerText || '').trim().length < 10)
  .map(e => ({text: e.innerText.trim(), tag: e.tagName.toLowerCase(), cls: typeof e.className === 'string' ? e.className : '',
              disabled: e.disabled || e.classList.contains('is-disabled') || e.getAttribute('aria-disabled') === 'true'}))
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--nos", default="", help="只切换这些题号，逗号分隔；默认全部")
    ap.add_argument("--no-walk", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    summary: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S")}

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp)
        page = find_exam_page(browser)
        if page is None:
            sys.exit("未找到 onlineexamh5new.zhihuishu.com 的标签页，请先打开章节测试页。")
        page.on("dialog", lambda d: (summary.setdefault("dialogs", []).append(d.message), d.dismiss()))

        # 1+2. reload 抓接口
        rec = XhrRecorder(OUT / "xhr")
        page.on("response", rec.on_response)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(3000)
        page.remove_listener("response", rec.on_response)
        summary["xhr_on_load"] = rec.items
        summary["frames"] = [redact_url(f.url) for f in page.frames]

        # 3. 整页
        (OUT / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(OUT / "page.png"), full_page=True)
        summary["buttons"] = page.evaluate(JS_BUTTONS)
        summary["body_text_head"] = page.evaluate("() => document.body.innerText.slice(0, 1500)")
        summary["vue"] = page.evaluate("() => !!(document.querySelector('[data-v-app]') || window.Vue || document.querySelector('#app')?.__vue__)")
        first = page.evaluate(JS_QUESTION_AREA)
        summary["initial_question"] = {k: v for k, v in (first or {}).items() if k != "html"}

        # 4. 逐题切换（只点答题卡题号）
        qdir = OUT / "questions"
        qdir.mkdir(exist_ok=True)
        if not args.no_walk:
            total = 0
            m = re.search(r"题目数\s*(\d+)", summary["body_text_head"])
            total = int(m.group(1)) if m else 0
            nos = [int(x) for x in args.nos.split(",") if x.strip()] or list(range(1, total + 1))
            walk = XhrRecorder(OUT / "walk_xhr")
            page.on("response", walk.on_response)
            per_q = []
            for n in nos:
                chain = page.evaluate(JS_FIND_CARD, n)
                if chain is None:
                    per_q.append({"no": n, "error": "card button not found"})
                    continue
                page.click(f"[data-recon-card='{n}']")
                page.wait_for_timeout(1200)
                info = page.evaluate(JS_QUESTION_AREA)
                if info:
                    (qdir / f"q{n:02d}.html").write_text(info.pop("html"), encoding="utf-8")
                try:
                    page.screenshot(path=str(qdir / f"q{n:02d}.png"))
                except Exception:
                    pass
                per_q.append({"no": n, "card_chain": chain, **(info or {"error": "question area not found"})})
            page.remove_listener("response", walk.on_response)
            summary["walk"] = per_q
            summary["walk_xhr"] = walk.items
            if 1 not in nos or nos[-1] != 1:
                if page.evaluate(JS_FIND_CARD, 1) is not None:
                    page.click("[data-recon-card='1']")
            summary["buttons_after_walk"] = page.evaluate(JS_BUTTONS)
        browser.close()  # 只断开 CDP，不会关闭用户的 Edge

    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"侦察完成 → {OUT}")


if __name__ == "__main__":
    main()
