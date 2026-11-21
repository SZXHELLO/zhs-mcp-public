"""提交条件检查（PLAN 第 8 节）。工具拿不到标准答案，「正确」定义为：
1. 页面题目数 = 题库题目数，且每题都有 final_answer
2. 最近一次 verify(reload) 之后没有再改动，且页面选中项与 final_answer 全部一致
3. 待确认清单为空（低置信度 / 不一致 / 公式有损 / 填写失败都已由用户裁决）
4. confirm=true（仅在用户于对话中明确说「提交」后传入）
"""
from __future__ import annotations

from dataclasses import dataclass

from .store import same_answer
from .models import ExamFile
from .review import build_review


@dataclass
class VerifyState:
    ok: bool = False
    exam_key: str | None = None
    mismatches: int = -1

    def invalidate(self) -> None:
        self.ok = False


def check_submit_conditions(exam: ExamFile, page_total: int | None, verify: VerifyState,
                            threshold: float, confirm: bool) -> list[str]:
    unmet: list[str] = []
    n = len(exam.questions)
    if not page_total or page_total != n:
        unmet.append(f"题库题数 {n} 与页面题目数 {page_total} 不一致，请先 extract_questions")
    missing = [q.no for q in exam.questions if not q.final_answer]
    if missing:
        unmet.append(f"以下题没有最终答案：{missing}")
    not_on_page = [q.no for q in exam.questions if q.final_answer and not same_answer(q.page_answer, q.final_answer)]
    if not_on_page:
        unmet.append(f"以下题页面答案与最终答案不一致：{not_on_page}")
    if not (verify.ok and verify.exam_key == exam.exam_key):
        unmet.append("需要先执行 verify(reload=true) 且结果全部一致（之后不能再有填写改动）")
    groups = build_review(exam, threshold)
    pending = {k: [q.no for q in v] for k, v in groups.items() if v}
    if pending:
        unmet.append(f"待确认清单未清空：{pending}")
    if not confirm:
        unmet.append("需要用户在对话中明确同意提交后传入 confirm=true")
    return unmet
