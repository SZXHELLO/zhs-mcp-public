"""业务层：MCP 工具的具体实现（与 FastMCP 解耦，便于离线测试）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .adapter import ListAdapter, OpenBlocked, PageAdapter
from .exam_list import BatchItem, BatchState, BatchStore, resolve_targets
from .store import ExamStore, norm, same_answer
from .config import Config
from .extractor import LETTERS, new_snapshot_id, raw_to_question
from .extractor_api import merge_api_dom
from .filler import Filler, select_targets
from .models import AIAnswer, ExamFile, ExamInfo, Question, UserDecision, now
from .review import build_review, review_item
from .safe_click import SubmitGuard
from .submit import VerifyState, check_submit_conditions


class ToolError(RuntimeError):
    pass


class ActionLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: str, **kw) -> None:
        rec = {"at": now(), "event": event, **kw}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _letters_list(ans) -> list[str]:
    if isinstance(ans, str):
        ans = [ans]
    out: list[str] = []
    for a in ans:
        a = a.strip()
        if a and len(a) > 1 and all(c.upper() in LETTERS for c in a):
            out += [c.upper() for c in a]   # "BD" → ["B","D"]
        else:
            out.append(a.upper() if len(a) == 1 else a)
    return out


class ExamService:
    def __init__(self, cfg: Config, adapter: PageAdapter | None = None, guard: SubmitGuard | None = None,
                 action_log: ActionLog | None = None, sleep=None):
        self.cfg = cfg
        self.store = ExamStore()
        self.adapter = adapter
        self.guard = guard or SubmitGuard()
        self.log = action_log or ActionLog(cfg.logs_dir / "actions.jsonl")
        self.exam: ExamFile | None = None
        self.info: ExamInfo | None = None
        self.verify_state = VerifyState()
        self._sleep = sleep
        self.list_adapter: ListAdapter | None = None
        self.batches = BatchStore(cfg.runtime_dir / "batch_state.json")

    # ---------- helpers ----------
    def _require(self) -> tuple[PageAdapter, ExamFile]:
        if self.adapter is None or self.exam is None:
            raise ToolError("请先调用 connect")
        return self.adapter, self.exam

    def _exam_for(self, exam_key: str | None) -> ExamFile:
        if exam_key:
            return self.store.load_exam_by_key(exam_key)
        if self.exam is None:
            raise ToolError("请先 connect，或传入 exam_key（形如「课程/测试名」）")
        return self.exam

    def _filler(self) -> Filler:
        kw = {"sleep": self._sleep} if self._sleep else {}
        return Filler(self.adapter, self.cfg, self.log, **kw)  # type: ignore[arg-type]

    # ---------- tools ----------
    async def connect(self) -> dict:
        assert self.adapter is not None
        info = await self.adapter.exam_info()
        self.info = info
        exam = self.store.load_exam(info.course, info.title)
        exam.chapter, exam.deadline, exam.total = info.chapter, info.deadline, info.total
        self.exam = exam
        self.verify_state = VerifyState()
        self.log("connect", exam_key=exam.exam_key)
        return {"exam_key": exam.exam_key, **info.model_dump(), "questions_cached": len(exam.questions)}

    async def get_overview(self) -> dict:
        adapter, exam = self._require()
        card = await adapter.card()
        rows = []
        for c in card:
            q = exam.by_no(c.no)
            rows.append({"no": c.no, "page_answered": c.answered,
                         "origin": q.origin if q else None, "has_ai": bool(q and q.ai),
                         "decided": bool(q and q.user_decision)})
        done = sum(1 for c in card if c.answered)
        return {"exam_key": exam.exam_key, "total": len(card),
                "completion": f"{done}/{len(card)}", "questions": rows}

    async def _read_raws(self, nos: list[int] | None, refresh: bool) -> tuple[list, list[dict]]:
        """读取 RawQuestion。api 模式：接口题干 + DOM 选中（不切题）；dom 模式：逐题切换。"""
        adapter, _ = self._require()
        errors: list[dict] = []
        if self.cfg.extract_mode == "api":
            info, api_qs = await adapter.paper(refresh=refresh)
            self.info = info if not self.info else self.info.model_copy(update={"total": info.total})
            # 服务器作答记录只在本次刷新时可信（之后可能又填写过）
            server = await adapter.server_answers() if refresh else None
            raws, warns = merge_api_dom(api_qs, await adapter.all_states(), server)
            errors += [{"warning": w} for w in warns]
            if nos:
                raws = [r for r in raws if r.no in set(nos)]
            return raws, errors
        if refresh:
            await adapter.reload()
        if not nos:
            card = await adapter.card()
            nos = [c.no for c in card] or list(range(1, ((self.info.total if self.info else 0) or 0) + 1))
        raws = []
        for no in nos:
            try:
                await adapter.goto(no)
                raws.append(await adapter.read_current())
            except Exception as e:  # noqa: BLE001
                errors.append({"no": no, "error": str(e)})
        return raws, errors

    async def extract_questions(self, nos: list[int] | None = None, refresh: bool = False) -> dict:
        _, exam = self._require()
        raws, errors = await self._read_raws(nos, refresh)
        sid = new_snapshot_id()
        out = []
        for raw in raws:
            fresh = raw_to_question(raw, exam.exam_key, sid)
            q = ExamStore.merge(exam, fresh)
            lm = q.snapshot.label_map  # type: ignore[union-attr]
            inv = {norm(v): k for k, v in lm.items()}
            out.append({
                "qid": q.qid, "no": q.no, "type": q.type, "score": q.score, "stem": q.stem,
                "options": lm, "page_answer": [inv.get(norm(a), "?") for a in q.page_answer],
                "origin": q.origin, "has_ai": q.ai is not None,
                **({"formula_lossy": True} if q.formula_lossy else {}),
            })
        if self.info:
            exam.total = self.info.total
        self.store.save_exam(exam)
        self.verify_state.invalidate()
        res = {"snapshot_id": sid, "count": len(out), "questions": out}
        if errors:
            res["errors"] = errors
        if self.info and self.info.total and len(exam.questions) != self.info.total:
            res["warning"] = f"已读题数 {len(exam.questions)} ≠ 页面题目数 {self.info.total}"
        return res

    async def save_answers(self, snapshot_id: str, items: list[dict], model: str = "") -> dict:
        _, exam = self._require()
        saved, errors, warnings = [], [], []
        for it in items:
            q = exam.by_qid(it.get("qid", ""))
            if q is None:
                errors.append({"qid": it.get("qid"), "error": "已读题目中无此 qid"})
                continue
            if q.snapshot is None or q.snapshot.snapshot_id != snapshot_id:
                errors.append({"qid": q.qid, "no": q.no, "error": "快照已过期，请重新 extract_questions"})
                continue
            letters = _letters_list(it.get("answer", []))
            bad = [l for l in letters if l not in q.snapshot.label_map]
            if bad:
                errors.append({"qid": q.qid, "no": q.no, "error": f"无效字母 {bad}"})
                continue
            letters = sorted(set(letters))
            if q.type in ("single", "judge") and len(letters) != 1:
                errors.append({"qid": q.qid, "no": q.no, "error": "单选题必须恰好 1 个答案"})
                continue
            if q.type == "multi" and len(letters) < 2:
                warnings.append({"qid": q.qid, "no": q.no, "warning": "多选题少于 2 个答案，已保存，请确认"})
            try:
                conf = float(it.get("confidence", 0.5))
            except (TypeError, ValueError):
                conf = 0.5
            q.ai = AIAnswer(answer=[q.snapshot.label_map[l] for l in letters],
                            explanation=str(it.get("explanation", "")),
                            confidence=min(max(conf, 0.0), 1.0), model=model)
            saved.append(q.no)
        self.store.save_exam(exam)
        res: dict = {"saved": saved}
        if errors:
            res["errors"] = errors
        if warnings:
            res["warnings"] = warnings
        return res

    async def fill_answers(self, scope: str = "unanswered", nos: list[int] | None = None,
                           dry_run: bool = False, max_items: int = 10, *, allow_user: bool = False) -> dict:
        adapter, exam = self._require()
        targets = select_targets(exam, scope, nos, allow_user=allow_user)
        if targets and self.cfg.extract_mode == "api":
            # DOM 题干为空：填写时的 qid 校验依赖接口题干；本进程尚未捕获时先（刷新）捕获
            await adapter.paper()
        rep = await self._filler().fill(exam, targets, allow_user=allow_user, dry_run=dry_run,
                                        max_items=max(1, min(max_items, 25)))
        if not dry_run:
            self.verify_state.invalidate()
            self.store.save_exam(exam)
        return rep.as_dict()

    async def save_progress(self) -> dict:
        """点「暂存作业」。注意：平台暂存成功后会自动关闭做题页，之后需用户重新打开测试页并 connect。
        每题的答案在「下一题」/「保存」时已存到服务器，暂存只是收尾。"""
        adapter, _ = self._require()
        d = await adapter.dialog_text()
        if d:
            raise ToolError(f"页面有弹窗，未点击暂存：{d}")
        r = await adapter.click_save_progress()
        ok = bool(r.get("page_closed")) or (r.get("confirmed") and not r.get("dialog"))
        self.log("save_progress", **r)
        res = {"ok": ok, **r}
        if r.get("page_closed"):
            self.verify_state.invalidate()
            res["note"] = "暂存成功，平台已自动关闭做题页。若还要继续，请重新打开该章节测试后再 connect。"
            self._batch_mark(status="saved")
            st = self.batches.load()
            if st is not None:
                nxt = st.next_pending()
                res["batch_next"] = nxt.name if nxt else None
                res["note"] = ("暂存成功，做题页已关闭。" +
                               (f"批量任务下一项：{nxt.name}（调用 open_exam）" if nxt else
                                "批量任务全部暂存完成：调用 batch_status，然后一次性向用户确认是否提交"))
        elif not r.get("confirmed"):
            res["note"] = "没有出现暂存确认框，暂存可能未执行"
        return res

    async def verify(self, reload: bool = True, nos: list[int] | None = None) -> dict:
        _, exam = self._require()
        raws, errors = await self._read_raws(nos, refresh=reload)
        sid = new_snapshot_id()
        mismatches = []
        for raw in raws:
            fresh = raw_to_question(raw, exam.exam_key, sid)
            if exam.by_qid(fresh.qid) is None:
                errors.append({"no": raw.no, "error": "题目不在已读题目中，请先 extract_questions"})
                continue
            q = ExamStore.merge(exam, fresh)
            if q.fill_error and q.final_answer and same_answer(q.page_answer, q.final_answer):
                q.fill_error = None  # 页面/服务器已是目标答案
            if q.final_answer and not same_answer(q.page_answer, q.final_answer):
                mismatches.append({"no": q.no, "origin": q.origin,
                                   "page": q.page_answer, "final": q.final_answer})
        self.store.save_exam(exam)
        hard_errors = [e for e in errors if "error" in e]
        full = not nos and reload and not hard_errors and len(raws) == len(exam.questions)
        self.verify_state = VerifyState(ok=full and not mismatches, exam_key=exam.exam_key,
                                        mismatches=len(mismatches))
        self.log("verify", checked=len(raws), mismatches=len(mismatches), errors=len(errors))
        self._batch_mark(verify_ok=self.verify_state.ok)
        return {"checked": len(raws), "mismatches": mismatches, "errors": errors,
                "source": "server" if (self.cfg.extract_mode == "api" and reload
                                       and await self.adapter.server_answers() is not None) else "page",
                "snapshot_id": sid, "all_consistent": self.verify_state.ok}

    async def get_review_list(self, exam_key: str | None = None, threshold: float | None = None) -> dict:
        exam = self._exam_for(exam_key)
        th = self.cfg.low_confidence_threshold if threshold is None else threshold
        groups = build_review(exam, th)
        return {"threshold": th,
                "count": sum(len(v) for v in groups.values()),
                "groups": {k: [review_item(q) for q in v] for k, v in groups.items()},
                "note": "字母按首次读题时的选项顺序 A–H，apply_user_decisions 使用同一套字母"}

    def _decision_texts(self, q: Question, answer) -> list[str]:
        out = []
        for a in _letters_list(answer):
            if len(a) == 1 and a in LETTERS[: len(q.options)]:
                out.append(q.options[LETTERS.index(a)])
            else:
                hit = next((o for o in q.options if norm(o) == norm(a)), None)
                if hit is None:
                    raise ToolError(f"第 {q.no} 题：答案 {a!r} 既不是有效字母也不匹配任何选项文本")
                out.append(hit)
        if not out:
            raise ToolError(f"第 {q.no} 题：答案为空")
        if q.type in ("single", "judge") and len(out) != 1:
            raise ToolError(f"第 {q.no} 题是单选题，必须恰好 1 个答案")
        return out

    async def apply_user_decisions(self, items: list[dict], fill: bool = True) -> dict:
        exam = self.exam
        if exam is None:
            raise ToolError("请先调用 connect")
        decided = []
        for it in items:
            q = exam.by_qid(it.get("qid", ""))
            if q is None:
                raise ToolError(f"已读题目中无此 qid：{it.get('qid')}")
            q.user_decision = UserDecision(answer=self._decision_texts(q, it.get("answer", [])))
            decided.append(q.no)
        self.store.save_exam(exam)
        self.verify_state.invalidate()
        res: dict = {"decided": decided}
        if fill and self.adapter is not None:
            res["fill"] = await self.fill_answers("nos", decided, max_items=25, allow_user=True)
        return res

    async def submit_exam(self, confirm: bool = False) -> dict:
        adapter, exam = self._require()
        info = await adapter.exam_info()
        unmet = check_submit_conditions(exam, info.total, self.verify_state,
                                        self.cfg.low_confidence_threshold, confirm)
        st = self.batches.load()
        if st is not None and not st.all_saved:
            left = [i.name for i in st.items if i.status not in ("saved", "submitted")]
            unmet.insert(0, f"批量任务未完成，所有章节暂存后才能提交（未完成：{left}）")
        if unmet:
            self.log("submit_refused", unmet=unmet)
            return {"submitted": False, "unmet": unmet}
        token = self.guard.issue()
        try:
            msg = await adapter.click_submit(token)
        finally:
            self.guard.revoke()
        shot = await adapter.screenshot("after_submit")
        self.log("submit", exam_key=exam.exam_key, toast=msg, screenshot=shot)
        self.verify_state.invalidate()
        self._batch_mark(status="submitted")
        return {"submitted": True, "toast": msg, "screenshot": shot, "at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # ---------- 作业考试列表 / 批量 ----------
    def _batch_mark(self, status: str | None = None, **kw) -> None:
        st = self.batches.load()
        if st is None or self.exam is None:
            return
        if st.get(self.exam.title) is not None:
            st.mark(self.exam.title, status, **kw)
            self.batches.save(st)

    def _require_list(self) -> ListAdapter:
        if self.list_adapter is None:
            raise ToolError("列表页适配器未初始化")
        return self.list_adapter

    async def list_exams(self, refresh: bool = True) -> dict:
        items = await self._require_list().items(refresh=refresh)
        rows = [{"chapter": it.chapter, "name": it.name, "problem_num": it.problem_num,
                 "answered_on_platform": it.answered, "status": it.status_text, "end_date": it.end_date}
                for it in items]
        return {"exams": rows, "note": "只列出「未提交」的章节测试；期末在线考试不在此列，也不会被处理"}

    async def start_batch(self, chapters: list) -> dict:
        old = self.batches.load()
        if old is not None and old.stopped is None and any(i.status in ("pending", "in_progress") for i in old.items):
            raise ToolError(f"已有未完成的批量任务：{[i.name for i in old.items]}。"
                            "继续用 batch_status / open_exam，或先 end_batch")
        items = await self._require_list().items(refresh=True)
        targets, missing = resolve_targets(items, chapters)
        if not targets:
            raise ToolError("没有可做的章节测试：" + "；".join(missing))
        st = BatchState(course=targets[0].course,
                        items=[BatchItem(name=t.name, chapter=t.chapter) for t in targets])
        self.batches.save(st)
        self.log("batch_start", items=[t.name for t in targets], missing=missing)
        return {"batch": [i.model_dump(include={"name", "chapter", "status"}) for i in st.items],
                "missing": missing,
                "rules": "逐个完成：open_exam → extract_questions → 作答 save_answers → fill_answers → verify → "
                         "get_review_list（只记录，不询问）→ save_progress（暂存）。"
                         "全部暂存前不提交、不询问提交；完成后调用 batch_status，一次性向用户确认。"}

    async def open_exam(self, chapter) -> dict:
        lst = self._require_list()
        st = self.batches.load()
        if st is not None and st.stopped:
            raise ToolError(f"批量任务已中止：{st.stopped}。处理后调用 end_batch 或重新 start_batch")
        items = await lst.items(refresh=True)
        targets, missing = resolve_targets(items, [chapter])
        if not targets:
            raise ToolError(missing[0])
        name = targets[0].name
        try:
            await lst.open(name)
        except OpenBlocked as e:
            if st is not None and st.get(name):
                st.mark(name, "failed", note=str(e))
                st.stopped = str(e)
                self.batches.save(st)
            raise ToolError(f"{e}。为避免触发平台的异常行为判定，已停止，请你在浏览器中处理后再告诉我") from e
        res = await self.connect()
        if res.get("title") != name:
            raise ToolError(f"打开的做题页是「{res.get('title')}」，与目标「{name}」不一致，已停止")
        it = st.get(name) if st is not None else None
        if it is not None and it.status in ("pending", "failed"):
            st.mark(name, "in_progress")  # 已暂存的章节（为提交而重新打开）保持 saved
            self.batches.save(st)
        return {"opened": name, **res}

    async def batch_status(self) -> dict:
        st = self.batches.load()
        if st is None:
            return {"batch": None}
        th = self.cfg.low_confidence_threshold
        course = st.course or (self.info.course if self.info else None)
        rows, reviews = [], []
        for it in st.items:
            row = it.model_dump()
            exam = self.store.get(course, it.name) if course else None
            if exam and exam.questions:
                groups = build_review(exam, th)
                row.update(questions=len(exam.questions),
                           with_answer=sum(1 for q in exam.questions if q.final_answer),
                           review_count=sum(len(v) for v in groups.values()), exam_key=exam.exam_key)
                if row["review_count"]:
                    reviews.append({"exam": it.name, "exam_key": exam.exam_key,
                                    "groups": {k: [review_item(q) for q in v] for k, v in groups.items() if v}})
            rows.append(row)
        nxt = st.next_pending()
        return {"all_saved": st.all_saved, "stopped": st.stopped, "items": rows, "pending_reviews": reviews,
                "next": nxt.name if nxt else None,
                "note": ("全部已暂存：把各章节结果和 pending_reviews 一次性展示给用户，询问是否提交（可只提交部分章节）"
                         if st.all_saved else "还有章节未完成：继续 open_exam 下一项；不要询问是否提交")}

    async def end_batch(self) -> dict:
        st = self.batches.load()
        self.batches.clear()
        final = [i.model_dump(include={"name", "status"}) for i in st.items] if st else None
        self.log("batch_end", items=final)
        return {"ended": True, "final": final}
