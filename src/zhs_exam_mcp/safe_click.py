"""点击守卫。

默认拒绝点击任何文本含「提交 / 交卷 / submit」的按钮类元素；唯一例外是 submit_exam
在「提交条件」全部满足后签发的短时放行令牌（SubmitGuard.issue）。
"""
from __future__ import annotations

import secrets
import time
import unicodedata

FORBIDDEN_WORDS = ("提交", "交卷", "submit")


class ForbiddenClick(RuntimeError):
    pass


def is_forbidden_text(text: str | None) -> bool:
    t = unicodedata.normalize("NFKC", text or "").lower().replace(" ", "")
    return any(w in t for w in FORBIDDEN_WORDS)


class SubmitGuard:
    """放行令牌：仅 submit_exam 能签发，有效期短，用完即撤销。"""

    def __init__(self, ttl_s: float = 30.0):
        self.ttl_s = ttl_s
        self._token: str | None = None
        self._expires = 0.0

    def issue(self) -> str:
        self._token = secrets.token_hex(8)
        self._expires = time.monotonic() + self.ttl_s
        return self._token

    def valid(self, token: str | None) -> bool:
        return bool(token) and token == self._token and time.monotonic() < self._expires

    def revoke(self) -> None:
        self._token = None
        self._expires = 0.0


# 取「按钮类祖先（或自身）」的文本；非按钮元素（如选项）只在 purpose != option 时检查自身文本
JS_CLICK_TEXT = r"""
(e, purpose) => {
  const btn = e.closest('button, a, [role=button], .el-button, input[type=button], input[type=submit]');
  const parts = [];
  if (btn) parts.push(btn.innerText || '', btn.value || '', btn.getAttribute('aria-label') || '', btn.title || '');
  else if (purpose !== 'option') parts.push(e.innerText || '', e.value || '', e.getAttribute('aria-label') || '', e.title || '');
  return parts.join(' ').slice(0, 200);
}
"""


def check_click(text: str, guard: SubmitGuard | None = None, token: str | None = None) -> None:
    if is_forbidden_text(text) and not (guard and guard.valid(token)):
        raise ForbiddenClick(f"拒绝点击提交类元素：{text.strip()[:40]!r}")


async def safe_click(handle, *, purpose: str, log=None, guard: SubmitGuard | None = None,
                     token: str | None = None) -> None:
    """handle: Playwright ElementHandle 或 Locator。"""
    text = await handle.evaluate(JS_CLICK_TEXT, purpose)
    try:
        check_click(text, guard, token)
    except ForbiddenClick:
        if log:
            log("blocked_click", purpose=purpose, text=text[:80])
        raise
    await handle.click()
    if log:
        log("click", purpose=purpose, text=text[:80])
