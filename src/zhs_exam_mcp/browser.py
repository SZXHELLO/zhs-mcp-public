"""Playwright：connect_over_cdp 连接用户已登录的 Edge；DOM 适配器实现。

侦察结论（docs/FINDINGS.md）：
- 题干在 DOM 中为空（防复制渲染），只能取自页面加载时 doHomework 的响应 → 被动监听响应
- 26 题的 DOM 同时存在（仅当前题可见），选项 input 的 value = 接口选项 id
- 选中：字母 span 带 .onChecked；答题卡 li.white = 未答
- 最后一题没有「下一题」，是「保存」；「暂存作业」会弹确认框
- 页面每 60 s 检测「异常脚本」，检测到会弹框并禁止答题 → 视为弹窗，立即中止（不做任何规避）
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from playwright.async_api import Browser, Page, Playwright, Response, async_playwright

from .adapter import PageAdapter
from .config import Config
from .extractor import parse_header
from .extractor_api import ApiQuestion, DomState, parse_do_homework, parse_server_answers
from .models import CardItem, ExamInfo, RawOption, RawQuestion
from .safe_click import SubmitGuard, safe_click

log = logging.getLogger(__name__)


class NotConnected(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.dialogs: list[str] = []
        self.allow_dialog_accept = False
        self.paper: dict | None = None          # 最近一次 doHomework 响应
        self.answers: dict | None = None        # 最近一次 getStuAnswerInfoNew 响应
        self.paper_seq = 0
        self.answers_seq = 0
        self.homework: dict | None = None       # 最近一次 getStudentHomework 响应（仅内存）
        self.homework_seq = 0

    async def ensure_browser(self, cdp_url: str | None = None) -> Browser:
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self.browser is None or not self.browser.is_connected():
            try:
                self.browser = await self._pw.chromium.connect_over_cdp(cdp_url or self.cfg.cdp_url)
            except Exception as e:  # noqa: BLE001
                raise NotConnected(f"连不上 {cdp_url or self.cfg.cdp_url}：调试端口 Edge 没在运行。"
                                   "请运行 scripts/start_edge.bat，登录并打开章节测试页后重试") from e
            for ctx in self.browser.contexts:
                ctx.on("page", self.attach)  # 新开的做题页尽早挂监听，才能捕获首次加载的 doHomework
        return self.browser

    def attach(self, p: Page) -> None:
        if getattr(p, "_zhs_attached", False):
            return
        p._zhs_attached = True  # type: ignore[attr-defined]
        p.on("dialog", self._on_dialog)
        p.on("response", self._on_response)

    def _pages(self) -> list[Page]:
        assert self.browser is not None
        return [p for ctx in self.browser.contexts for p in ctx.pages if not p.is_closed()]

    def exam_pages(self) -> list[Page]:
        return [p for p in self._pages() if self.cfg.page_url_match in p.url and self.cfg.page_url_prefer in p.url]

    async def list_page(self) -> Page:
        await self.ensure_browser()
        lst = [p for p in self._pages() if self.cfg.exam_list.url_match in p.url and self.cfg.page_url_prefer not in p.url]
        if not lst:
            raise NotConnected("未找到「作业考试」列表页（URL 含 #/webExamList），请在调试端口 Edge 中打开课程的作业考试页")
        self.attach(lst[0])
        return lst[0]

    def use_exam_page(self, p: Page, *, keep_capture: bool = False) -> None:
        self.attach(p)
        if self.page is not p and not keep_capture:
            self.paper = self.answers = None  # 不能沿用上一份测试的数据
        self.page = p

    async def connect(self, cdp_url: str | None = None) -> Page:
        if self.page is not None and not self.page.is_closed():
            return self.page
        await self.ensure_browser(cdp_url)
        preferred = self.exam_pages()
        if len(preferred) > 1:
            raise NotConnected("打开了多个章节测试做题页，请只保留要处理的那一个")
        if preferred:
            self.use_exam_page(preferred[0])
            return preferred[0]
        if any(self.cfg.page_url_match in p.url for p in self._pages()):
            raise NotConnected("找到了智慧树考试页面，但不是做题页（URL 不含 /dohomework/）："
                               "请打开具体的章节测试，或用 open_exam 从作业考试列表打开")
        raise NotConnected(f"未找到 URL 含 {self.cfg.page_url_match} 的标签页，请先在调试端口 Edge 中打开章节测试页")

    async def _on_dialog(self, d) -> None:
        self.dialogs.append(d.message)
        if self.allow_dialog_accept:
            await d.accept()
        else:
            await d.dismiss()

    async def _on_response(self, resp: Response) -> None:
        url = resp.url
        try:
            if self.cfg.exam_list.homework_url_pattern in url:
                self.homework = await resp.json()   # 含姓名等个人信息：只留在内存
                self.homework_seq += 1
            elif self.cfg.api.paper_url_pattern in url:
                if self.page is not None and resp.frame.page is not self.page:
                    return  # 不是当前做题页的数据
                self.paper = await resp.json()
                self.paper_seq += 1
            elif self.cfg.api.answer_url_pattern in url:
                self.answers = await resp.json()
                self.answers_seq += 1   # 作答记录只留在内存，不落盘
        except Exception as e:  # noqa: BLE001
            log.warning("capture %s failed: %s", url, e)

    async def reload_and_capture(self, timeout_s: float = 15.0) -> None:
        assert self.page is not None
        seq, aseq = self.paper_seq, self.answers_seq
        self.answers = None  # 只用本次加载的服务器作答记录
        await self.page.reload(wait_until="networkidle")
        deadline = time.monotonic() + timeout_s
        while self.paper_seq == seq and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if self.paper_seq == seq:
            raise RuntimeError("reload 后没有捕获到 doHomework 响应")
        # 有作答时页面会接着请求 getStuAnswerInfoNew（hasAnswer=1 才请求）
        deadline = time.monotonic() + 4
        while self.answers_seq == aseq and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.5)  # 等 DOM 根据已作答记录渲染选中状态

    async def close(self) -> None:
        # connect_over_cdp 下 close() 只断开连接，不会关闭用户的 Edge
        if self.browser is not None:
            await self.browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._pw = self.browser = self.page = None


JS_READ_QUESTION = r"""
(s) => {
  const pick = (root, sel) => sel ? root.querySelector(sel) : null;
  const has = (el, sel) => !!sel && (el.matches(sel) || !!el.querySelector(sel));
  const visible = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const roots = s.question_root ? [...document.querySelectorAll(s.question_root)].filter(visible) : [];
  const root = roots[0] || document.body;
  const header = pick(root, s.question_header);
  const stem = pick(root, s.question_stem);
  const items = s.option_item ? [...root.querySelectorAll(s.option_item)].filter(visible) : [];
  return {
    found_root: roots.length > 0,
    header_text: header ? header.innerText : (root.innerText || '').slice(0, 80),
    stem_html: stem ? stem.innerHTML : '',
    options: items.map(it => {
      const lab = pick(it, s.option_label);
      const con = pick(it, s.option_content) || it;
      const inp = it.querySelector('input[type=radio], input[type=checkbox]');
      return {
        label: lab ? lab.innerText.trim() : '',
        html: con.innerHTML,
        selected: has(it, s.option_selected),
        option_id: (inp && inp.value) || it.getAttribute('data-id') || null,
      };
    }),
  };
}
"""

JS_ALL_STATES = r"""
(s) => {
  const has = (el, sel) => !!sel && (el.matches(sel) || !!el.querySelector(sel));
  return [...document.querySelectorAll(s.question_root)].map((root, index) => {
    const items = [...root.querySelectorAll(s.option_item)];
    const ids = [], sel = [];
    items.forEach(it => {
      const inp = it.querySelector('input[type=radio], input[type=checkbox]');
      const id = inp ? inp.value : null;
      if (id) { ids.push(id); if (has(it, s.option_selected)) sel.push(id); }
    });
    return {index, option_ids: ids, selected_ids: sel};
  });
}
"""

JS_CARD = r"""
(s) => {
  const has = (el, sel) => !!sel && (el.matches(sel) || !!el.querySelector(sel));
  if (!s.card_item) return [];
  return [...document.querySelectorAll(s.card_item)].map((e, i) => {
    const m = (e.innerText || '').trim().match(/^\d+$/);
    return {no: m ? parseInt(m[0]) : i + 1, answered: has(e, s.card_answered), current: has(e, s.card_current)};
  });
}
"""

JS_EXAM_INFO = r"""
(s) => {
  const t = document.body.innerText;
  const g = re => { const m = t.match(re); return m ? m[1].trim() : null; };
  const titleEl = s.exam_title ? document.querySelector(s.exam_title) : null;
  return {
    course: g(/名称\s*\n?\s*([^\n]+)/),
    title: titleEl ? titleEl.innerText.trim() : g(/(第[^\n]{1,10}章[^\n]{0,10}(?:测试|作业))/),
    chapter: g(/对应章节\s*\n?\s*([^\n]+)/),
    deadline: g(/截止时间\s*\n?\s*([^\n]+)/),
    total: g(/题目数\s*\n?\s*(\d+)/),
  };
}
"""

JS_DIALOG = r"""
(sel) => {
  if (!sel) return null;
  // 真正可见：有尺寸、与视口相交、自身及祖先不透明（验证码空闲时停在 top:-1000000 且 opacity:0）
  const shown = e => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return false;
    for (let c = e; c && c.nodeType === 1; c = c.parentElement) {
      const st = getComputedStyle(c);
      if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false;
    }
    return true;
  };
  const vis = [...document.querySelectorAll(sel)].filter(shown);
  return vis.length ? vis.map(e => (e.innerText || e.id || e.className).trim()).join(' | ').slice(0, 300) || '(无文字弹窗)' : null;
}
"""

JS_TOAST = r"""
() => [...document.querySelectorAll('.el-message, .el-notification, .layui-layer-msg, [role=alert]')]
  .map(e => e.innerText.trim()).filter(Boolean).join(' | ') || null
"""


class DomAdapter(PageAdapter):
    def __init__(self, session: BrowserSession, guard: SubmitGuard, action_log):
        self.s = session
        self.cfg = session.cfg
        self.sel = session.cfg.selectors.model_dump()
        self.guard = guard
        self.log = action_log
        self._api_by_ids: dict[frozenset[str], ApiQuestion] = {}
        self._api_seq = -1

    @property
    def page(self) -> Page:
        if self.s.page is None:
            raise NotConnected("尚未 connect")
        return self.s.page

    async def url(self) -> str:
        return self.page.url

    # ---------- 接口数据 ----------
    async def paper(self, refresh: bool = False) -> tuple[ExamInfo, list[ApiQuestion]]:
        """返回解析后的 doHomework；没有捕获过（或 refresh）时 reload 页面被动捕获。"""
        if refresh or self.s.paper is None:
            await self.s.reload_and_capture()
        info, qs = parse_do_homework(self.s.paper)  # type: ignore[arg-type]
        if self._api_seq != self.s.paper_seq:
            self._api_by_ids = {q.option_ids: q for q in qs}
            self._api_seq = self.s.paper_seq
        return info, qs

    async def server_answers(self) -> dict[str, set[str]] | None:
        return parse_server_answers(self.s.answers)

    async def all_states(self) -> list[DomState]:
        return [DomState(**d) for d in await self.page.evaluate(JS_ALL_STATES, self.sel)]

    # ---------- 页面 ----------
    async def exam_info(self) -> ExamInfo:
        if self.s.paper is not None:
            info, _ = parse_do_homework(self.s.paper)
            d = await self.page.evaluate(JS_EXAM_INFO, self.sel)
            info.deadline = d["deadline"]
            return info
        d = await self.page.evaluate(JS_EXAM_INFO, self.sel)
        return ExamInfo(course=d["course"] or "未知课程", title=d["title"] or "未知测试",
                        chapter=d["chapter"], deadline=d["deadline"],
                        total=int(d["total"]) if d["total"] else None)

    async def card(self) -> list[CardItem]:
        return [CardItem(**c) for c in await self.page.evaluate(JS_CARD, self.sel)]

    async def _current_no(self) -> int | None:
        d = await self.page.evaluate(JS_READ_QUESTION, self.sel)
        return parse_header(d["header_text"])[0]

    async def goto(self, no: int) -> None:
        if await self._current_no() == no:
            return
        items = self.page.locator(self.sel["card_item"])
        target = items.filter(has_text=re.compile(rf"^\s*{no}\s*$"))
        if await target.count() == 0:
            target = items.nth(no - 1)
        await safe_click(target.first, purpose="card", log=self.log)
        deadline = time.monotonic() + self.cfg.render_timeout_ms / 1000
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            if await self._current_no() == no:
                await asyncio.sleep(0.2)
                return
        raise RuntimeError(f"切换到第 {no} 题超时")

    async def read_current(self) -> RawQuestion:
        d = await self.page.evaluate(JS_READ_QUESTION, self.sel)
        no, type_text, score = parse_header(d["header_text"])
        if no is None:
            raise RuntimeError("读不到题号/题型（question_header 选择器可能需要修正）")
        opts = [RawOption(**o) for o in d["options"]]
        stem_html, pid = d["stem_html"], None
        api = self._api_by_ids.get(frozenset(o.option_id for o in opts if o.option_id))
        if api is not None:  # DOM 题干为空 → 用接口题干
            stem_html, pid = api.stem_html, api.eid
            content = {o.id: o.content for o in api.options}
            for o in opts:
                if o.option_id in content:
                    o.html = content[o.option_id]
        return RawQuestion(no=no, type_text=type_text, score=score, stem_html=stem_html,
                           options=opts, platform_id=pid)

    async def _option_handles(self):
        root = self.page.locator(self.sel["question_root"]).filter(visible=True).first
        return root.locator(self.sel["option_item"]).filter(visible=True)

    async def click_option(self, index: int) -> None:
        item = (await self._option_handles()).nth(index)
        target = item.locator(self.sel["option_click_target"]).first if self.sel["option_click_target"] else item
        await safe_click(target, purpose="option", log=self.log)
        await asyncio.sleep(0.3)

    def _button(self, text: str):
        return self.page.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")).filter(visible=True).first

    async def click_next(self) -> bool:
        btn = self._button(self.sel["next_button_text"])
        if await btn.count():
            if await btn.is_disabled():
                return False
            await safe_click(btn, purpose="next", log=self.log)
            await asyncio.sleep(0.6)
            return True
        last = self._button(self.sel["last_save_button_text"])
        if await last.count():
            await safe_click(last, purpose="last_save", log=self.log)
            await asyncio.sleep(1.0)
        return False

    async def _confirm_message_box(self, must_contain: str, *, guard_token: str | None = None) -> bool:
        box = self.page.locator(".el-message-box").filter(visible=True).filter(has_text=must_contain)
        try:
            await box.first.wait_for(state="visible", timeout=4000)
        except Exception:  # noqa: BLE001
            return False
        for txt in self.cfg.submit_confirm_texts:
            b = box.first.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(txt)}\s*$"))
            if await b.count():
                await safe_click(b.first, purpose="confirm", log=self.log, guard=self.guard, token=guard_token)
                await asyncio.sleep(1.5)
                return True
        return False

    async def click_save_progress(self) -> dict:
        """点「暂存作业」→ 确认框「确定」。平台暂存成功后会 window.close() 关闭做题页（saveSuccess）。"""
        page = self.page
        await safe_click(self._button(self.sel["save_button_text"]), purpose="save_progress", log=self.log)
        box = page.locator(".el-message-box").filter(visible=True).filter(has_text="暂存")
        try:
            await box.first.wait_for(state="visible", timeout=4000)
        except Exception:  # noqa: BLE001
            return {"confirmed": False, "page_closed": page.is_closed(), "toast": None}
        await safe_click(box.first.locator("button").filter(has_text=re.compile(r"^\s*确定\s*$")).first,
                         purpose="confirm", log=self.log)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not page.is_closed():
            await asyncio.sleep(0.3)
        if page.is_closed():
            self.s.page = None
            return {"confirmed": True, "page_closed": True, "toast": None}
        toast = await page.evaluate(JS_TOAST)
        err = await self.dialog_text()
        return {"confirmed": True, "page_closed": False, "toast": toast, "dialog": err}

    async def click_submit(self, token: str) -> str | None:
        self.s.allow_dialog_accept = True
        try:
            await safe_click(self._button(self.cfg.submit_button_text), purpose="submit",
                             log=self.log, guard=self.guard, token=token)
            await asyncio.sleep(1.0)
            box = self.page.locator(".el-message-box").filter(visible=True)
            text = (await box.first.inner_text()) if await box.count() else ""
            confirmed = await self._confirm_message_box("", guard_token=token) if text else False
            await asyncio.sleep(1.5)
            toast = await self.page.evaluate(JS_TOAST)
            return f"确认框：{text.strip()[:80] or '无'}；已确认：{confirmed}；提示：{toast or ''}"
        finally:
            self.s.allow_dialog_accept = False

    async def dialog_text(self) -> str | None:
        if self.s.dialogs:
            return "原生弹窗：" + self.s.dialogs.pop()
        return await self.page.evaluate(JS_DIALOG, self.sel["dialog"])

    async def reload(self) -> None:
        await self.s.reload_and_capture()

    async def screenshot(self, name: str) -> str | None:
        d: Path = self.cfg.screenshots_dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}.png"
        try:
            await self.page.screenshot(path=str(p))
            return str(p)
        except Exception as e:  # noqa: BLE001
            log.warning("screenshot failed: %s", e)
            return None


JS_VISIBLE_BOX = r"""
() => {
  const shown = e => { const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.top >= innerHeight) return false;
    for (let c = e; c && c.nodeType === 1; c = c.parentElement) { const st = getComputedStyle(c);
      if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) return false; }
    return true; };
  const els = [...document.querySelectorAll('.el-message-box, .el-dialog, .layui-layer, [id^=tcaptcha], .yidun_popup, .yidun_modal')].filter(shown);
  return els.length ? els.map(e => (e.innerText || e.id || e.className).trim()).join(' | ').slice(0, 300) || '(无文字弹窗)' : null;
}
"""


class DomListAdapter:
    """「作业考试」列表页：读列表、点「开始答题/继续答题」打开做题页。"""

    def __init__(self, session: BrowserSession, action_log):
        self.s = session
        self.cfg = session.cfg
        self.lc = session.cfg.exam_list
        self.log = action_log

    async def items(self, refresh: bool = True):
        from .exam_list import parse_homework_list
        page = await self.s.list_page()
        if refresh or self.s.homework is None:
            seq = self.s.homework_seq
            await page.reload(wait_until="networkidle")
            deadline = time.monotonic() + 15
            while self.s.homework_seq == seq and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            if self.s.homework_seq == seq:
                raise RuntimeError("刷新作业考试列表后没有捕获到 getStudentHomework 响应")
            await asyncio.sleep(1.0)
        return parse_homework_list(self.s.homework or {}, self.lc.allowed_name_pattern)

    async def open(self, name: str) -> None:
        from .adapter import OpenBlocked
        page = await self.s.list_page()
        box = await page.evaluate(JS_VISIBLE_BOX)
        if box:
            raise OpenBlocked(f"列表页有弹窗，未点击：{box}")
        card = page.locator(self.lc.card).filter(has=page.locator(self.lc.card_title, has_text=name))
        if await card.count() != 1:
            raise RuntimeError(f"列表中找到 {await card.count()} 个「{name}」卡片（需要恰好 1 个）")
        pat = "|".join(re.escape(t) for t in self.lc.start_button_texts)
        btn = card.locator(self.lc.start_button).filter(has_text=re.compile(rf"^\s*({pat})\s*$"))
        if await btn.count() == 0:
            raise RuntimeError(f"「{name}」没有「开始答题/继续答题」按钮（可能未开放或已截止）")
        before = {id(p) for p in self.s.exam_pages()}
        if before:
            raise RuntimeError("已有做题页开着，请先处理完（暂存/提交）或关闭它，再打开下一个测试")
        self.s.paper = self.s.answers = None
        self.s.page = None  # 让新做题页首次加载的 doHomework 能被接收
        await safe_click(btn.first, purpose="open_exam", log=self.log)
        deadline = time.monotonic() + self.lc.open_timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            new = [p for p in self.s.exam_pages() if id(p) not in before]
            if new:
                p = new[0]
                self.s.use_exam_page(p, keep_capture=True)
                try:
                    await p.wait_for_load_state("networkidle", timeout=15000)
                except Exception:  # noqa: BLE001
                    pass
                t_end = time.monotonic() + 8   # 等首次加载的 doHomework（拿不到时 extract 会刷新）
                while self.s.paper is None and time.monotonic() < t_end:
                    await asyncio.sleep(0.2)
                await asyncio.sleep(1.0)
                self.log("open_exam", name=name, paper_captured=self.s.paper is not None)
                return
            box = await page.evaluate(JS_VISIBLE_BOX)
            if box:
                self.log("open_blocked", name=name, dialog=box)
                raise OpenBlocked(f"点击「{name}」后平台弹出提示，需要你处理：{box}")
        raise RuntimeError(f"点击「{name}」后 {self.lc.open_timeout_s:.0f} 秒内没有打开做题页")
