"""侦察「作业考试」列表页（只读：reload 抓接口、保存 DOM/截图，不点击任何条目）。
产物：runtime/recon/list/"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from recon import XhrRecorder, redact_url  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "runtime" / "recon" / "list"

JS_ITEMS = r"""
() => {
  const desc = e => e.tagName.toLowerCase() + (typeof e.className === 'string' && e.className ? '.' + e.className.trim().replace(/\s+/g, '.') : '');
  // 含「单元测试/章节测试/作业/考试」字样的可见叶子文本，往上找 4 层作为条目容器线索
  const hits = [...document.querySelectorAll('body *')].filter(e => e.children.length === 0 &&
      /测试|作业|考试/.test(e.textContent) && e.textContent.trim().length < 40 && e.getBoundingClientRect().width > 0);
  return hits.slice(0, 40).map(e => {
    const chain = []; let c = e; for (let i = 0; i < 5 && c; i++, c = c.parentElement) chain.push(desc(c));
    return {text: e.textContent.trim(), chain};
  });
}
"""
JS_BUTTONS = r"""
() => [...document.querySelectorAll('button, a, [role=button], .el-button, span, div')]
  .filter(e => e.children.length <= 1 && (e.innerText || '').trim().length > 0 && (e.innerText || '').trim().length < 8
          && /做|答|开始|继续|查看|进入|提交|暂存|作业|考试/.test(e.innerText) && e.getBoundingClientRect().width > 0)
  .map(e => ({text: e.innerText.trim(), tag: e.tagName.toLowerCase(), cls: typeof e.className === 'string' ? e.className : '',
              href: e.getAttribute('href'), target: e.getAttribute('target'), onclick: !!e.onclick}))
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        page = next((p for c in b.contexts for p in c.pages if "webExamList" in p.url and "dohomework" not in p.url), None)
        if page is None:
            sys.exit("未找到作业考试列表页（#/webExamList）")
        rec = XhrRecorder(OUT / "xhr")
        page.on("response", rec.on_response)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(3000)
        page.remove_listener("response", rec.on_response)
        (OUT / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(OUT / "page.png"), full_page=True)
        summary = {"url": redact_url(page.url), "xhr": rec.items, "items": page.evaluate(JS_ITEMS),
                   "buttons": page.evaluate(JS_BUTTONS),
                   "body_text": page.evaluate("() => document.body.innerText.slice(0, 3000)")}
        (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        b.close()
    print("done", OUT)


if __name__ == "__main__":
    main()
