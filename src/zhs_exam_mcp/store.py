"""已读题目的进程内存储：qid 计算、合并（不做任何文件 I/O）。

题目、AI 答案与用户裁决只保存在本次 MCP 服务器进程的内存中，服务器重启后需要重新读题作答。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from .models import ExamFile, Question, now


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip()


def compute_qid(stem: str, options: list[str]) -> str:
    key = norm(stem) + "|" + "|".join(sorted(norm(o) for o in options))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def same_answer(a: list[str] | None, b: list[str] | None) -> bool:
    return {norm(x) for x in (a or [])} == {norm(x) for x in (b or [])}


def make_exam_key(course: str, title: str) -> str:
    return f"{course}/{title}"


class ExamStore:
    def __init__(self) -> None:
        self._exams: dict[str, ExamFile] = {}

    def load_exam(self, course: str, title: str) -> ExamFile:
        """取已有的 ExamFile；没有就新建并登记（connect 用）。返回的是同一个对象，修改即生效。"""
        key = make_exam_key(course, title)
        if key not in self._exams:
            self._exams[key] = ExamFile(exam_key=key, course=course, title=title)
        return self._exams[key]

    def get(self, course: str, title: str) -> ExamFile | None:
        """只查不建（batch_status 用，避免给未打开的章节登记空记录）。"""
        return self._exams.get(make_exam_key(course, title))

    def load_exam_by_key(self, exam_key: str) -> ExamFile:
        """有就返回；没有返回一个空 ExamFile（不登记）。"""
        course, _, title = exam_key.partition("/")
        return self._exams.get(exam_key) or ExamFile(exam_key=exam_key, course=course, title=title)

    def save_exam(self, exam: ExamFile) -> None:
        """排序 + 更新时间 + 登记，不落盘。"""
        exam.questions.sort(key=lambda q: q.no)
        exam.updated_at = now()
        self._exams[exam.exam_key] = exam

    # ---- 合并 ----
    @staticmethod
    def merge(exam: ExamFile, fresh: Question) -> Question:
        """重新读题只更新 no / page_answer / snapshot / last_seen（以及推断 origin），
        不删除 ai / user_decision / correct_answer。"""
        old = exam.by_qid(fresh.qid)
        if old is None:
            fresh.origin = "user" if fresh.page_answer else "empty"
            # 同一测试里题号被另一道题占用（不应发生）→ 不处理，保留两条
            exam.questions.append(fresh)
            return fresh
        old.no = fresh.no
        old.snapshot = fresh.snapshot
        old.last_seen = fresh.last_seen
        old.platform_id = fresh.platform_id or old.platform_id
        if not fresh.page_answer:
            old.origin = "empty"       # 页面无答案（未答 / 工具填的没保存住）
        else:
            if old.origin == "empty":
                old.origin = "user"
            elif old.origin == "tool" and not same_answer(fresh.page_answer, old.page_answer):
                old.origin = "user"    # 工具填写之后用户手动改过
        old.page_answer = fresh.page_answer
        return old
