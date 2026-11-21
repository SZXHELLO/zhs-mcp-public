"""「作业考试」列表页：解析 getStudentHomework、按章节解析指令、批量状态。

侦察结论（docs/FINDINGS.md）：
- 列表页 URL `stuExamWeb.html#/webExamList?recruitId=...`，加载时请求 /student/getStudentHomework（明文）：
  rt.studentHomeworkList[{id(stuExamId), examId, examName, cpOrderNumber, chapterRank, problemNum,
                           achieveCount, achieve, state, startTime, endDate, ...}]
  （含真实姓名 examUserName —— 这里只取需要的字段，绝不落盘个人信息）
- 期末「在线考试」走另一个接口 getStudentFinalExam，本工具**不处理**
- 卡片 `.examItemWrap`，标题 `.examTit .name`，按钮 `.examChartBox1 span.themeBg`（「开始答题」/「继续答题」）
- 点击后平台依次检查：作答锁、异常行为（可能清空记录/0分）、定位、人脸识别 → 通过后 window.open 做题页
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from .models import now

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn_to_int(s: str) -> int | None:
    """「第十二章」→ 12（支持到九十九）。"""
    m = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*章", s or "")
    if not m:
        return None
    t = m.group(1)
    if t.isdigit():
        return int(t)
    if t == "十":
        return 10
    if t.startswith("十"):
        return 10 + _CN_NUM.get(t[1:], 0)
    if "十" in t:
        a, _, b = t.partition("十")
        return _CN_NUM.get(a, 0) * 10 + (_CN_NUM.get(b, 0) if b else 0)
    return _CN_NUM.get(t)


class ListItem(BaseModel):
    name: str
    course: str | None = None
    chapter: int | None
    chapter_text: str | None = None
    problem_num: int | None = None
    answered: int = 0
    state: int | None = None
    start_time: str | None = None
    end_date: str | None = None
    exam_id: str | None = None

    @property
    def status_text(self) -> str:
        if self.answered == 0:
            return "未作答"
        if self.problem_num and self.answered >= self.problem_num:
            return "已全部作答（未提交）"
        return f"已答 {self.answered}/{self.problem_num}"


def parse_homework_list(body: dict, allowed_name_pattern: str) -> list[ListItem]:
    rt = (body or {}).get("rt") or {}
    pat = re.compile(allowed_name_pattern)
    out = []
    for d in rt.get("studentHomeworkList") or []:
        name = (d.get("examName") or "").strip()
        if not pat.search(name):
            continue  # 只处理章节/单元测试
        ch = d.get("chapterRank")
        out.append(ListItem(
            name=name,
            course=d.get("courseName"),
            chapter=int(ch) if isinstance(ch, int) or (isinstance(ch, str) and ch.isdigit()) else cn_to_int(d.get("cpOrderNumber") or name),
            chapter_text=d.get("cpOrderNumber"),
            problem_num=d.get("problemNum"),
            answered=d.get("achieveCount") or 0,
            state=d.get("state"),
            start_time=d.get("startTime"),
            end_date=d.get("endDate"),
            exam_id=d.get("examId"),
        ))
    return out


def resolve_targets(items: list[ListItem], specs: list) -> tuple[list[ListItem], list[str]]:
    """specs：章节号（4 / "4" / "第四章"）或测试名。返回 (按指令顺序的条目, 找不到的说明)。"""
    found, missing = [], []
    for sp in specs:
        s = str(sp).strip()
        n = int(s) if s.isdigit() else cn_to_int(s)
        hit = None
        if n is not None:
            hit = next((it for it in items if it.chapter == n), None)
        if hit is None:
            hit = next((it for it in items if s and s in it.name), None)
        if hit is None:
            missing.append(f"{s}：不在「未提交」的章节测试列表中（可能已提交、未开放或不存在）")
        elif hit not in found:
            found.append(hit)
    return found, missing


# ---------------- 批量任务状态（落盘，跨进程有效） ----------------

class BatchItem(BaseModel):
    name: str
    chapter: int | None = None
    status: str = "pending"        # pending | in_progress | saved | submitted | failed
    note: str | None = None
    verify_ok: bool | None = None
    updated_at: str = Field(default_factory=now)


class BatchState(BaseModel):
    created_at: str = Field(default_factory=now)
    course: str | None = None
    items: list[BatchItem] = Field(default_factory=list)
    stopped: str | None = None     # 中止原因（弹窗/验证码等）

    def get(self, name: str) -> BatchItem | None:
        return next((i for i in self.items if i.name == name), None)

    def mark(self, name: str, status: str | None = None, **kw) -> None:
        it = self.get(name)
        if it is None:
            return
        if status:
            it.status = status
        for k, v in kw.items():
            setattr(it, k, v)
        it.updated_at = now()

    @property
    def all_saved(self) -> bool:
        return all(i.status in ("saved", "submitted") for i in self.items)

    def next_pending(self) -> BatchItem | None:
        return next((i for i in self.items if i.status in ("pending", "in_progress")), None)


class BatchStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> BatchState | None:
        if not self.path.exists():
            return None
        return BatchState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, st: BatchState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(st.model_dump_json(indent=1), encoding="utf-8")

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


def dump_safe_list(items: list[ListItem]) -> str:
    return json.dumps([i.model_dump() for i in items], ensure_ascii=False)
