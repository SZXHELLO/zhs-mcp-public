"""填写：按选项文本匹配 → 点选（多选只点差异）→ 校验 → 保存（下一题）。"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from .adapter import PageAdapter
from .store import compute_qid, norm, same_answer
from .config import Config
from .extractor import raw_texts
from .models import ExamFile, Question
from .safe_click import ForbiddenClick


class AbortFill(RuntimeError):
    pass


@dataclass
class FillReport:
    filled: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    planned: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    aborted: str | None = None
    remaining: int = 0

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], 0) or k == "remaining"}


def select_targets(exam: ExamFile, scope: str, nos: list[int] | None, allow_user: bool = False) -> list[Question]:
    qs = sorted(exam.questions, key=lambda q: q.no)
    if scope == "nos":
        wanted = set(nos or [])
        return [q for q in qs if q.no in wanted]
    # unanswered：页面未答 或 工具填过但与 final_answer 不一致
    out = []
    for q in qs:
        if q.origin == "empty" or (q.origin == "tool" and not same_answer(q.page_answer, q.final_answer)):
            out.append(q)
        elif allow_user and q.user_decision and not same_answer(q.page_answer, q.final_answer):
            out.append(q)
    return out


class Filler:
    def __init__(self, adapter: PageAdapter, cfg: Config, action_log=None, sleep=asyncio.sleep):
        self.a = adapter
        self.cfg = cfg
        self.log = action_log or (lambda *a, **k: None)
        self.sleep = sleep

    async def fill(self, exam: ExamFile, targets: list[Question], *, allow_user: bool = False,
                   dry_run: bool = False, max_items: int = 10) -> FillReport:
        rep = FillReport()
        start_url = await self.a.url()
        total = max((q.no for q in exam.questions), default=0)
        consecutive_fail = 0
        processed = 0
        queue = list(targets)
        while queue and processed < max_items:
            q = queue.pop(0)
            if q.origin == "user" and not allow_user:
                rep.skipped.append({"no": q.no, "reason": "用户自己作答的题，不修改"})
                continue
            if not q.final_answer:
                rep.skipped.append({"no": q.no, "reason": "没有 final_answer"})
                continue
            processed += 1
            try:
                await self._check_env(start_url)
                status = await self._fill_one(q, is_last=(q.no == total), dry_run=dry_run, rep=rep)
                consecutive_fail = 0
                if status == "filled":
                    rep.filled.append(q.no)
                elif status == "unchanged":
                    rep.unchanged.append(q.no)
            except AbortFill as e:
                rep.aborted = str(e)
                shot = await self.a.screenshot(f"abort_q{q.no}")
                self.log("fill_abort", no=q.no, reason=str(e), screenshot=shot)
                queue.insert(0, q)
                break
            except (ForbiddenClick, Exception) as e:  # noqa: BLE001
                consecutive_fail += 1
                q.fill_error = str(e)
                shot = await self.a.screenshot(f"fail_q{q.no}")
                rep.failed.append({"no": q.no, "error": str(e), "screenshot": shot})
                self.log("fill_fail", no=q.no, error=str(e), screenshot=shot)
                if consecutive_fail >= self.cfg.max_consecutive_failures:
                    rep.aborted = f"连续 {consecutive_fail} 题失败"
                    break
        rep.remaining = len([q for q in queue if q.final_answer and (allow_user or q.origin != "user")])
        return rep

    async def _check_env(self, start_url: str) -> None:
        d = await self.a.dialog_text()
        if d:
            raise AbortFill(f"出现弹窗：{d}")
        if await self.a.url() != start_url:
            raise AbortFill("页面 URL 已变化")

    async def _fill_one(self, q: Question, *, is_last: bool, dry_run: bool, rep: FillReport) -> str:
        await self.a.goto(q.no)
        raw = await self.a.read_current()
        stem, opts, _ = raw_texts(raw)
        if compute_qid(stem, opts) != q.qid:
            raise RuntimeError("题目与题库不一致（qid 不匹配），请重新 extract_questions")
        counts: dict[str, int] = {}
        for t in opts:
            counts[norm(t)] = counts.get(norm(t), 0) + 1
        index_of = {norm(t): i for i, t in enumerate(opts) if counts[norm(t)] == 1}
        target = set()
        for ans in q.final_answer or []:
            i = index_of.get(norm(ans))
            if i is None and counts.get(norm(ans), 0) > 1 and not (q.snapshot and q.snapshot.option_ids):
                raise RuntimeError(f"多个选项文本相同（{ans[:20]!r}），无法区分，跳过")
            if i is None and q.snapshot and q.snapshot.option_ids:
                # 退化：按快照 option_id 匹配
                inv = {v: q.snapshot.option_ids.get(k) for k, v in q.snapshot.label_map.items()}
                oid = inv.get(ans)
                i = next((j for j, o in enumerate(raw.options) if oid and o.option_id == oid), None)
            if i is None:
                raise RuntimeError(f"选项匹配不上：{ans[:30]!r}")
            target.add(i)
        if q.type in ("single", "judge") and len(target) != 1:
            raise RuntimeError(f"单选题目标答案数为 {len(target)}")
        current = {i for i, o in enumerate(raw.options) if o.selected}
        if current == target:
            q.page_answer = [opts[i] for i in sorted(target)]
            q.fill_error = None
            return "unchanged"
        # 单选：点目标；多选：点击是切换，只点差异部分
        clicks = sorted(current ^ target) if q.type == "multi" else [next(iter(target))]
        if dry_run:
            rep.planned.append({"no": q.no, "click_indexes": clicks, "target": [opts[i] for i in sorted(target)]})
            return "planned"
        for i in clicks:
            await self.a.click_option(i)
        after = {i for i, o in enumerate((await self.a.read_current()).options) if o.selected}
        if after != target:
            raise RuntimeError(f"点选后状态不符：期望 {sorted(target)}，实际 {sorted(after)}")
        await self._save(is_last)
        q.origin = "tool"
        q.page_answer = [opts[i] for i in sorted(target)]
        q.fill_error = None
        self.log("filled", no=q.no, qid=q.qid)
        await self.sleep(random.uniform(self.cfg.delay_min, self.cfg.delay_max))
        return "filled"

    async def _save(self, is_last: bool) -> None:
        if self.cfg.save_action != "next":
            return
        moved = await self.a.click_next()
        if not moved and self.cfg.last_question_save == "save_progress":
            await self.a.click_save_progress()
