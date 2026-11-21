"""模式 A：解析页面加载时 doHomework 的响应（明文 JSON），与 DOM 选中状态合并。

doHomework 结构（阶段0侦察）：
rt.examBase{name, courseName, toChapter, problemNum, totalScore,
            workExamParts[{startSort, questionCount,
                           questionDtos[{eid, name(题干HTML), questionType{id,name},
                                         questionScore, questionOptions[{id, content}]}]}]}
- 选项顺序 = 页面显示顺序（每次加载随机）；选项 id 稳定，与 DOM 中 input 的 value 相同
- 不含标准答案（result 为 null）
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from .models import ExamInfo, RawOption, RawQuestion


class ApiOption(BaseModel):
    id: str
    content: str


class ApiQuestion(BaseModel):
    no: int
    eid: str | None
    type_text: str
    score: float | None
    stem_html: str
    options: list[ApiOption]

    @property
    def option_ids(self) -> frozenset[str]:
        return frozenset(o.id for o in self.options)


class DomState(BaseModel):
    """DOM 中一题的状态（按出现顺序）。"""
    index: int
    option_ids: list[str]
    selected_ids: list[str]


def _score(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_do_homework(body: dict) -> tuple[ExamInfo, list[ApiQuestion]]:
    rt = body.get("rt") or {}
    base = rt.get("examBase") or {}
    if not base:
        raise ValueError("doHomework 响应中没有 rt.examBase")
    qs: list[ApiQuestion] = []
    no = 0
    for part in base.get("workExamParts") or []:
        for d in part.get("questionDtos") or []:
            no += 1
            qs.append(ApiQuestion(
                no=no,
                eid=d.get("eid"),
                type_text=(d.get("questionType") or {}).get("name") or "",
                score=_score(d.get("questionScore")),
                stem_html=d.get("name") or "",
                options=[ApiOption(id=str(o.get("id")), content=o.get("content") or "")
                         for o in d.get("questionOptions") or []],
            ))
    total = base.get("problemNum")
    info = ExamInfo(course=base.get("courseName") or "未知课程", title=base.get("name") or "未知测试",
                    chapter=base.get("toChapter"), total=int(total) if total else len(qs))
    return info, qs


def parse_server_answers(body: dict | None) -> dict[str, set[str]] | None:
    """getStuAnswerInfoNew：rt[eid] = {questionId, answer: "选项id[,选项id…]"}（阶段4实测，单选为单个 id）。"""
    if not body or not isinstance(body.get("rt"), dict):
        return None
    out: dict[str, set[str]] = {}
    for eid, v in body["rt"].items():
        ans = str((v or {}).get("answer") or "")
        out[(v or {}).get("questionId") or eid] = set(re.findall(r"\d+", ans))
    return out


def merge_api_dom(api_qs: list[ApiQuestion], states: list[DomState],
                  server: dict[str, set[str]] | None = None) -> tuple[list[RawQuestion], list[str]]:
    """按选项 id 集合把 DOM 选中状态对应到接口题目；有服务器作答记录时以服务器为准。
    返回 (RawQuestion 列表, 警告)。"""
    warnings: list[str] = []
    by_ids = {frozenset(s.option_ids): s for s in states if s.option_ids}
    out: list[RawQuestion] = []
    for q in api_qs:
        st = by_ids.get(q.option_ids)
        if st is None and q.no - 1 < len(states):
            st = states[q.no - 1]
            if frozenset(st.option_ids) != q.option_ids:
                warnings.append(f"第 {q.no} 题：DOM 与接口选项 id 不一致，未读取选中状态")
                st = None
        sel = set(st.selected_ids) if st else set()
        if server is not None:
            srv = server.get(q.eid or "", set()) & q.option_ids
            if st is not None and srv != sel:
                warnings.append(f"第 {q.no} 题：页面显示与服务器作答记录不一致（以服务器为准；页面上的可能只存在本地、未保存）")
            sel = srv
        out.append(RawQuestion(
            no=q.no, type_text=q.type_text, score=q.score, stem_html=q.stem_html, platform_id=q.eid,
            options=[RawOption(html=o.content, selected=o.id in sel, option_id=o.id) for o in q.options],
        ))
    if len(states) != len(api_qs):
        warnings.append(f"DOM 题数 {len(states)} ≠ 接口题数 {len(api_qs)}")
    return out, warnings
