"""待确认清单：低置信度 / 与我的答案不一致 / 公式有损或填写失败。"""
from __future__ import annotations

from .store import same_answer
from .models import ExamFile, Question

LETTERS = "ABCDEFGH"


def build_review(exam: ExamFile, threshold: float) -> dict[str, list[Question]]:
    low, disagree, problem = [], [], []
    for q in sorted(exam.questions, key=lambda x: x.no):
        if q.user_decision is not None:
            continue  # 已裁决
        if q.ai is not None and q.ai.confidence < threshold:
            low.append(q)
        elif q.origin == "user" and q.ai is not None and not same_answer(q.page_answer, q.ai.answer):
            disagree.append(q)
        elif q.formula_lossy or q.fill_error or q.ai is None:
            problem.append(q)
    return {"low_confidence": low, "disagree": disagree, "problem": problem}


def to_letters(q: Question, answer: list[str] | None) -> list[str]:
    inv = {o: LETTERS[i] for i, o in enumerate(q.options)}
    return [inv.get(a, a) for a in (answer or [])]


def review_item(q: Question) -> dict:
    """一条待确认题（字母按首次选项顺序 A–H，与导出的 Markdown 一致）。"""
    return {
        "qid": q.qid,
        "no": q.no,
        "type": q.type,
        "stem": q.stem,
        "options": {LETTERS[i]: o for i, o in enumerate(q.options)},
        "ai_answer": to_letters(q, q.ai.answer) if q.ai else None,
        "ai_confidence": q.ai.confidence if q.ai else None,
        "ai_explanation": q.ai.explanation if q.ai else None,
        "my_answer": to_letters(q, q.page_answer) if q.origin == "user" else None,
        "page_answer": to_letters(q, q.page_answer),
        "origin": q.origin,
        "formula_lossy": q.formula_lossy,
        "fill_error": q.fill_error,
    }
