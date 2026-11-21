"""假页面：模拟一屏一题、选项随机重排（reload 时）、点「下一题」才保存。"""
from __future__ import annotations

import random

from zhs_exam_mcp.adapter import PageAdapter
from zhs_exam_mcp.extractor_api import ApiOption, ApiQuestion, DomState
from zhs_exam_mcp.models import CardItem, ExamInfo, RawOption, RawQuestion
from zhs_exam_mcp.safe_click import check_click


class FakeQ:
    def __init__(self, no, type_text, stem_html, options, saved=()):
        self.no, self.type_text, self.stem_html = no, type_text, stem_html
        self.options = list(options)          # 显示顺序（html 文本）
        self.ids = {o: f"{no}{i:02d}" for i, o in enumerate(options)}  # 稳定选项 id
        self.saved = set(saved)               # 服务器端已保存（按选项内容）
        self.ui = set(saved)                  # 页面当前勾选（按选项内容）


class FakePage(PageAdapter):
    def __init__(self, qs: list[FakeQ], seed=0, guard=None, title="第三章单元测试"):
        self.title = title
        self.qs = {q.no: q for q in qs}
        self.cur = 1
        self.rng = random.Random(seed)
        self._url = "https://onlineexamh5new.zhihuishu.com/x"
        self.dialog = None
        self.clicks: list[tuple] = []
        self.submitted = False
        self.guard = guard
        self.fail_click_on: set[int] = set()

    async def url(self):
        return self._url

    async def exam_info(self):
        return ExamInfo(course="计算机组成与系统结构", title=self.title, chapter="第三章", total=len(self.qs))

    async def card(self):
        return [CardItem(no=n, answered=bool(q.saved), current=n == self.cur) for n, q in sorted(self.qs.items())]

    async def goto(self, no):
        self.cur = no

    async def read_current(self):
        q = self.qs[self.cur]
        return RawQuestion(no=q.no, type_text=q.type_text, score=2, stem_html=q.stem_html,
                           options=[RawOption(label="ABCD"[i] + ".", html=f"{'ABCD'[i]}. {o}", selected=o in q.ui,
                                              option_id=q.ids[o])
                                    for i, o in enumerate(q.options)])

    async def paper(self, refresh=False):
        if refresh:
            await self.reload()
        info = await self.exam_info()
        return info, [ApiQuestion(no=q.no, eid=f"e{q.no}", type_text=q.type_text, score=2, stem_html=q.stem_html,
                                  options=[ApiOption(id=q.ids[o], content=o + "<br>") for o in q.options])
                      for q in sorted(self.qs.values(), key=lambda x: x.no)]

    async def all_states(self):
        return [DomState(index=i, option_ids=[q.ids[o] for o in q.options],
                         selected_ids=[q.ids[o] for o in q.ui])
                for i, q in enumerate(sorted(self.qs.values(), key=lambda x: x.no))]

    async def click_option(self, index):
        q = self.qs[self.cur]
        self.clicks.append(("option", self.cur, index))
        if self.cur in self.fail_click_on:
            return
        o = q.options[index]
        if q.type_text == "多选题":
            q.ui ^= {o}
        else:
            q.ui = {o}

    async def click_next(self):
        q = self.qs[self.cur]
        q.saved = set(q.ui)
        self.clicks.append(("next", self.cur))
        if self.cur == max(self.qs):
            return False
        self.cur += 1
        return True

    async def click_save_progress(self):
        self.clicks.append(("save",))
        return {"confirmed": True, "page_closed": True, "toast": None}

    async def click_submit(self, token):
        check_click("提交作业", self.guard, token)
        self.submitted = True
        return "提交成功"

    async def dialog_text(self):
        return self.dialog

    async def reload(self):
        for q in self.qs.values():
            self.rng.shuffle(q.options)
            q.ui = set(q.saved)
        self.cur = 1


def sample_page(seed=0, guard=None, title="第三章单元测试") -> FakePage:
    return FakePage([
        FakeQ(1, "单选题", "动态RAM刷新方式不包括（ ）", ["分散刷新", "集中刷新", "异步刷新", "静态刷新"]),
        FakeQ(2, "多选题", "以下属于易失性存储器的是", ["SRAM", "DRAM", "ROM", "Flash"]),
        FakeQ(3, "单选题", '容量为 <script type="math/tex">2^{10}</script> 字节是', ["1KB", "1MB", "1GB", "1TB"],
              saved={"1MB"}),  # 用户自己答过
        FakeQ(4, "单选题", "Cache 的作用是", ["提高速度", "扩大容量", "降低功耗", "增加可靠性"]),
    ], seed=seed, guard=guard, title=title)
