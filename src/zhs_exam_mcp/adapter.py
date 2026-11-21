"""页面适配器接口。业务逻辑（读题、填写、提交）只依赖这个接口；
DomAdapter（browser.py）是 Playwright 实现，测试中用假适配器替代。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import CardItem, ExamInfo, RawQuestion


class PageAdapter(ABC):
    @abstractmethod
    async def url(self) -> str: ...

    @abstractmethod
    async def exam_info(self) -> ExamInfo: ...

    @abstractmethod
    async def card(self) -> list[CardItem]: ...

    @abstractmethod
    async def goto(self, no: int) -> None:
        """点答题卡题号切到第 no 题，并等待题目区渲染为该题。"""

    @abstractmethod
    async def read_current(self) -> RawQuestion: ...

    @abstractmethod
    async def click_option(self, index: int) -> None:
        """点击当前题第 index 个（页面显示顺序，0 起）选项。"""

    @abstractmethod
    async def click_next(self) -> bool:
        """点「下一题」。返回 False 表示按钮不可用（最后一题）。"""

    @abstractmethod
    async def click_save_progress(self) -> dict:
        """点「暂存作业」并确认。返回 {confirmed, page_closed, toast, dialog?}。
        平台暂存成功后会自动关闭做题页（page_closed=True 即成功）。"""

    @abstractmethod
    async def click_submit(self, token: str) -> str | None:
        """点「提交作业」（需放行令牌）并处理确认弹窗，返回页面提示文字。"""

    @abstractmethod
    async def dialog_text(self) -> str | None:
        """当前是否有弹窗/对话框；有则返回其文字。"""

    @abstractmethod
    async def reload(self) -> None: ...

    async def screenshot(self, name: str) -> str | None:
        return None

    # ---- 模式 A（接口）可选能力 ----
    async def paper(self, refresh: bool = False):
        """返回 (ExamInfo, list[ApiQuestion])：页面加载时 doHomework 的解析结果。"""
        raise NotImplementedError

    async def all_states(self):
        """返回 list[DomState]：DOM 中每题的选项 id 与选中 id（无需切题）。"""
        raise NotImplementedError

    async def server_answers(self):
        """最近一次 reload 捕获的服务器作答记录 {eid: {选项id}}；无记录（未作答）时返回 None。"""
        return None


class ListAdapter(ABC):
    """「作业考试」列表页。"""

    @abstractmethod
    async def items(self, refresh: bool = True):
        """返回 list[ListItem]（未提交的章节测试）。"""

    @abstractmethod
    async def open(self, name: str) -> None:
        """点击该测试的「开始答题/继续答题」，等待做题页打开并切换到它；
        出现任何弹窗（人脸/定位/异常行为/锁定）则抛出 OpenBlocked。"""


class OpenBlocked(RuntimeError):
    pass
